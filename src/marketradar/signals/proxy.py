"""Read a merger proxy: locate the section, then extract from the section.

A DEFM14A is about 1.26 million characters -- twenty to forty times an 8-K body
-- and the part worth reading is a few thousand of them. So this is two steps
that fail differently, and keeping them apart is the whole design:

1. **Locate, deterministically.** The headings are standardised by market
   practice: "The Merger Consideration", "Opinion of ... Financial Advisor",
   "Premiums Paid Analysis". A regex finds them, and a regex either finds them or
   does not -- no judgement, no cost, and a miss is reported as ``no_section``
   rather than guessed at.
2. **Extract, with the cheap LLM tier.** The numbers live in prose and HTML
   tables whose phrasing varies by investment bank, which is what regexes cannot
   do: nine hand-written probes against one real proxy hit three, and two of the
   misses were *correct absences*. A located section is 5-20k characters, which
   fits a prompt; the document does not, and **the whole document is never sent**.

**Every figure carries its provenance.** Accession, section heading, the
character offsets of the section, the verbatim quote the number came from, and
the provider, model and prompt version that read it. A number from an LLM with no
provenance is unauditable, and this table will be read a year from now by someone
deciding whether to trust it.

**And the quote is verified against the source.** The model is required to return
the exact text it took the number from; if that text is not in the section it was
given, the figure is rejected as ``UNCITED``. It catches real errors -- two of
nineteen in the hand-check -- and it is the reason a small local model is an
acceptable fallback at all.

**What it does not catch is misattribution, and that is the dominant failure.**
Measured over 20 proxies on 2026-09-11: every wrong number was genuinely in the
text and cited correctly, and answered a different question.

    Farmer Brothers   exchange_ratio 1.0, quoting "each share of common stock of
                      **Merger Sub** ... shall automatically be converted" --
                      boilerplate merger mechanics, not what target holders get.
                      It is an all-cash deal with no ratio at all.
    Royal Gold        consideration $2.00, quoting "C$2.00 in cash per common
                      share" -- a *different* deal inside the same document
                      (Sandstorm buying Horizon), in Canadian dollars, in a proxy
                      where Royal Gold is the buyer.
    Comerica          premium 7%, quoting "premium of 7.0% and 75th percentile
                      premium of 22" -- a percentile from a comparables table.

A citation proves the number was read rather than invented. It says nothing about
*what the number is of*. So **every figure now returns what it is attached to** --
whose shares, which currency, which reference price -- and that is checked against
the filer's own identity before the figure is stored, as :data:`MISATTRIBUTED`.
See :func:`check_attribution`.

**Consideration is a structure, not a scalar.** It was two scalar fields, a cash
price and an exchange ratio, and that shape is wrong for 4 of the 15 takeouts
measured: Enviri's $14.50-$16.50 collar has no single price, CoreCard's
0.2783-0.3142 ratio collar likewise, Veeco and Norfolk Southern pay cash *and*
shares so neither half is the consideration, and FONAR pays $19.00 to two classes
and $6.34 to a third. A collar recorded as its upper bound is a wrong number that
looks right, so the reading is one row per (share class, component) and the scalar
is **derived** -- :func:`scalar_consideration` -- returning None with the shape
that explains why wherever one does not genuinely exist.

**Reason codes, same discipline as deals.** ``not_stated`` and ``not_parsed``
stay distinct, because a stock-for-stock merger genuinely has no cash price per
share and recording that as a parse failure would send someone looking for a
number that was never written down. See :data:`REASONS`.

v1 reads **two things only**: the consideration and the premium. They are the
two that fix known errors -- Dean Foods' $48M liquidation sale read as a takeout,
Anixter's $400M against a $4.5B deal, and the takeout premium CLAUDE.md records
as unmeasurable from a survivor-only price history. Comps, management projections
and DCF ranges come after these two are shown to work.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field as dc_field
from datetime import date
from typing import Any, Final

from marketradar.llm import router

log = logging.getLogger(__name__)

#: Bumped by hand on any prompt change. Stored with every figure: a number read
#: under v1 and one read under v2 are different measurements, and a table that
#: mixes them without saying so is not comparable with itself.
#:
#: ``proxy-v2`` asks for the consideration as a structure and requires an
#: attribution on every figure. A v1 number and a v2 number are not the same
#: measurement -- v1 could not record a collar and could not reject a merger sub's
#: ratio -- so they are stored side by side under different versions rather than
#: one overwriting the other.
PROMPT_VERSION: Final[str] = "proxy-v2"

# --- reason codes -------------------------------------------------------

#: A figure was found and its quote verified against the section.
STATED: Final[str] = "stated"

#: The section exists and genuinely does not contain this figure. A
#: stock-for-stock merger has no cash price per share; an all-cash deal has no
#: exchange ratio. **Not a failure**, and conflating it with one sends the next
#: reader hunting for something nobody wrote down.
NOT_STATED: Final[str] = "not_stated"

#: The section exists and should contain the figure, and we could not get it out.
#: The actionable bucket: a prompt to improve or a bank's phrasing to handle.
NOT_PARSED: Final[str] = "not_parsed"

#: The heading was not found at all, so there was nothing to read. Distinct from
#: ``not_parsed`` because the remedy is a locator pattern, not a prompt.
NO_SECTION: Final[str] = "no_section"

#: The model returned a figure whose quote is not in the text it was given.
#: Rejected rather than stored, and counted -- this is the failure mode the
#: citation check exists for, and its rate is how trust in a provider is earned.
UNCITED: Final[str] = "uncited"

#: The document is a merger proxy, and not one in which *this* company is being
#: bought. A population status, not a field failure.
#:
#: **Measured 2026-09-11 and it changes what the form type means.** `DEFM14A` is
#: a definitive proxy relating to a merger, which includes the *acquirer* asking
#: its own holders to approve a share issuance, a holding-company
#: reorganisation, or an internal restructuring. Dillard's filed one to issue
#: 41,496 Class A and 3,985,776 Class B shares under the NYSE Company Manual;
#: Dillard's is not being acquired, and the first batch read it as a takeout
#: proxy with "no cash price stated" -- which was true, and useless.
#:
#: So the form type is necessary and not sufficient, the same shape as every
#: other identification problem here. See :func:`is_takeout_proxy`.
NOT_A_TAKEOUT: Final[str] = "not_a_takeout"

#: The figure is in the text, correctly quoted, and is *of something else*. A
#: merger sub's share conversion, a comparables-table percentile, a different
#: deal's C$2.00.
#:
#: **The dominant failure, and the one the citation check is blind to.** Measured
#: over 20 proxies on 2026-09-11: every wrong number was genuinely present and
#: cited correctly. Kept distinct from ``uncited`` because they say opposite
#: things about the provider -- an uncited figure means the model invented text,
#: a misattributed one means it read the document correctly and answered the
#: wrong question, and the remedy is a prompt in one case and a locator in the
#: other. See :func:`check_attribution`.
MISATTRIBUTED: Final[str] = "misattributed"

REASONS: Final[tuple[str, ...]] = (
    STATED, NOT_STATED, NOT_PARSED, NO_SECTION, UNCITED, NOT_A_TAKEOUT,
    MISATTRIBUTED,
)

# --- locating -----------------------------------------------------------

#: Headings worth finding, most specific first. Standardised by practice rather
#: than by rule, which is why each carries alternatives and why a miss is
#: reported rather than worked around.
SECTION_PATTERNS: Final[dict[str, tuple[str, ...]]] = {
    # **Not** a bare "Merger Consideration". Measured on a real proxy: that
    # phrase matches 22 times because it is a *defined term* used throughout the
    # document -- the tax discussion, the merger agreement annex, the fairness
    # opinion letter -- and every occurrence is prose. A defined term is not a
    # heading. What is reliable is the operative language that states the number.
    # Anchored on the clause that *carries the amount*, not on a heading, for the
    # same reason the premium is: measured across 15 real takeout proxies, a
    # heading-anchored window held the true per-share figure 6 times in 9, while
    # the phrasing that carries it varies far more than the heading does --
    # "Per Share Price, which is (i) $19.00", "per share merger consideration of
    # $1.29 in cash", "entitled to receive $6.50 in cash ... for each Common
    # Share". Each of those is a different sentence and the same fact.
    #
    # The loose heading alternatives stay as fallbacks, last, for a filing that
    # states the consideration somewhere a money pattern does not reach.
    "merger_consideration": (
        r"(?:right\s+to\s+receive|entitled\s+to\s+receive|consideration\s+of|"
        r"Per\s+Share\s+Price[^.]{0,30}?is)[^.]{0,90}?"
        r"(?:\$\s?[\d,]+(?:\.\d+)?|[\d.]{4,7}\s+(?:of\s+a\s+)?share)",
        r"each\s+(?:issued\s+and\s+outstanding\s+)?share[^.]{0,160}?"
        r"(?:converted\s+into|entitled\s+to\s+receive)",
        r"What\s+(?:will\s+I|I\s+will)\s+receive\s+in\s+the\s+[Mm]erger",
        r"Consideration\s+to\s+be\s+Received",
        r"(?:The\s+)?Merger\s+Consideration\b",
    ),
    # **Not** "Premiums Paid Analysis", which was the first guess and is the
    # wrong section. Measured across 13 real takeout proxies: the deal's own
    # premium appeared in that window **zero times**, at every window size from
    # 3k to 14k characters. The fairness opinion's Premiums Paid Analysis is a
    # table of premiums paid in *other* transactions -- which is what the model
    # said the first time it was asked ("only range quartiles from other
    # transactions are provided") and was right about while the locator was not.
    #
    # The deal's own premium lives in the letter to shareholders and in Reasons
    # for the Merger, under no standard heading at all. So this field anchors on
    # the fact-bearing sentence instead of on a heading, which is still
    # deterministic and is the only thing that finds it.
    #
    # The model's job is then the part a regex cannot do: several premiums are
    # quoted against different reference prices -- 208.5%, 231% and 84.9% in one
    # filing -- and choosing the headline one and saying what it is measured
    # against is judgement, not pattern matching.
    "premium_statement": (
        r"premium\s+of\s+(?:approximately\s+)?[\d.]+\s?%",
        r"[\d.]+\s?%\s+premium\b",
        r"represent(?:s|ed|ing)\s+a\s+premium",
    ),
    # **The one section that is reliably where it says it is.** Measured
    # 2026-09-12 over the 20 cached proxies: all 15 real takeouts carry a named
    # projections heading, all 15 have a multi-year table under it, and all 15
    # name at least one measure a DCF could use. 100%, against 27% for the
    # Premiums Paid Analysis.
    #
    # That is not luck. Sharing forecasts with a buyer triggers a disclosure
    # obligation, so the heading is close to boilerplate -- "Certain Unaudited
    # Prospective Financial Information" is the form of words counsel uses.
    #
    # Why it matters more than the other unread fields: management projections are
    # the **only forward estimate anywhere in this system**. Everything else is
    # as-filed history, and the DCF's weakest input is a flat growth constant that
    # beat every rate fitted from our own 30 quarters out of sample.
    #
    # Measures present across the 15: EBITDA 12, revenue 10, free cash flow 7,
    # capex 7, net income 6, EBIT 2. EBITDA is the one a banker projects.
    # Scoped ``(?i:...)`` rather than relying on the matcher, because
    # :func:`section_window` is **case-sensitive** -- a latent trap these patterns
    # are the first to hit. The older two anchor on lowercase prose ("right to
    # receive", "premium of") and never noticed; a title-case heading does, and
    # AstroNova, Electro Sensors and CoreCard all write it in a case the
    # case-sensitive matcher misses. 12 of 15 became 15 of 15.
    #
    # Not fixed globally, and that is deliberate rather than lazy. Measured
    # 2026-09-12: making the matcher case-insensitive moves **5 of 40** existing
    # windows -- two takeouts whose consideration window shifts, and two
    # liquidations that would go from `no_section` to having one. Two of those
    # documents are already read under the current behaviour, so flipping it
    # mid-measurement would quietly invalidate results rather than improve them.
    # It is a real improvement and it needs its own before-and-after.
    "prospective_financial": (
        r"(?i:(?:Certain\s+)?(?:Unaudited\s+)?Prospective\s+Financial\s+Information)",
        r"(?i:Management(?:'s)?\s+(?:Projections|Forecasts|Financial\s+Projections))",
        r"(?i:(?:Internal\s+)?Financial\s+(?:Projections|Forecasts))",
    ),
    "fairness_opinion": (
        r"Opinion\s+of\s+[A-Z][^\n]{0,70}?(?:Financial\s+Advisor|"
        r"&\s*Co\.?|Securities|Partners|LLC|Inc\.)",
        r"Opinion\s+of\s+the\s+(?:Company|Board)'?s?\s+Financial\s+Advisor",
    ),
}

#: How much text after a heading is handed to the model.
#:
#: 8k characters is roughly two pages: enough to contain the consideration
#: sentence or a premiums table, and small enough that the prompt stays cheap and
#: the citation check stays meaningful. A section cap is also the mechanical
#: guarantee that the whole document is never sent -- see :func:`section_window`.
#:
#: Was 20k, which is ~5k tokens a call and turned out to be the binding
#: constraint rather than a safety margin: a free Groq tier rate-limits on
#: tokens per minute, so 20k-char windows throttled the batch to about one
#: extraction a minute. The passage that states a number is a paragraph, not five
#: pages, and sending five pages bought nothing but queue time.
SECTION_CHARS: Final[int] = 8_000

#: Currency amounts, percentages and bare decimals -- an exchange ratio of
#: 0.6303 is a figure and carries no symbol.
#:
#: Used to *choose between* matches, never to extract anything. Position rules do
#: not work here, measured: the first occurrence of a heading is usually the
#: table of contents and the last is usually the appended merger agreement, so
#: neither "first" nor "last" finds the body. The window that holds the numbers
#: does.
FIGURE_RE: Final[re.Pattern[str]] = re.compile(
    r"\$\s?[\d,]+(?:\.\d+)?|\d+(?:\.\d+)?\s?%|\b\d+\.\d{2,4}\b"
)

#: A window with fewer figures than this was a prose mention rather than the
#: section. Not zero: a heading followed by no numbers cannot be the passage that
#: states a number, and ``no_section`` is more useful than a prompt aimed at the
#: wrong paragraph.
MIN_FIGURES: Final[int] = 3


@dataclass(frozen=True, slots=True)
class Section:
    """A located window of a document, and where it came from."""

    name: str
    heading: str
    start: int
    end: int
    text: str
    #: How many figures the window holds, which is *why* this window was chosen
    #: over the other matches. Carried so a reader can see the selection rather
    #: than trust it.
    figures: int = 0
    #: How many places the heading matched at all.
    candidates: int = 1

    @property
    def chars(self) -> int:
        return len(self.text)


def section_window(text: str, name: str, *,
                   chars: int = SECTION_CHARS,
                   min_figures: int = MIN_FIGURES) -> Section | None:
    """Locate one section by heading, choosing the window that holds numbers.

    **Selection is by figure density, not position**, and that is a measured
    decision rather than a preference. In a real proxy the same heading matches in
    the table of contents, in the body, in the tax discussion and in the appended
    merger agreement, so "first" lands in the contents and "last" lands in the
    annex. Counting the currency amounts, percentages and decimal ratios in each
    candidate window picks the passage that states a number, which is the only one
    worth sending.

    Patterns are tried in order and the first to produce a window with at least
    ``min_figures`` wins, so a specific locator beats a loose one. A heading found
    only in prose returns None: ``no_section`` is a more useful answer than a
    prompt aimed at the wrong paragraph.
    """
    patterns = SECTION_PATTERNS.get(name)
    if not patterns:
        raise ValueError(f"no locator for section {name!r}")
    best: Section | None = None
    for pattern in patterns:
        found = list(re.finditer(pattern, text))
        if not found:
            continue
        scored = []
        for match in found:
            start = match.start()
            window = text[start:start + chars]
            # Ties break to the earliest match, so the choice is deterministic.
            scored.append((len(FIGURE_RE.findall(window)), -start,
                           match.start(), match.group(0), window))
        scored.sort(reverse=True)
        figures, _, start, heading, window = scored[0]
        candidate = Section(
            name=name, heading=re.sub(r"\s+", " ", heading).strip()[:120],
            start=start, end=start + len(window), text=window,
            figures=figures, candidates=len(found),
        )
        if figures >= min_figures:
            return candidate
        if best is None or candidate.figures > best.figures:
            best = candidate
    return best if best is not None and best.figures >= min_figures else None


# --- extracting ---------------------------------------------------------

_SYSTEM: Final[str] = (
    "You extract figures from sections of SEC merger proxy statements. "
    "You reply with a single JSON object and nothing else. "
    "You never infer, compute or estimate a number: you report only figures "
    "written in the text you were given. "
    "For every figure you report you must also return the exact verbatim "
    "substring of the provided text that contains it, copied character for "
    "character, so it can be checked. "
    "If a figure is not present in the text, say so rather than guessing -- "
    "being wrong is far worse than being absent."
)

#: The name of the consideration reading, which is not a :data:`FIELDS` entry.
#:
#: It was two -- ``consideration_per_share`` and ``exchange_ratio``, one scalar
#: each -- and that shape is wrong for 4 of the 15 takeouts measured: a collar has
#: no single price, a mixed deal's cash is not its consideration, and a two-class
#: deal has two. It is now one structured reading, :func:`extract_consideration`,
#: producing one row per (share class, component). Asking in one call is also the
#: correct question: "cash or shares" is one fact about a deal, and asking
#: separately is what made Veeco's cash read ``not_stated`` while its ratio was
#: extracted from the same sentence.
CONSIDERATION: Final[str] = "consideration"

#: What v1 reads. Consideration is structured; the premium is a scalar.
V1_FIELDS: Final[tuple[str, ...]] = (CONSIDERATION, "premium_pct")

#: The scalar fields, and what "absent" means for each.
#:
#: The ``absent_when`` note is in the prompt on purpose. Without it a model asked
#: for a premium in a document that only tabulates other deals' premiums will
#: produce one, and the citation check would pass because some percentage is
#: always nearby.
FIELDS: Final[dict[str, dict[str, str]]] = {
    "premium_pct": {
        "section": "premium_statement",
        "question": (
            "the premium the merger consideration represents over the "
            "company's own share price before announcement, as a percentage. "
            "Several premiums may be quoted against different reference prices "
            "-- a closing price, a 20-day or 90-day volume weighted average -- "
            "and more than one may be a premium paid in some *other* "
            "transaction. Report the one measured against this company's own "
            "pre-announcement price, and prefer the last closing price before "
            "announcement where more than one qualifies"
        ),
        "unit": "percent",
        "absent_when": (
            "the text discusses premiums paid in other transactions without "
            "stating this deal's own premium, in which case report absent"
        ),
        # The premium's attribution has two parts and both were wrong in the
        # hand-check. Comerica's 7% was a 25th-percentile premium from a
        # comparables table -- right number, wrong deal -- and several filings
        # quote three premiums against three reference prices, where naming the
        # reference is the difference between a comparable figure and a number.
        "attributed_to": (
            "the name of the company whose shareholders receive this premium, "
            "exactly as the text names it. If the percentage is a premium paid "
            "in some other transaction, or a quartile, median or percentile of "
            "premiums paid in other transactions, report absent instead"
        ),
        "reference": (
            "what the premium is measured against -- 'closing price on <date>', "
            "'20-trading-day VWAP', '90-day VWAP', 'unaffected price'"
        ),
    },
}


CASH: Final[str] = "cash"
ACQUIRER_SHARES: Final[str] = "acquirer_shares"

#: Shapes a deal's consideration can take, and the reason a scalar is absent.
SCALAR: Final[str] = "scalar"
COLLAR: Final[str] = "collar"
MIXED: Final[str] = "mixed"
SHARES_ONLY: Final[str] = "shares_only"
PER_CLASS: Final[str] = "per_class"
NOT_READ: Final[str] = "not_read"

SHAPES: Final[tuple[str, ...]] = (
    SCALAR, COLLAR, MIXED, SHARES_ONLY, PER_CLASS, NOT_READ,
)


@dataclass(frozen=True, slots=True)
class Amount:
    """A number that may be a range, with what it is denominated in.

    ``low == high`` is a point value and ``low < high`` a collar. There is no
    third case, and no way to write a collar that reads as a point value -- which
    was the defect: the model returned 0.3142 for CoreCard, one end of a
    0.2783-0.3142 ratio collar, and stored as a scalar that is a wrong number
    that looks right.
    """

    low: float
    high: float
    #: ``USD``, ``CAD``; None for a share ratio, which has no currency. Recorded
    #: because a proxy can state a price in a currency that is not the reporting
    #: one -- Royal Gold's document carries "C$2.00 in cash per common share"
    #: belonging to a different deal inside it.
    currency: str | None = None

    def __post_init__(self) -> None:
        if self.high < self.low:
            raise ValueError(f"range is the wrong way round: {self.low}..{self.high}")

    @property
    def is_range(self) -> bool:
        return self.high > self.low

    @property
    def scalar(self) -> float | None:
        """The single number, or None for a collar. Never a midpoint.

        A midpoint would be an invented figure that no document states, which is
        the same mistake as inferring a split ratio from a price jump.
        """
        return None if self.is_range else self.low

    def __str__(self) -> str:
        unit = f" {self.currency}" if self.currency else ""
        if self.is_range:
            return f"{self.low:g}-{self.high:g}{unit}"
        return f"{self.low:g}{unit}"


@dataclass(frozen=True, slots=True)
class Consideration:
    """What one share of one class receives: cash, acquirer shares, or both."""

    share_class: str = "common"
    cash: Amount | None = None
    shares: Amount | None = None

    @property
    def is_mixed(self) -> bool:
        return self.cash is not None and self.shares is not None

    @property
    def is_collar(self) -> bool:
        return any(a.is_range for a in (self.cash, self.shares) if a)

    def __str__(self) -> str:
        legs = []
        if self.shares:
            legs.append(f"{self.shares} acquirer shares")
        if self.cash:
            legs.append(str(self.cash))
        return f"{self.share_class}: " + (" + ".join(legs) or "nothing stated")


def shape_of(considerations: list[Consideration]) -> str:
    """Which of :data:`SHAPES` a filing's consideration is.

    The order matters: a filing can be several of these at once and the *reason a
    scalar is absent* should name the most fundamental one, because that is what a
    reader has to do something about.
    """
    usable = [c for c in considerations if c.cash or c.shares]
    if not usable:
        return NOT_READ
    if len({c.share_class for c in usable}) > 1:
        return PER_CLASS
    only = usable[0]
    if only.is_mixed:
        return MIXED
    if only.shares is not None:
        return SHARES_ONLY
    if only.is_collar:
        return COLLAR
    return SCALAR


def scalar_consideration(
    considerations: list[Consideration],
) -> tuple[float | None, str]:
    """``(usd per share, shape)``. A number only when one genuinely exists.

    Returns None for a collar, a mix, a share-only deal and a multi-class deal --
    four of the fifteen takeouts in the hand-check -- with the shape saying which,
    so three different absences are not three identical NULLs.
    """
    shape = shape_of(considerations)
    if shape != SCALAR:
        return None, shape
    cash = considerations[0].cash
    assert cash is not None  # shape_of guarantees it
    return cash.scalar, shape


@dataclass(frozen=True, slots=True)
class Figure:
    """One extracted number, with everything needed to audit it."""

    accession: str
    field: str
    reason: str
    value: float | None = None
    unit: str | None = None
    #: The verbatim text the number came from, checked against the section.
    quote: str | None = None
    section: str | None = None
    section_heading: str | None = None
    section_start: int | None = None
    section_end: int | None = None
    provider: str | None = None
    model: str | None = None
    prompt_version: str = PROMPT_VERSION
    note: str | None = None

    # --- consideration rows carry a range rather than a scalar ------------
    #: ``low``/``high`` are the authoritative pair; ``value`` is the scalar and is
    #: set only when they are equal. A collar therefore has a range and **no**
    #: value, which is what stops it reading as a point price.
    low: float | None = None
    high: float | None = None
    currency: str | None = None
    share_class: str | None = None
    #: ``cash`` | ``acquirer_shares`` for a consideration row; None for premium.
    component: str | None = None

    # --- attribution: what the figure is *of* ----------------------------
    #: The entity or reference the model says the figure belongs to. The citation
    #: check proves a number was read; this is what says it answers the question
    #: asked. See :func:`check_attribution`.
    attributed_to: str | None = None
    attribution_ok: bool | None = None
    #: What a premium is measured against: a closing price, a 20-day VWAP, an
    #: unaffected price. Stored rather than checked -- there is nothing to check it
    #: against -- and it is what makes two premiums comparable instead of two
    #: numbers. One filing quotes 208.5%, 231% and 84.9% for the same deal.
    reference: str | None = None

    @property
    def usable(self) -> bool:
        return self.reason == STATED and (self.value is not None
                                          or self.low is not None)

    @property
    def amount(self) -> Amount | None:
        if self.low is None or self.high is None:
            return None
        return Amount(low=self.low, high=self.high, currency=self.currency)


def _user_prompt(field: str, spec: dict[str, str], section: Section) -> str:
    return (
        f"Section heading: {section.heading}\n"
        f"Find: {spec['question']}.\n"
        f"Report absent when: {spec['absent_when']}.\n\n"
        "Reply with exactly this JSON shape:\n"
        '{"present": true|false, "value": <number or null>, '
        '"quote": "<verbatim substring or null>", '
        f'"attributed_to": "<{spec["attributed_to"]}>", '
        f'"reference": "<{spec["reference"]}>", '
        '"why_absent": "<short reason or null>"}\n\n'
        "`attributed_to` and `reference` matter as much as the number. A quote "
        "proves you read the figure; they are what say it answers the question "
        "asked rather than a neighbouring one.\n\n"
        "The value must be a plain number with no currency symbol, no commas "
        "and no percent sign. For a percentage report 23.4 rather than 0.234.\n\n"
        "--- TEXT ---\n"
        f"{section.text}"
    )


#: Words that carry no identity, so they cannot be the thing that matches two
#: company names to each other. "Merger Sub" against "Farmer Brothers Co" must not
#: agree on "co".
_NAME_NOISE: Final[frozenset[str]] = frozenset({
    "inc", "corp", "corporation", "company", "co", "llc", "lp", "plc", "ltd",
    "limited", "holdings", "holding", "group", "the", "and", "of", "new",
    "sub", "merger", "parent", "acquisition", "technologies", "international",
    "common", "stock", "shares", "shareholders", "stockholders", "class",
})

#: Phrases that say outright that a figure belongs to something other than this
#: company's own shareholders. Checked before name matching, because a name can
#: overlap by accident and these cannot.
_NOT_THE_FILER: Final[re.Pattern[str]] = re.compile(
    r"\bmerger\s*sub\b|\bsub\s*\d\b|\bsurviving\s+(?:corporation|company)\b|"
    r"\bparent\b|\bacquir(?:er|or)\b|\bbuyer\b|"
    r"\b(?:\d+(?:th|st|nd|rd)\s+)?percentile\b|\bselected\s+transactions?\b|"
    r"\bmedian\b|\bother\s+transactions?\b",
    re.IGNORECASE,
)


def _identity_words(name: str) -> list[str]:
    """Identifying words, in the order written. Order carries the test."""
    out, seen = [], set()
    for word in re.findall(r"[a-z]+", (name or "").lower()):
        if len(word) > 2 and word not in _NAME_NOISE and word not in seen:
            seen.add(word)
            out.append(word)
    return out


def check_attribution(
    attributed_to: str | None, filer: str | None
) -> tuple[bool, str | None]:
    """Does the figure belong to *this* company's shareholders?

    **The check the citation cannot do.** Measured over 20 proxies: every wrong
    figure was genuinely in the text and cited correctly, and answered a different
    question -- a merger sub's share conversion, a different deal's C$2.00, a
    comparables-table percentile. A quote proves the number was read; this is what
    says it is the number that was asked for.

    Two tests, cheapest first. A phrase that names something other than the
    company's own holders fails outright -- "merger sub", "parent", "75th
    percentile" -- because those cannot be right by accident.

    Otherwise the two names must agree on a **head word**: the first identifying
    word of either name must appear in the other, with corporate-form noise
    stripped so "Merger Sub, Inc." and "Farmer Brothers Co" cannot agree on "co".
    Head word rather than any shared word, because any-shared-word is too weak by
    exactly the case that motivated this: Royal Gold's proxy carries Sandstorm
    Gold's consideration, and the two names share "gold". A sector word is not an
    identity. "Farmer Bros. Co." against "Farmer Brothers Co" still agrees, on
    "farmer", which is what an abbreviation leaves intact.

    Name comparison is used here as a **rejection** and never as a join, which is
    what makes it acceptable under the identifier rule: a false mismatch throws
    away a good figure and is visible as a ``misattributed`` row carrying the
    reason, while a false match only leaves today's behaviour unchanged. The costs
    are not symmetric and the cheap direction is the safe one. The filer name
    itself comes from ``companies`` keyed on CIK -- the identifier does the
    identifying, and the string is only what the rejection is measured against.
    """
    if not attributed_to:
        return False, "no attribution returned, so the figure cannot be placed"
    if _NOT_THE_FILER.search(attributed_to):
        return False, (f"attributed to {attributed_to!r}, which names something "
                       "other than this company's own shareholders")
    if not filer:
        # Nothing to compare against. Recorded as unchecked rather than passed:
        # the row carries attribution_ok=None and a consumer can see it was not
        # verified, which is different from having been verified.
        return True, None
    mine, theirs = _identity_words(filer), _identity_words(attributed_to)
    if not mine or not theirs:
        return True, None
    if mine[0] not in theirs and theirs[0] not in mine:
        return False, (f"attributed to {attributed_to!r}, which does not name the "
                       f"filer {filer!r}")
    return True, None


def _normalise_quote(text: str) -> str:
    """Whitespace-collapsed and case-folded, for the citation check.

    The model reflows whitespace when it copies, and rejecting a figure over a
    double space would make the check useless while looking strict. Everything
    else must match: digits, words and order.
    """
    return re.sub(r"\s+", " ", text).strip().casefold()


def extract_field(
    accession: str,
    text: str,
    field: str,
    *,
    client: Any = None,
    providers: tuple[router.Provider, ...] = router.PROVIDERS,
    filer: str | None = None,
) -> Figure:
    """One field from one document, with provenance and a verified citation."""
    spec = FIELDS.get(field)
    if spec is None:
        raise ValueError(
            f"{field!r} is not a scalar v1 field. v1 reads {list(V1_FIELDS)}; "
            f"the scalar ones are {sorted(FIELDS)} and the consideration is "
            "structured -- see extract_consideration. Comps, projections and DCF "
            "ranges come after these two are shown to work."
        )
    section = section_window(text, spec["section"])
    if section is None:
        return Figure(
            accession=accession, field=field, reason=NO_SECTION,
            note=(f"no heading matched for {spec['section']!r}; the remedy is a "
                  "locator pattern, not a prompt"),
        )

    base = dict(
        accession=accession, field=field, section=section.name,
        section_heading=section.heading, section_start=section.start,
        section_end=section.end,
    )
    try:
        answer = router.ask(
            _SYSTEM, _user_prompt(field, spec, section),
            prompt_version=PROMPT_VERSION, client=client, providers=providers,
        )
    except router.LlmError as exc:
        # Degrades, never crashes: the field is unread, which is a fact about
        # this run rather than about the document.
        log.warning("%s %s: no provider answered (%s)", accession, field, exc)
        return Figure(reason=NOT_PARSED, note=f"no provider: {exc}"[:200],
                      **base)

    said = answer.data
    provenance = dict(base, provider=answer.provider, model=answer.model)
    if not said.get("present"):
        return Figure(
            reason=NOT_STATED, unit=spec["unit"],
            note=str(said.get("why_absent") or "")[:200] or None,
            **provenance,
        )

    value, quote = said.get("value"), said.get("quote")
    attributed = str(said.get("attributed_to") or "")[:120] or None
    reference = str(said.get("reference") or "")[:160] or None
    provenance = dict(provenance, attributed_to=attributed, reference=reference)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return Figure(reason=NOT_PARSED, unit=spec["unit"],
                      note=f"value was not a number: {value!r}"[:200],
                      **provenance)
    if not quote or not isinstance(quote, str):
        return Figure(reason=UNCITED, unit=spec["unit"],
                      note="a figure with no quote cannot be checked",
                      **provenance)
    if _normalise_quote(quote) not in _normalise_quote(section.text):
        # The failure mode this tier has. Rejected rather than stored, and
        # counted, because its rate is how trust in a provider is earned.
        return Figure(reason=UNCITED, unit=spec["unit"], quote=quote[:400],
                      note="the quote is not in the section it was given",
                      **provenance)
    ok, why = check_attribution(attributed, filer)
    if not ok:
        # Cited and still wrong, which is the case the citation check cannot see.
        return Figure(reason=MISATTRIBUTED, unit=spec["unit"], quote=quote[:400],
                      attribution_ok=False, note=why, **provenance)
    return Figure(reason=STATED, value=number, unit=spec["unit"],
                  quote=quote[:400], attribution_ok=ok, **provenance)


#: The operative language of a merger agreement: what happens to a share of the
#: company being bought. A proxy that does not contain it is not a proxy about
#: this company being bought.
#:
#: Deterministic and deliberately so -- this gate decides the population, and a
#: population decided by an LLM is a population nobody can reproduce.
#: A share class qualifier between "each" and "share". Canada writes "each
#: issued and outstanding **Common** Share"; a dual-class US filing writes "each
#: share of Class A Common Stock". An enumerated list rather than ``\w+`` because
#: the gate decides the population and a loose qualifier would admit "each of the
#: following" -- the same reason the locator patterns are specific.
_CLASS: Final[str] = (
    r"(?:(?:common|ordinary|subordinate(?:\s+voting)?|multiple\s+voting|"
    r"limited\s+voting|class\s+[a-z]|series\s+[a-z\d]+|variable\s+voting)\s+)"
    r"{0,3}"
)

TAKEOUT_RE: Final[re.Pattern[str]] = re.compile(
    # US: each share ... converted into / entitled to receive.
    rf"each\s+(?:issued\s+and\s+outstanding\s+)?{_CLASS}share[^.]{{0,200}}?"
    r"(?:converted\s+into|entitled\s+to\s+receive)"
    r"|will\s+be\s+entitled\s+to\s+receive[^.]{0,120}?for\s+each\s+share"
    r"|you\s+will\s+(?:be\s+entitled\s+to\s+)?receive[^.]{0,120}?"
    r"for\s+each\s+share"
    # Canada, a plan of arrangement under the CBCA or a provincial act. The
    # operative verb is not "converted into": a share is **transferred to** the
    # purchaser for the consideration, and the holder is named rather than the
    # share. SunOpta was the measured miss -- a real $6.50 cash takeout by KKR
    # that the US-only pattern gated out, which is the expensive direction of
    # error because a gated document says nothing and costs nothing.
    #
    # Anchored on the transfer-for-consideration clause and not on "plan of
    # arrangement" alone, which would be the wrong test: Coeur Mining's proxy and
    # Royal Gold's both describe a plan of arrangement in which the *other*
    # company's shares are acquired, and both are filings where this company is
    # the buyer.
    rf"|each\s+(?:issued\s+and\s+outstanding\s+)?{_CLASS}share\s+"
    r"(?:[^.]{0,120}?)?will\s+be\s+transferred\s+to\s+[^.]{0,60}?"
    r"(?:for|in\s+exchange\s+for)\s+(?:the\s+)?consideration"
    rf"|each\s+holder\s+of\s+{_CLASS}shares?\s+will\s+"
    r"(?:be\s+entitled\s+to\s+)?receive"
    rf"|receive[^.]{{0,120}}?in\s+respect\s+of\s+each\s+{_CLASS}share",
    re.IGNORECASE,
)


def is_takeout_proxy(text: str) -> bool:
    """Is this a proxy about *this* company being acquired?

    The cheap deterministic gate that the first batch was missing. Five of the
    first six documents read came back "no cash price stated", which was true and
    useless: they were acquirers seeking issuance approval, not targets.
    """
    return bool(TAKEOUT_RE.search(text))


#: The consideration prompt asks for the whole structure in one call.
#:
#: One call rather than two -- a cash field and a ratio field -- because they are
#: not independent questions: a deal pays cash, or shares, or both, and asking
#: separately is what produced "not_stated" for Veeco's cash and a wrong ratio for
#: the same filing. It also halves the tokens, which on an 8,000-per-minute free
#: tier is the difference between a re-run and a queue.
CONSIDERATION_PROMPT: Final[str] = (
    "Find what a holder of one share of the company's stock receives in this "
    "transaction.\n"
    "\n"
    "Report every share class separately if they receive different amounts.\n"
    "For each class report the cash and the acquirer shares it receives. A deal "
    "may pay one, the other, or both.\n"
    "If an amount is a range -- a collar, 'not less than X and not more than Y' "
    "-- report low and high as the two ends. If it is a single figure report the "
    "same number as low and high. **Never report the midpoint of a range as if "
    "it were the price.**\n"
    "\n"
    "Reply with exactly this JSON shape:\n"
    '{"present": true|false, "classes": [{"share_class": "<name, or \'common\'>",'
    ' "cash": {"low": <number>, "high": <number>, "currency": "USD"} | null,'
    ' "shares": {"low": <number>, "high": <number>} | null,'
    ' "quote": "<verbatim substring>",'
    ' "attributed_to": "<the name of the company whose shareholders receive'
    ' this, exactly as the text names it>"}],'
    ' "why_absent": "<short reason or null>"}\n'
    "\n"
    "`attributed_to` matters as much as the number. A proxy contains share "
    "conversions belonging to a merger subsidiary, to the acquirer, and "
    "sometimes to an entirely separate transaction described in the same "
    "document. Name the company whose public shareholders receive the amount you "
    "report, and if the amount belongs to a merger sub or to another deal, "
    "report present=false instead.\n"
    "\n"
    "Numbers must be plain: no currency symbol, no commas, no percent sign.\n"
    "\n"
    "--- TEXT ---\n"
)


def _amount(raw: Any, *, currency: str | None) -> Amount | None:
    """An ``Amount`` from the model's ``{"low":..,"high":..}``, or None."""
    if not isinstance(raw, dict):
        return None
    try:
        low = float(raw["low"])
        high = float(raw.get("high", raw["low"]))
    except (KeyError, TypeError, ValueError):
        return None
    if high < low:
        low, high = high, low
    given = raw.get("currency") or currency
    return Amount(low=low, high=high,
                  currency=str(given).upper() if given else None)


def extract_consideration(
    accession: str,
    text: str,
    *,
    client: Any = None,
    providers: tuple[router.Provider, ...] = router.PROVIDERS,
    filer: str | None = None,
) -> list[Figure]:
    """Consideration as rows: one per (share class, component).

    A point value is one row with ``low == high``, a collar one row with
    ``low < high``, a mix two rows for the same class, and two classes two sets of
    rows. No arrangement of these can be misread as a single price, which is the
    whole reason the scalar is derived by :func:`scalar_consideration` rather than
    stored.
    """
    section = section_window(text, "merger_consideration")
    base = dict(accession=accession, field="consideration")
    if section is None:
        return [Figure(reason=NO_SECTION, note="no consideration clause located",
                       **base)]
    try:
        answer = router.ask(_SYSTEM, CONSIDERATION_PROMPT + section.text,
                            prompt_version=PROMPT_VERSION, client=client,
                            providers=providers)
    except router.LlmError as exc:
        return [Figure(reason=NOT_PARSED, note=f"no provider: {exc}"[:200],
                       **base)]

    prov = dict(
        base, section=section.name, section_heading=section.heading,
        section_start=section.start, section_end=section.end,
        provider=answer.provider, model=answer.model,
    )
    said = answer.data
    if not said.get("present"):
        return [Figure(reason=NOT_STATED,
                       note=str(said.get("why_absent") or "")[:200] or None,
                       **prov)]

    rows: list[Figure] = []
    classes = said.get("classes")
    if not isinstance(classes, list) or not classes:
        return [Figure(reason=NOT_PARSED,
                       note="present=true with no classes", **prov)]
    for entry in classes:
        if not isinstance(entry, dict):
            continue
        share_class = str(entry.get("share_class") or "common")[:60]
        quote = entry.get("quote")
        attributed = str(entry.get("attributed_to") or "")[:120] or None
        cited = bool(quote) and isinstance(quote, str) and (
            _normalise_quote(quote) in _normalise_quote(section.text))
        ok, why = check_attribution(attributed, filer)
        for component, amount in ((CASH, _amount(entry.get("cash"),
                                                 currency="USD")),
                                  (ACQUIRER_SHARES, _amount(entry.get("shares"),
                                                            currency=None))):
            if amount is None:
                continue
            if component == ACQUIRER_SHARES:
                amount = Amount(low=amount.low, high=amount.high, currency=None)
            common = dict(prov, share_class=share_class, component=component,
                          quote=(quote or "")[:400] or None,
                          attributed_to=attributed, attribution_ok=ok,
                          currency=amount.currency,
                          unit=("usd_per_share" if component == CASH
                                else "acquirer_shares_per_share"))
            if not cited:
                rows.append(Figure(reason=UNCITED, note=(
                    "the quote is not in the section it was given"
                    if quote else "a figure with no quote cannot be checked"),
                    **common))
                continue
            if not ok:
                rows.append(Figure(reason=MISATTRIBUTED, note=why, **common))
                continue
            rows.append(Figure(reason=STATED, low=amount.low, high=amount.high,
                               value=amount.scalar, **common))
    if not rows:
        return [Figure(reason=NOT_PARSED,
                       note="classes carried no usable amount", **prov)]
    return rows


def considerations_from(rows: list[Figure]) -> list[Consideration]:
    """The structure, rebuilt from the stored rows."""
    by_class: dict[str, dict[str, Amount]] = {}
    for row in rows:
        if row.reason != STATED or row.component is None:
            continue
        amount = row.amount
        if amount is None:
            continue
        by_class.setdefault(row.share_class or "common", {})[row.component] = amount
    return [
        Consideration(share_class=name, cash=legs.get(CASH),
                      shares=legs.get(ACQUIRER_SHARES))
        for name, legs in sorted(by_class.items())
    ]


def read(
    accession: str,
    text: str,
    *,
    fields: tuple[str, ...] | None = None,
    client: Any = None,
    providers: tuple[router.Provider, ...] = router.PROVIDERS,
    filer: str | None = None,
) -> list[Figure]:
    """Every v1 field from one proxy. At least one row per field, always.

    The population gate runs first and costs nothing: a document that is not
    about this company being acquired gets one row per field saying so, and no
    prompt is sent. Sending one would produce a correct ``not_stated`` that reads
    like a coverage problem.

    ``filer`` is the company the accession belongs to, and passing it is what turns
    the attribution check on. It comes from ``companies`` keyed on CIK -- an
    identifier, not a name the model returned -- and is used only to *reject* a
    figure attributed elsewhere. Omitted, figures come back with
    ``attribution_ok`` unset, which is "not checked" rather than "checked and
    fine".
    """
    wanted = fields or V1_FIELDS
    if not is_takeout_proxy(text):
        return [
            Figure(accession=accession, field=name, reason=NOT_A_TAKEOUT,
                   unit=(None if name == CONSIDERATION
                         else FIELDS[name]["unit"]),
                   note=("the document carries no share-conversion language, so "
                         "it is a merger proxy in which this company is not the "
                         "company being bought"))
            for name in wanted
        ]
    out: list[Figure] = []
    for name in wanted:
        if name == CONSIDERATION:
            out.extend(extract_consideration(accession, text, client=client,
                                             providers=providers, filer=filer))
        else:
            out.append(extract_field(accession, text, name, client=client,
                                     providers=providers, filer=filer))
    return out


#: The filing's own directory listing, which names every document in it.
#: Structured JSON rather than scraping the header page, so "which file is the
#: proxy" is read rather than guessed.
INDEX_JSON: Final[str] = "{archives}/edgar/data/{cik}/{bare}/index.json"


def fetch_document(
    cik: str,
    accession: str,
    *,
    client: Any,
    pacer: Any = None,
) -> tuple[str, str]:
    """``(filename, visible text)`` for a filing's main document.

    The largest ``.htm`` in the filing, which for a proxy is the proxy: the
    exhibits are images and the merger agreement is inside the same document. Two
    requests, and deliberately **not** the ``.txt`` full submission, which bundles
    every exhibit and runs to tens of megabytes for the same prose.
    """
    from marketradar import manifest
    from marketradar.signals.deals import visible
    from marketradar.signals.edgar_rss import user_agent

    archives = manifest.get("edgar", "archives").location
    bare = accession.replace("-", "")
    headers = {"User-Agent": user_agent(),
               "Accept-Encoding": "gzip, deflate"}
    if pacer is not None:
        pacer.wait()
    listing = client.get(
        INDEX_JSON.format(archives=archives, cik=str(cik).lstrip("0"),
                          bare=bare),
        headers=headers,
    )
    listing.raise_for_status()
    items = [(it.get("name", ""), int(it.get("size") or 0))
             for it in (listing.json().get("directory") or {}).get("item", [])]
    docs = sorted(
        (n for n, _ in sorted(items, key=lambda x: -x[1])
         if n.lower().endswith((".htm", ".html")) and "index" not in n.lower()),
        key=lambda n: -dict(items)[n],
    )
    if not docs:
        raise ValueError(f"{accession}: no HTML document in the filing")
    if pacer is not None:
        pacer.wait()
    doc = client.get(
        f"{archives}/edgar/data/{str(cik).lstrip('0')}/{bare}/{docs[0]}",
        headers=headers,
    )
    doc.raise_for_status()
    return docs[0], visible(doc.text)


@dataclass(frozen=True, slots=True)
class ReadReport:
    """What a batch of proxies produced, by reason."""

    documents: int
    figures: list[Figure] = dc_field(default_factory=list)

    def by_reason(self, field: str | None = None) -> dict[str, int]:
        out = {r: 0 for r in REASONS}
        for fig in self.figures:
            if field is None or fig.field == field:
                out[fig.reason] = out.get(fig.reason, 0) + 1
        return out

    def lines(self) -> list[str]:
        out = [f"proxy read: {self.documents} documents, "
               f"{len(self.figures)} figures"]
        for name in V1_FIELDS:
            counts = self.by_reason(name)
            total = sum(counts.values())
            if not total:
                continue
            got = counts.get(STATED, 0)
            out.append(f"  {name:<26} {got:>3}/{total:<3} stated "
                       f"({got / total * 100:.0f}%)")
            rest = ", ".join(f"{r} {n}" for r, n in counts.items()
                             if r != STATED and n)
            if rest:
                out.append(f"  {'':<26} {rest}")
        uncited = self.by_reason().get(UNCITED, 0)
        if uncited:
            out.append(f"  {uncited} figure(s) rejected for an unverifiable "
                       "quote -- the citation check earning its keep")
        wrong_of = self.by_reason().get(MISATTRIBUTED, 0)
        if wrong_of:
            out.append(f"  {wrong_of} figure(s) rejected as of something else -- "
                       "present in the text, correctly quoted, wrong question")
        return out
