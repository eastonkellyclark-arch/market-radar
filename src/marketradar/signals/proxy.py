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
given, the figure is rejected as ``not_parsed``. That is a cheap, mechanical
check on the one failure mode this tier actually has -- inventing a plausible
number -- and it is the reason a small local model is an acceptable fallback.

**Reason codes, same discipline as deals.** ``not_stated`` and ``not_parsed``
stay distinct, because a stock-for-stock merger genuinely has no cash price per
share and recording that as a parse failure would send someone looking for a
number that was never written down. See :data:`REASONS`.

v1 extracts **two fields only**: the consideration and the premium. They are the
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
PROMPT_VERSION: Final[str] = "proxy-v1"

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

REASONS: Final[tuple[str, ...]] = (
    STATED, NOT_STATED, NOT_PARSED, NO_SECTION, UNCITED, NOT_A_TAKEOUT,
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

#: The two fields v1 reads, and what "absent" means for each.
#:
#: The ``absent_when`` note is in the prompt on purpose. Without it a model asked
#: for a cash price per share in a stock-for-stock merger will produce one, and
#: the citation check would pass because some dollar figure is always nearby.
FIELDS: Final[dict[str, dict[str, str]]] = {
    "consideration_per_share": {
        "section": "merger_consideration",
        "question": (
            "the cash amount a holder of one share of the company's common "
            "stock will receive in the merger"
        ),
        "unit": "usd_per_share",
        "absent_when": (
            "the consideration is shares of the acquirer rather than cash "
            "(a stock-for-stock merger), in which case report absent"
        ),
    },
    # The other half of "consideration", and not a third field sneaking in: a
    # stock-for-stock merger's consideration *is* an exchange ratio, so without
    # this every stock deal reads `not_stated` and the table learns nothing about
    # half the market. The first document tested was exactly that case --
    # 0.6303 acquirer shares per target share, no cash at all.
    "exchange_ratio": {
        "section": "merger_consideration",
        "question": (
            "the number of acquirer shares a holder of one share of the "
            "company's common stock will receive in the merger"
        ),
        "unit": "acquirer_shares_per_share",
        "absent_when": (
            "the consideration is cash rather than shares of the acquirer, in "
            "which case report absent"
        ),
    },
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
    },
}


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

    @property
    def usable(self) -> bool:
        return self.reason == STATED and self.value is not None


def _user_prompt(field: str, spec: dict[str, str], section: Section) -> str:
    return (
        f"Section heading: {section.heading}\n"
        f"Find: {spec['question']}.\n"
        f"Report absent when: {spec['absent_when']}.\n\n"
        "Reply with exactly this JSON shape:\n"
        '{"present": true|false, "value": <number or null>, '
        '"quote": "<verbatim substring or null>", '
        '"why_absent": "<short reason or null>"}\n\n'
        "The value must be a plain number with no currency symbol, no commas "
        "and no percent sign. For a percentage report 23.4 rather than 0.234.\n\n"
        "--- TEXT ---\n"
        f"{section.text}"
    )


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
) -> Figure:
    """One field from one document, with provenance and a verified citation."""
    spec = FIELDS.get(field)
    if spec is None:
        raise ValueError(
            f"{field!r} is not in v1. Two fields are read -- "
            f"{sorted(FIELDS)} -- and comps, projections and DCF ranges come "
            "after those two are shown to work."
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
    return Figure(reason=STATED, value=number, unit=spec["unit"],
                  quote=quote[:400], **provenance)


#: The operative language of a merger agreement: what happens to a share of the
#: company being bought. A proxy that does not contain it is not a proxy about
#: this company being bought.
#:
#: Deterministic and deliberately so -- this gate decides the population, and a
#: population decided by an LLM is a population nobody can reproduce.
TAKEOUT_RE: Final[re.Pattern[str]] = re.compile(
    r"each\s+(?:issued\s+and\s+outstanding\s+)?share[^.]{0,200}?"
    r"(?:converted\s+into|entitled\s+to\s+receive)"
    r"|will\s+be\s+entitled\s+to\s+receive[^.]{0,120}?for\s+each\s+share"
    r"|you\s+will\s+(?:be\s+entitled\s+to\s+)?receive[^.]{0,120}?"
    r"for\s+each\s+share",
    re.IGNORECASE,
)


def is_takeout_proxy(text: str) -> bool:
    """Is this a proxy about *this* company being acquired?

    The cheap deterministic gate that the first batch was missing. Five of the
    first six documents read came back "no cash price stated", which was true and
    useless: they were acquirers seeking issuance approval, not targets.
    """
    return bool(TAKEOUT_RE.search(text))


def read(
    accession: str,
    text: str,
    *,
    fields: tuple[str, ...] | None = None,
    client: Any = None,
    providers: tuple[router.Provider, ...] = router.PROVIDERS,
) -> list[Figure]:
    """Every v1 field from one proxy. One row per field, always.

    The population gate runs first and costs nothing: a document that is not
    about this company being acquired gets one row per field saying so, and no
    prompt is sent. Sending one would produce a correct ``not_stated`` that reads
    like a coverage problem.
    """
    wanted = fields or tuple(FIELDS)
    if not is_takeout_proxy(text):
        return [
            Figure(accession=accession, field=name, reason=NOT_A_TAKEOUT,
                   unit=FIELDS[name]["unit"],
                   note=("the document carries no share-conversion language, so "
                         "it is a merger proxy in which this company is not the "
                         "company being bought"))
            for name in wanted
        ]
    return [extract_field(accession, text, f, client=client,
                          providers=providers) for f in wanted]


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
        for name in FIELDS:
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
        return out
