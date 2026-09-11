"""8-K deal extraction: Items 1.01 and 2.01 into the ``deals`` table.

**Item 1.01 is mostly not M&A, and that was measured before this module was
written.** Across 949 8-Ks filed 2026-08-31 to 2026-09-04, Item 1.01 appeared
on 171 (18.0%) and roughly 16% of those were acquisitions -- about five or six
a day across the whole market. The rest were credit facilities (31% of Item
1.01), equity raises (16%), commercial agreements, securitisations and leases.
The same discipline that established P is 8.72% of Form 4 non-derivative
transactions applies here: the base rate comes first, and the classifier is
built to fit it.

Two independent classifiers, stored side by side rather than merged:

``exhibit_signal``
    An EX-2.x exhibit is attached. Reg S-K 601(b)(2) reserves exhibit 2 for a
    "plan of acquisition, reorganization, arrangement, liquidation or
    succession", so a filer attaching one has already classified the filing.
    This is the primary signal -- it reads the filer's own answer instead of
    guessing at prose, and it needs no keyword list to maintain.

``text_signal``
    The agreement named in the prose, weighted. Secondary.

Over the measured week they agreed on 20 filings, the exhibit fired alone on
2, and the text fired alone on 9 -- three of which were private placements
that borrow the words "purchase agreement". Neither is a superset of the
other, so both are stored along with whether they agreed, and disagreement is
a queue to read rather than a silent miss.

What is *not* reliably in an 8-K, stated plainly because the schema depends
on it:

- **Consideration is often unstated.** 45% of the M&A filings named neither
  cash nor stock in the Item 1.01 prose.
- **Value is stated 68% of the time in the body**, 77% once exhibits are
  read, which is why :func:`extract` will fetch a press release.
- **"Terms were not disclosed" is not a thing filers write.** It occurred
  zero times in 31 M&A filings and zero times in their 19 press releases.
  Filers omit the price; they do not announce the omission. So there is no
  "undisclosed" basis here -- an absent figure is ``not_stated`` and a figure
  we could not read is ``not_parsed``, and the table's check constraint makes
  a null impossible without one of them.
- **Target financials are almost never disclosed.** Only 9.7% of M&A Item
  1.01 filings promise Rule 3-05 statements, so the acquisition multiples
  this table could in principle support usually cannot be computed at all.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from html import unescape
from typing import Any, Final, Iterable, Iterator

import httpx

from marketradar import manifest
from marketradar.freshness import StaleDataError, assert_fresh, utc_today
from marketradar.signals.edgar_rss import (
    EdgarError,
    Pacer,
    REQUEST_TIMEOUT,
    user_agent,
)

log = logging.getLogger(__name__)

SOURCE: Final[str] = "edgar_8k"

#: Amendments carry their own accession and restate the original, so both are
#: read and the accession unique key keeps them as separate rows.
FORM_TYPES: Final[tuple[str, ...]] = ("8-K", "8-K/A")

#: 1.01 is the announcement; 2.01 is the completion. 2.01 needs no classifier
#: -- "Completion of Acquisition or Disposition of Assets" is M&A by the
#: item's own definition -- but it is extracted through the same path so the
#: two sides of one deal land in one table.
DEAL_ITEMS: Final[tuple[str, ...]] = ("1.01", "2.01")

#: SIC 6770, "blank checks". The structural way to spot a SPAC: it comes off
#: the filing header rather than out of the prose.
SIC_BLANK_CHECK: Final[str] = "6770"


class DealError(RuntimeError):
    """An 8-K could not be read."""


# --- the filing ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Filing:
    """Everything the submission header says, before any document is read."""

    accession: str
    cik: str
    company: str
    form: str
    filed: date
    items: tuple[str, ...]
    doc_types: tuple[str, ...]
    doc_names: tuple[str, ...]
    sic: str | None = None
    event_date: date | None = None
    base: str = ""

    @property
    def is_deal_item(self) -> bool:
        return any(i in DEAL_ITEMS for i in self.items)

    @property
    def primary(self) -> str | None:
        for kind, name in zip(self.doc_types, self.doc_names):
            if kind == self.form and name.endswith((".htm", ".txt")):
                return name
        return None

    def documents(self, prefix: str) -> list[str]:
        """Filenames whose exhibit type starts with ``prefix``."""
        return [
            name
            for kind, name in zip(self.doc_types, self.doc_names)
            if kind.upper().startswith(prefix) and name.endswith((".htm", ".txt"))
        ]

    @property
    def exhibit_signal(self) -> bool:
        """An EX-2.x exhibit: the filer's own classification of the filing."""
        return any(
            k.upper() == "EX-2" or k.upper().startswith("EX-2.")
            for k in self.doc_types
        )


#: The submission header, HTML-escaped inside -index-headers.html. One request
#: yields the item list, every document's exhibit type, and the SIC code --
#: which is why this file is read rather than the full submission text, whose
#: median size is two orders of magnitude larger.
_DOC_BLOCK = re.compile(
    r"&lt;TYPE&gt;([^&\r\n]+).*?&lt;FILENAME&gt;([^&\r\n]+)", re.S
)
_SIC = re.compile(r"STANDARD INDUSTRIAL CLASSIFICATION:.*?\[(\d{4})\]")
_PERIOD = re.compile(r"CONFORMED PERIOD OF REPORT:\s*(\d{8})")

#: Official 8-K item names to their numbers. EDGAR writes the *name* in the
#: header, never the number, and has carried more than one spelling for the
#: same item over the years -- so unmapped names are counted and surfaced
#: rather than silently dropped, because a dropped item reads as an absent one.
ITEM_NUMBERS: Final[dict[str, str]] = {
    "Entry into a Material Definitive Agreement": "1.01",
    "Termination of a Material Definitive Agreement": "1.02",
    "Bankruptcy or Receivership": "1.03",
    "Mine Safety - Reporting of Shutdowns and Patterns of Violations": "1.04",
    "Material Cybersecurity Incidents": "1.05",
    "Completion of Acquisition or Disposition of Assets": "2.01",
    "Results of Operations and Financial Condition": "2.02",
    "Creation of a Direct Financial Obligation or an Obligation under an "
    "Off-Balance Sheet Arrangement of a Registrant": "2.03",
    "Triggering Events That Accelerate or Increase a Direct Financial "
    "Obligation or an Obligation under an Off-Balance Sheet Arrangement": "2.04",
    "Costs Associated with Exit or Disposal Activities": "2.05",
    "Cost Associated with Exit or Disposal Activities": "2.05",
    "Material Impairments": "2.06",
    "Notice of Delisting or Failure to Satisfy a Continued Listing Rule or "
    "Standard; Transfer of Listing": "3.01",
    "Unregistered Sales of Equity Securities": "3.02",
    "Material Modifications to Rights of Security Holders": "3.03",
    "Material Modification to Rights of Security Holders": "3.03",
    "Changes in Registrant's Certifying Accountant": "4.01",
    "Non-Reliance on Previously Issued Financial Statements or a Related "
    "Audit Report or Completed Interim Review": "4.02",
    "Changes in Control of Registrant": "5.01",
    "Departure of Directors or Certain Officers; Election of Directors; "
    "Appointment of Certain Officers: Compensatory Arrangements of Certain "
    "Officers": "5.02",
    "Amendments to Articles of Incorporation or Bylaws; Change in Fiscal "
    "Year": "5.03",
    "Temporary Suspension of Trading Under Registrant's Employee Benefit "
    "Plans": "5.04",
    "Amendment to Registrant's Code of Ethics, or Waiver of a Provision of "
    "the Code of Ethics": "5.05",
    "Amendments to the Registrant's Code of Ethics, or Waiver of a Provision "
    "of the Code of Ethics": "5.05",
    "Change in Shell Company Status": "5.06",
    "Submission of Matters to a Vote of Security Holders": "5.07",
    "Shareholder Director Nominations": "5.08",
    "Shareholder Nominations Pursuant to Exchange Act Rule 14a-11": "5.08",
    "ABS Informational and Computational Material": "6.01",
    "Change of Servicer or Trustee": "6.02",
    "Change in Credit Enhancement or External Support": "6.03",
    "Failure to Make a Required Distribution": "6.04",
    "Securities Act Updating Disclosure": "6.05",
    "Static Pool": "6.06",
    "Regulation FD Disclosure": "7.01",
    "Other Events": "8.01",
    "Financial Statements and Exhibits": "9.01",
}

#: A handful of filer agents write the raw SGML tag instead of the caption.
_RAW_ITEM = re.compile(r"^(?:&lt;|<)ITEMS(?:&gt;|>)\s*(\d\.\d\d)")


def parse_items(header_html: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(item numbers, names that could not be mapped) from a header page."""
    numbers: list[str] = []
    unknown: list[str] = []
    for line in header_html.splitlines():
        if "ITEM INFORMATION:" not in line:
            continue
        name = unescape(line.split(":", 1)[1]).strip()
        raw = _RAW_ITEM.match(name)
        if raw:
            numbers.append(raw.group(1))
            continue
        number = ITEM_NUMBERS.get(name)
        if number:
            numbers.append(number)
        else:
            unknown.append(name)
    return tuple(sorted(set(numbers))), tuple(unknown)


def parse_header(
    html: str, *, accession: str, cik: str, company: str, form: str, filed: date,
    base: str,
) -> Filing:
    """A :class:`Filing` from one ``-index-headers.html`` page."""
    items, unknown = parse_items(html)
    if unknown:
        log.warning("%s: unmapped 8-K item names %s", accession, list(unknown))
    docs = _DOC_BLOCK.findall(html)
    sic = _SIC.search(html)
    period = _PERIOD.search(html)
    event: date | None = None
    if period:
        try:
            raw = period.group(1)
            event = date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
        except ValueError:
            event = None
    return Filing(
        accession=accession,
        cik=cik.lstrip("0") or "0",
        company=company,
        form=form,
        filed=filed,
        items=items,
        doc_types=tuple(t for t, _ in docs),
        doc_names=tuple(n for _, n in docs),
        sic=sic.group(1) if sic else None,
        event_date=event,
        base=base,
    )


# --- reading documents --------------------------------------------------

_TAG = re.compile(r"<[^>]+>")

#: Unicode spaces EDGAR filers use freely: nbsp, thin, en/em, hair, and the
#: zero-width ones. Matching on entity *spellings* instead left
#: "Item 1.01&#160;Entry into a Material Definitive Agreement" unrecognised
#: and lost 28 sections in 171 filings, so entities are decoded first and the
#: resulting characters collapsed here.
_WS = re.compile(
    "[\\s   -‏    ⁠　﻿]+"
)


def visible(html: str) -> str:
    """Rendered text of an EDGAR document.

    Tags are stripped *before* entities are decoded. Unescaping first would
    turn a literal ``&lt;`` in the prose into a ``<`` and the tag stripper
    would then eat the sentence after it.
    """
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<(br|/p|/div|/tr|/h\d)[^>]*>", " \n", html)
    text = unescape(_TAG.sub(" ", html))
    for a, b in (
        ("‘", "'"), ("’", "'"), ("“", '"'),
        ("”", '"'), ("–", "-"), ("—", "-"),
    ):
        text = text.replace(a, b)
    return _WS.sub(" ", text).strip()


#: ``Item\s*`` rather than ``Item\s+``: after entity decoding, some filers
#: leave no gap at all between the number and its caption.
_ITEM_HEAD = re.compile(r"Item\s*(\d\.\d\d)", re.I)

_CAPTIONS: Final[dict[str, re.Pattern[str]]] = {
    "1.01": re.compile(r"entry\s+into\s+a\s+material\s+definitive\s+agreement", re.I),
    "2.01": re.compile(
        r"completion\s+of\s+(?:the\s+)?acquisition\s+or\s+disposition", re.I
    ),
}


def section(text: str, item: str) -> str:
    """The prose under one item heading, to the next item heading.

    A heading is "Item N.NN" with that item's caption following it. A bare
    mention is not: 8-Ks routinely say "the disclosure under Item 1.01 is
    incorporated herein by reference" from a later item, and taking the last
    mention picked up exactly that boilerplate and left 63% of a sample
    unclassifiable.
    """
    marks = [(m.start(), m.end(), m.group(1)) for m in _ITEM_HEAD.finditer(text)]
    here = [i for i, (_, _, num) in enumerate(marks) if num == item]
    if not here:
        return ""
    caption = _CAPTIONS.get(item)
    captioned = [
        i for i in here
        if caption and caption.match(text[marks[i][1]:marks[i][1] + 70].lstrip(" .:-"))
    ]
    # The cover page repeats every caption in a checkbox list, so among real
    # headings take the last: the body follows the table of contents.
    k = captioned[-1] if captioned else here[0]

    begin = marks[k][0]
    end = len(text)
    for pos, _, num in marks[k + 1:]:
        if num != item and pos > begin + 40:
            end = pos
            break
    body = text[begin:end]
    # A heading with nothing under it means the cover-page copy won.
    return body if len(body) > 120 else ""


# --- the text classifier ------------------------------------------------

_STRONG, _WEAK = 3, 1

#: Weighted because an unweighted pass produced ten false M&A hits in forty:
#: two asset-backed note sales, a tenth amendment to a credit agreement, a
#: securities purchase agreement, and a poison pill whose rights plan says
#: "merger" repeatedly. Each tied with a genuine signal and the tie broke
#: arbitrarily. An instrument that names itself is worth three; a word that
#: merely appears near deals is worth one.
_PHRASES: Final[tuple[tuple[str, int, str], ...]] = (
    ("m_and_a", _STRONG, r"agreement and plan of (?:merger|reorganization)"),
    ("m_and_a", _STRONG, r"\bmerger agreement\b"),
    ("m_and_a", _STRONG, r"business combination agreement"),
    ("m_and_a", _STRONG, r"\barrangement agreement\b"),
    ("m_and_a", _STRONG, r"(?:stock|share|equity|asset|unit) purchase agreement"),
    ("m_and_a", _STRONG, r"membership interest purchase agreement"),
    ("m_and_a", _STRONG, r"\bacquisition agreement\b"),
    ("m_and_a", _WEAK, r"\bagreed to acquire\b|\bto acquire all\b"),
    ("m_and_a", _WEAK, r"acquisition of (?:all|substantially all)"),
    ("m_and_a", _WEAK, r"\btender offer\b"),
    ("m_and_a", _WEAK, r"\bmerger sub\b"),
    ("debt", _STRONG, r"\bcredit agreement\b"),
    ("debt", _STRONG, r"loan (?:and security )?agreement"),
    ("debt", _STRONG, r"\bindenture\b"),
    ("debt", _STRONG, r"note purchase agreement"),
    ("debt", _STRONG, r"\bcredit facility\b"),
    ("debt", _STRONG, r"revenue interest financing"),
    ("debt", _WEAK, r"revolving credit|\bterm loan\b"),
    ("debt", _WEAK, r"promissory note|convertible note"),
    ("debt", _WEAK, r"\bforbearance\b"),
    ("debt", _WEAK, r"exchange agreement"),
    ("securitization", _STRONG, r"asset.backed notes"),
    ("securitization", _STRONG, r"pooling and servicing agreement"),
    ("securitization", _STRONG, r"\breceivables\b.{0,40}\btrust\b"),
    ("securitization", _WEAK, r"\bissuing entity\b|\bcertificateholders?\b"),
    ("equity_raise", _STRONG, r"securities purchase agreement"),
    ("equity_raise", _STRONG, r"underwriting agreement"),
    ("equity_raise", _STRONG, r"placement agen(?:cy|t) agreement"),
    ("equity_raise", _STRONG, r"at.the.market"),
    ("equity_raise", _STRONG, r"equity (?:line|purchase|distribution) agreement"),
    ("equity_raise", _WEAK, r"registration rights agreement"),
    ("equity_raise", _WEAK, r"subscription agreement"),
    ("equity_raise", _WEAK, r"\bpre-funded warrant"),
    ("equity_raise", _WEAK, r"standby equity"),
    ("employment", _STRONG, r"employment agreement"),
    ("employment", _STRONG, r"separation (?:and release )?agreement"),
    ("employment", _STRONG, r"transition (?:and separation )?agreement"),
    ("employment", _STRONG, r"consulting agreement"),
    ("employment", _WEAK, r"\bseverance\b|retention (?:agreement|award|bonus)"),
    ("employment", _WEAK, r"indemnification agreement"),
    ("commercial", _STRONG, r"supply agreement"),
    ("commercial", _STRONG, r"licen[cs]e agreement"),
    ("commercial", _STRONG, r"collaboration agreement"),
    ("commercial", _STRONG, r"distribution agreement"),
    ("commercial", _STRONG, r"master services agreement"),
    ("commercial", _STRONG, r"management services agreement"),
    ("commercial", _STRONG, r"development agreement"),
    ("commercial", _STRONG, r"manufacturing agreement"),
    ("commercial", _WEAK, r"\boff.?take\b|\bjoint venture\b"),
    ("real_estate", _STRONG, r"\blease agreement\b|\bground lease\b|\bthe leases?\b"),
    ("real_estate", _WEAK, r"purchase and sale agreement"),
    ("governance", _STRONG, r"rights agreement|rights plan"),
    ("governance", _STRONG, r"cooperation agreement|standstill agreement"),
    ("settlement", _STRONG, r"settlement agreement"),
)
_COMPILED: Final[tuple[tuple[str, int, re.Pattern[str]], ...]] = tuple(
    (bucket, weight, re.compile(pattern, re.I))
    for bucket, weight, pattern in _PHRASES
)


def text_signal(prose: str) -> tuple[str | None, dict[str, int]]:
    """Top agreement bucket and every bucket's score.

    ``None`` when nothing matched *or* the top two tie -- an ambiguous
    document is reported as ambiguous rather than assigned to whichever
    bucket a dictionary happened to yield first.
    """
    scores: dict[str, int] = {}
    for bucket, weight, rx in _COMPILED:
        if rx.search(prose):
            scores[bucket] = scores.get(bucket, 0) + weight
    if not scores:
        return None, scores
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None, scores
    return ranked[0][0], scores


# --- the fields ---------------------------------------------------------

_MULTIPLIER: Final[dict[str, int]] = {
    "": 1, "thousand": 1_000, "k": 1_000,
    "million": 1_000_000, "mm": 1_000_000, "m": 1_000_000,
    "billion": 1_000_000_000, "bn": 1_000_000_000, "b": 1_000_000_000,
}
_MONEY = re.compile(
    r"\$\s?([\d][\d,]*(?:\.\d+)?)\s*(billion|million|thousand|bn|mm|[kmb])?\b",
    re.I,
)
#: Words that make a nearby figure the deal price rather than a filing fee, a
#: par value, or a share count.
_PRICE_CONTEXT = re.compile(
    r"(?:aggregate|total|purchase price|consideration|enterprise value|"
    r"equity value|transaction value|base purchase price|valued at|"
    r"for a price of|in exchange for|acquisition price)",
    re.I,
)
#: A deal is worth more than a rounding error and less than the largest
#: transaction in history. Outside this band the parse is wrong, not the deal.
_VALUE_FLOOR: Final[Decimal] = Decimal("10000")
_VALUE_CEILING: Final[Decimal] = Decimal("500000000000")

_CASH = re.compile(r"\bin cash\b|\bcash consideration\b|\ball.cash\b", re.I)
_STOCK = re.compile(
    r"\bshares? of (?:the )?(?:parent|acquir\w+|buyer|company)?\s*common stock\b|"
    r"\bstock consideration\b|\bexchange ratio\b|\bshare consideration\b",
    re.I,
)
_RULE_305 = re.compile(
    r"financial statements[^.]{0,140}(?:will be|to be|are to be) filed by amendment|"
    r"required by Item 9\.01\(a\)|not later than seventy.one|"
    r"not later than 71 calendar days|Rule 3-05",
    re.I,
)
_TARGET_FIGURES = re.compile(
    r"\b(?:revenue|revenues|EBITDA|net income|annual sales)\b[^.]{0,60}"
    r"(?:\$|approximately|of about)",
    re.I,
)
_SELLER = re.compile(
    r"\bas seller\b|\bagreed to sell\b|\bwill sell\b|\bsold the (?:equity|business)\b|"
    r"\bdivest\w*\b|\bdisposition of\b",
    re.I,
)
_ACQUIRER = re.compile(
    r"\bas (?:buyer|purchaser|acquirer)\b|\bagreed to acquire\b|\bwill acquire\b|"
    r"\bto acquire all\b|\bthe \"?Acquirer\"?\b|\bthe \"?Buyer\"?\b",
    re.I,
)
#: "... with Foo Holdings, Inc., a Delaware corporation" -- the standard shape
#: of the counterparty clause. Deliberately narrow: a wrong company name is
#: worse than an absent one, so anything that does not match this reports
#: ``not_parsed`` rather than a guess.
_COUNTERPARTY = re.compile(
    # "to" and "from" carry the seller side -- "sold the business to X" is as
    # common a shape as "entered into an agreement with X".
    r"\b(?:with|among|by and among|and|to|from)\s+"
    r"([A-Z][\w.&'-]*(?:\s+[A-Z][\w.&'-]*){0,6}"
    r"(?:,?\s+(?:Inc|LLC|L\.L\.C|Ltd|Corp|Corporation|Company|Holdings|"
    r"L\.P|LP|plc|N\.V|S\.A|GmbH)\.?))",
)
#: Shells that exist only to be merged out of existence. Naming one as the
#: counterparty is accurate and worthless -- Vertiv's buyer read as "Vultra
#: Merger Sub, Inc." on the first run.
_SHELL_PARTY = re.compile(
    r"\bmerger\s+sub\b|\bacquisition\s+sub\b|\bmerger\s+corp\b|"
    r"\bsub\s+(?:i{1,3}|\d)\b",
    re.I,
)

#: A business unit changing hands rather than a company.
#:
#: **This is a deal type, not a caveat, and the reason is arithmetic.** Dividing
#: a division's price by its parent's whole revenue is a category error: the
#: numerator is part of the company and the denominator is all of it, so the
#: multiple comes out small. A screen ranking cheap deals first would rank its
#: own mistakes first, and nothing about the output would look wrong -- a low
#: multiple is exactly what it was looking for.
#:
#: Measured from the other side: of 2,112 filings with a stated value and a
#: pre-deal annual report, 1,778 -- 84% -- filed another 10-K afterwards, so the
#: thing sold was not the filer. That measurement is the *confirmation* and it
#: arrives years later; this regex is the classification available at extraction,
#: from the filer's own description. Both are kept, because they fail
#: differently: prose is available immediately and can be wrong, the filing
#: record is slow and cannot.
#:
#: Deliberately requires a named unit. "Substantially all of the assets" on its
#: own is how a whole small company is sold as well, so it is not enough.
_DIVISION = re.compile(
    r"\b(?:segment|division|business unit|product line|operating unit)\b|"
    r"\bits\s+(?:wholly[- ]owned\s+)?subsidiar(?:y|ies)\b|"
    r"\bcarve[- ]out\b|"
    r"\bsubstantially all of the assets of (?:its|the)\s+\w+\s+"
    r"(?:business|segment|division|operations)\b",
    re.I,
)

#: A whole company changing hands. Beats :data:`_DIVISION` when both fire,
#: because a merger agreement that converts every share is not a carve-out
#: however many segments the prose happens to mention.
_WHOLE_COMPANY = re.compile(
    r"\beach (?:issued and outstanding )?share[^.]{0,120}"
    r"(?:converted into|cancelled and converted)\b|"
    r"\ball of the (?:issued and )?outstanding (?:shares|capital stock|equity)"
    r"\s+of the Company\b|"
    r"\bmerged? with and into\b[^.]{0,80}\bthe Company\b|"
    r"\bwill (?:become|be) a wholly[- ]owned subsidiary of\b",
    re.I,
)

_SPAC_TEXT = re.compile(
    r"business combination agreement|\btrust account\b|\bblank check\b|"
    r"\bde-SPAC\b|\bsponsor\b.{0,40}\bfounder shares\b",
    re.I,
)


#: Market-sizing copy. A press release that says the addressable market is
#: expected to reach $500 billion is not announcing a $500 billion deal --
#: which is exactly what a nano-cap's exhibit produced on the first run.
#: Never a deal price, in a body or an exhibit.
_MARKET_SIZE = re.compile(
    r"\b(?:addressable|global|worldwide|total)\s+market\b|\bmarket\s+(?:size|"
    r"opportunity|is\s+(?:expected|projected|forecast))|\bTAM\b|"
    r"\bindustry\s+is\s+(?:expected|projected)|\bexpected\s+to\s+reach\b|"
    r"\bprojected\s+to\s+(?:reach|grow)\b|\bassets\s+under\s+management\b",
    re.I,
)

#: Deal cues required before a figure in *exhibit* text counts. The 8-K body
#: is a legal description of one agreement, so price language alone is enough
#: there; a press release is marketing and talks about many numbers.
_DEAL_CUE = re.compile(
    r"\bacquisi\w+|\bacquire\w*|\bmerger\b|\bpurchase price\b|"
    r"\bconsideration\b|\btransaction\b|\bcombination\b",
    re.I,
)


def value(prose: str, *, strict: bool = False) -> tuple[Decimal | None, str | None]:
    """The announced consideration, or ``(None, None)``.

    Only figures within 140 characters of price language count. A raw
    largest-dollar-figure heuristic picks up escrow amounts, break fees, and
    the par value of a share class.

    ``strict`` additionally requires a deal cue in the window and is used for
    exhibit text, where the surrounding prose is promotional rather than
    contractual.
    """
    best: Decimal | None = None
    phrase: str | None = None
    for match in _MONEY.finditer(prose):
        window = prose[max(0, match.start() - 140):match.start() + 60]
        if not _PRICE_CONTEXT.search(window):
            continue
        if _MARKET_SIZE.search(window):
            continue
        if strict and not _DEAL_CUE.search(window):
            continue
        try:
            amount = Decimal(match.group(1).replace(",", ""))
        except InvalidOperation:
            continue
        amount *= _MULTIPLIER[(match.group(2) or "").lower()]
        if not (_VALUE_FLOOR <= amount <= _VALUE_CEILING):
            continue
        if best is None or amount > best:
            best, phrase = amount, match.group(0).strip()
    return best, phrase


def consideration(prose: str) -> str:
    cash, stock = bool(_CASH.search(prose)), bool(_STOCK.search(prose))
    if cash and stock:
        return "mixed"
    if cash:
        return "cash"
    if stock:
        return "stock"
    return "not_stated"


def target_financials(prose: str) -> str:
    if _RULE_305.search(prose):
        return "rule_305_promised"
    if _TARGET_FIGURES.search(prose):
        return "figures_in_filing"
    return "none_disclosed"


def parties(prose: str, company: str) -> tuple[str, str | None, str | None, str | None, str]:
    """``(filer_role, counterparty, acquirer, target, party_basis)``.

    Acquirer and target are *derived* from the filer's role rather than named
    directly, because an 8-K names its parties by defined term ("the Buyer",
    "Parent", "Merger Sub") and resolving those to entities is not something
    a regular expression should be trusted with. When the role is unclear the
    counterparty is still recorded and both derived columns stay NULL --
    a wrong acquirer is worse than a missing one.
    """
    seller = bool(_SELLER.search(prose))
    buyer = bool(_ACQUIRER.search(prose))

    # A merger sub is an empty Delaware shell incorporated to be merged out of
    # existence; naming it as the counterparty is technically true and useless.
    # Take the first named party that is not one, and not the filer itself.
    other = None
    for match in _COUNTERPARTY.finditer(prose):
        name = match.group(1).strip(" ,")
        if _SHELL_PARTY.search(name):
            continue
        if name.lower()[:12] == company.lower()[:12]:
            continue
        other = name
        break

    if buyer and not seller:
        return "acquirer", other, company, other, (
            "derived_from_role" if other else "not_parsed"
        )
    if seller and not buyer:
        return "seller", other, other, company, (
            "derived_from_role" if other else "not_parsed"
        )
    if other:
        return "party", other, None, None, "counterparty_only"
    return "not_stated", None, None, None, "not_parsed"


def deal_type(filing: Filing, prose: str, bucket: str | None) -> str:
    """``operating`` | ``division_sale`` | ``spac`` | ``securitization`` |
    ``unclassified``.

    SPACs are separated because a quarter of the M&A set is a de-SPAC, and a
    de-SPAC has no operating acquirer, no target financials, and no
    computable multiple. Left in the same population it would drag any
    forward-return study toward the behaviour of trust-account shells.

    ``division_sale`` is separated for a sharper version of the same reason: a
    business unit's price over its parent's revenue is not a small multiple, it
    is a category error -- part of a company divided by all of it. See
    :data:`_DIVISION`. It is a *type* rather than a flag because every consumer
    has to exclude it, and a flag is the thing that gets forgotten.

    SIC 6770 ("blank checks") is checked first: it comes off the filing
    header, so it is the filer's own registration rather than our reading.
    """
    if filing.sic == SIC_BLANK_CHECK:
        return "spac"
    if _SPAC_TEXT.search(prose) and bucket == "m_and_a":
        return "spac"
    if bucket == "securitization":
        return "securitization"
    if bucket == "m_and_a" or filing.exhibit_signal:
        # Whole-company language wins: a merger that converts every share is
        # not a carve-out however many segments the prose mentions.
        if _DIVISION.search(prose) and not _WHOLE_COMPANY.search(prose):
            return "division_sale"
        return "operating"
    return "unclassified"


# --- the extracted row --------------------------------------------------


@dataclass(frozen=True, slots=True)
class Deal:
    accession: str
    cik: str
    company: str
    filed_date: date
    event_date: date | None
    items: str
    exhibit_signal: bool
    text_signal: str | None
    classifiers_agree: bool
    deal_type: str
    consideration: str
    value_usd: Decimal | None
    value_basis: str
    value_text: str | None
    filer_role: str
    counterparty: str | None
    acquirer: str | None
    target: str | None
    party_basis: str
    target_financials: str
    url: str
    scores: dict[str, int] = field(default_factory=dict)

    @property
    def is_candidate(self) -> bool:
        """Fired at least one classifier. Only candidates are stored."""
        return self.exhibit_signal or self.text_signal == "m_and_a"


def extract(
    filing: Filing,
    body_html: str,
    *,
    exhibit_texts: Iterable[str] = (),
) -> Deal:
    """One :class:`Deal` from a filing's primary document.

    ``exhibit_texts`` are read only for the value: 68% of M&A Item 1.01
    filings state a figure in the body and 77% state one once the press
    release is included, so the extra fetch buys nine points and is worth it.
    Everything else is taken from the 8-K body, which is the filer's own
    legal description rather than its marketing copy.
    """
    text = visible(body_html)
    prose = section(text, "1.01") or section(text, "2.01") or text
    bucket, scores = text_signal(prose)

    amount, phrase = value(prose)
    basis = "stated_8k" if amount is not None else "not_stated"
    if amount is None:
        for exhibit in exhibit_texts:
            amount, phrase = value(exhibit, strict=True)
            if amount is not None:
                basis = "stated_exhibit"
                break

    role, other, acquirer, target, party_basis = parties(prose, filing.company)
    # Rule 3-05 lives under Item 9.01, not under 1.01 -- searching only the
    # deal section found it zero times in 39 filings where the whole document
    # finds it in about one in ten.
    joined = text + " " + " ".join(exhibit_texts)
    return Deal(
        accession=filing.accession,
        cik=filing.cik,
        company=filing.company,
        filed_date=filing.filed,
        event_date=filing.event_date,
        items=",".join(filing.items),
        exhibit_signal=filing.exhibit_signal,
        text_signal=bucket,
        classifiers_agree=filing.exhibit_signal == (bucket == "m_and_a"),
        deal_type=deal_type(filing, prose, bucket),
        consideration=consideration(prose),
        value_usd=amount,
        value_basis=basis,
        value_text=phrase,
        filer_role=role,
        counterparty=other,
        acquirer=acquirer,
        target=target,
        party_basis=party_basis,
        target_financials=target_financials(joined),
        url=filing.base + (filing.primary or ""),
        scores=scores,
    )


# --- fetching -----------------------------------------------------------


def _client(client: httpx.Client | None) -> tuple[httpx.Client, bool]:
    if client is not None:
        return client, False
    return httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=True), True


def daily_filings(
    day: date,
    *,
    client: httpx.Client | None = None,
    pacer: Pacer | None = None,
) -> Iterator[Filing]:
    """Every 8-K filed on ``day`` that carries Item 1.01 or 2.01.

    One request for the daily index, then one ``-index-headers.html`` per
    8-K. The header page is read rather than the full submission because it
    answers three questions at once -- which items, which exhibit types, and
    the SIC code -- in about 27 KB, where the complete submission text for a
    deal 8-K routinely runs to twenty megabytes of scanned exhibit images.
    """
    # Weekends have no index and SEC answers with 403 rather than 404, which
    # would abort a week-long window on its first Saturday.
    if day.weekday() >= 5:
        return

    archives = manifest.get("edgar", "archives").location
    index_base = manifest.get("edgar", "daily_index").location
    headers = {"User-Agent": user_agent(), "Accept-Encoding": "gzip, deflate"}
    con, owns = _client(client)
    pacer = pacer or Pacer()
    try:
        quarter = (day.month - 1) // 3 + 1
        url = f"{index_base}/{day.year}/QTR{quarter}/form.{day:%Y%m%d}.idx"
        pacer.wait()
        resp = con.get(url, headers=headers)
        if resp.status_code in (403, 404):
            log.info("no daily index for %s (holiday?) -- HTTP %s",
                     day, resp.status_code)
            return
        resp.raise_for_status()

        rows: list[tuple[str, str, str]] = []
        for line in resp.text.splitlines():
            form = line[:12].strip()
            if form not in FORM_TYPES:
                continue
            path = line.split()[-1]
            if path.endswith(".txt"):
                rows.append((form, line[12:74].strip(), path))

        log.info("%s: %d 8-K filings", day, len(rows))
        for form, company, path in rows:
            cik = path.split("/")[2]
            accession = path.rsplit("/", 1)[1].removesuffix(".txt")
            base = f"{archives}/edgar/data/{cik}/{accession.replace('-', '')}/"
            pacer.wait()
            try:
                page = con.get(base + accession + "-index-headers.html",
                               headers=headers)
                page.raise_for_status()
            except httpx.HTTPError as exc:
                log.warning("skipping %s: %s", accession, exc)
                continue
            filing = parse_header(
                page.text, accession=accession, cik=cik, company=company,
                form=form, filed=day, base=base,
            )
            if filing.is_deal_item:
                yield filing
    except httpx.HTTPError as exc:
        raise EdgarError(f"could not read 8-K filings for {day}: {exc}") from exc
    finally:
        if owns:
            con.close()


#: Exhibit prefixes worth a second request when the body states no figure.
#: EX-99 is the press release, which is where a headline number lives.
PRICE_EXHIBITS: Final[tuple[str, ...]] = ("EX-99",)


def fetch(
    start: date,
    end: date,
    *,
    client: httpx.Client | None = None,
    pacer: Pacer | None = None,
    read_exhibits: bool = True,
) -> list[Deal]:
    """Every deal candidate filed between ``start`` and ``end`` inclusive."""
    headers = {"User-Agent": user_agent(), "Accept-Encoding": "gzip, deflate"}
    con, owns = _client(client)
    pacer = pacer or Pacer()
    out: list[Deal] = []
    try:
        day = start
        while day <= end:
            for filing in daily_filings(day, client=con, pacer=pacer):
                if not filing.primary:
                    log.warning("%s has no primary document", filing.accession)
                    continue
                pacer.wait()
                try:
                    body = con.get(filing.base + filing.primary, headers=headers)
                    body.raise_for_status()
                except httpx.HTTPError as exc:
                    log.warning("skipping %s: %s", filing.accession, exc)
                    continue

                deal = extract(filing, body.text)
                # Only fetch a press release when it can change something:
                # a candidate whose value is still missing.
                if (read_exhibits and deal.is_candidate
                        and deal.value_usd is None):
                    texts = []
                    for prefix in PRICE_EXHIBITS:
                        for name in filing.documents(prefix)[:1]:
                            pacer.wait()
                            try:
                                ex = con.get(filing.base + name, headers=headers)
                                ex.raise_for_status()
                                texts.append(visible(ex.text))
                            except httpx.HTTPError as exc:
                                log.warning("exhibit %s: %s", name, exc)
                    if texts:
                        deal = extract(filing, body.text, exhibit_texts=texts)
                if deal.is_candidate:
                    out.append(deal)
            day += timedelta(days=1)
    finally:
        if owns:
            con.close()
    return out


# --- persistence --------------------------------------------------------


def load(deals: Iterable[Deal], con: Any = None, *, min_rows: int = 1,
         historical: bool = False) -> dict[str, int]:
    """Upsert deals, keyed on accession.

    Candidates are stored whether or not the two classifiers agreed -- the
    disagreements are the review queue, and a table that held only the
    confident rows would make them unreadable.
    """
    from marketradar import storage

    # Deduplicate on accession before the insert, keeping the last seen.
    #
    # One 8-K can reach us twice: a filing made by a parent and a subsidiary
    # is listed under both CIKs, and Postgres rejects the whole statement
    # with "ON CONFLICT DO UPDATE command cannot affect row a second time"
    # when both land in one batch. The accession identifies the filing, so
    # the second copy is the same document, not a second deal.
    unique: dict[str, Deal] = {}
    for deal in deals:
        unique[deal.accession] = deal
    rows = list(unique.values())

    con = con or storage.connect(attach_postgres=True)
    if not storage.postgres_attached(con):
        raise DealError("No Postgres attached; cannot upsert deals.")

    def ex(sql: str) -> None:
        con.execute("CALL postgres_execute('pg', ?)", [sql])

    def q(sql: str) -> list[tuple]:
        return con.execute("SELECT * FROM postgres_query('pg', ?)", [sql]).fetchall()

    def lit(v: Any) -> str:
        if v is None:
            return "null"
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, Decimal):
            return str(v)
        return "'" + str(v).replace("'", "''") + "'"

    before = q("select count(*) from deals")[0][0]
    for chunk in (rows[i:i + 100] for i in range(0, len(rows), 100)):
        values = ", ".join(
            "({})".format(", ".join(lit(v) for v in (
                d.accession, d.cik, d.company, d.filed_date.isoformat(),
                d.event_date.isoformat() if d.event_date else None, d.items,
                d.exhibit_signal, d.text_signal, d.classifiers_agree,
                d.deal_type, d.consideration, d.value_usd, d.value_basis,
                d.value_text, d.filer_role, d.counterparty, d.acquirer,
                d.target, d.party_basis, d.target_financials, SOURCE, d.url,
            )))
            for d in chunk
        )
        ex(
            "insert into deals (accession, cik, company, filed_date, "
            "event_date, items, exhibit_signal, text_signal, "
            "classifiers_agree, deal_type, consideration, value_usd, "
            "value_basis, value_text, filer_role, counterparty, acquirer, "
            "target, party_basis, target_financials, source, url) "
            f"values {values} "
            "on conflict (accession) do update set "
            "exhibit_signal = excluded.exhibit_signal, "
            "text_signal = excluded.text_signal, "
            "classifiers_agree = excluded.classifiers_agree, "
            "deal_type = excluded.deal_type, "
            "consideration = excluded.consideration, "
            "value_usd = excluded.value_usd, "
            "value_basis = excluded.value_basis, "
            "value_text = excluded.value_text, "
            "filer_role = excluded.filer_role, "
            "counterparty = excluded.counterparty, "
            "acquirer = excluded.acquirer, target = excluded.target, "
            "party_basis = excluded.party_basis, "
            "target_financials = excluded.target_financials, "
            "ingested_at = now()"
        )

    after = q("select count(*) from deals")[0][0]
    stored = con.sql(
        "select * from (values " +
        ", ".join(
            f"('{d.accession}', DATE '{d.filed_date.isoformat()}')" for d in rows
        ) + ") as t(accession, date)"
    ) if rows else con.sql("select '' as accession, current_date as date where false")

    # A daily sweep must be fresh; a historical one cannot be.
    #
    # Same split as tiingo's closed-year contract, and for the same reason: a
    # backfill of 2019-2023 filings has a newest date years in the past and
    # that is not a defect, but dropping the assertion entirely would let a
    # backfill that matched nothing exit green. So only the wall-clock test
    # goes, and the span is asserted instead -- a historical batch whose
    # filings are all dated today is a sign the date parsing broke, which the
    # live contract would never notice.
    if historical:
        observed = assert_fresh(
            "deals",
            stored,
            min_rows=min_rows,
            date_column=None,
            expect_cols=("accession", "date"),
        )
        row = stored.query("s", "select min(date) AS lo, max(date) AS hi FROM s")
        lo, hi = row.fetchone()
        if lo is None or hi is None:
            raise StaleDataError(
                f"deals: {observed.row_count:,} historical rows but no filing "
                "dates, so the batch's span cannot be established."
            )
        if hi > utc_today():
            raise StaleDataError(
                f"deals: newest historical filing is {hi.isoformat()}, which is "
                f"after {utc_today().isoformat()}. Check the date parsing."
            )
    else:
        assert_fresh(
            "deals",
            stored,
            min_rows=min_rows,
            date_column="date",
            max_staleness_days=10,
            expect_cols=("accession", "date"),
        )
    return {"candidates": len(rows), "before": before, "after": after,
            "inserted": after - before}


# --- targeted by CIK, for the filers a day sweep has least of -------------

#: Form types worth asking a CIK's filing history for.
#:
#: The same 8-K pair the daily sweep reads. The other five watched form types in
#: CLAUDE.md -- S-4, DEFM14A, SC 13D, SC TO-T, SC 13E-3 -- are **not** read here
#: either, and that is a known gap rather than an oversight: they need their own
#: extractors, because a merger proxy is not an 8-K and parsing it as one would
#: produce a row that is present, plausible and wrong. Named so the gap is on the
#: record where someone looking for it will find it.
CIK_FORMS: Final[tuple[str, ...]] = FORM_TYPES


def filings_for_cik(
    cik: str,
    *,
    client: httpx.Client | None = None,
    pacer: Pacer | None = None,
    since: date | None = None,
) -> Iterator[Filing]:
    """Every deal-item 8-K a CIK ever filed, newest first.

    The same shape as :func:`daily_filings` and deliberately the same code after
    the first request: one ``-index-headers.html`` per candidate, parsed by
    :func:`parse_header`, so a filing found this way is indistinguishable from
    one found by walking a daily index. Two populations that had to agree would
    otherwise drift.

    **Why by CIK at all.** A company that stopped filing in 2021 is thinly
    represented in a day sweep -- it existed for a third of the years the sweep
    covers -- and it is exactly the population an acquisition study needs. Asking
    EDGAR for one company's history is a single request; finding the same filings
    by walking eleven years of daily indexes is about fifty thousand. The CIK
    list comes from the point-in-time filer universe, which is the thing that
    knows these companies existed at all.
    """
    archives = manifest.get("edgar", "archives").location
    url = manifest.get("sec_submissions", "company").location.format(
        cik=str(cik).lstrip("0").zfill(10)
    )
    headers = {"User-Agent": user_agent(), "Accept-Encoding": "gzip, deflate"}
    con, owns = _client(client)
    pacer = pacer or Pacer()
    try:
        pacer.wait()
        resp = con.get(url, headers=headers)
        if resp.status_code == 404:
            log.info("no submissions record for CIK %s", cik)
            return
        resp.raise_for_status()
        payload = resp.json()
        recent = (payload.get("filings") or {}).get("recent") or {}
        company = payload.get("name") or ""
        rows = list(zip(
            recent.get("accessionNumber") or (),
            recent.get("form") or (),
            recent.get("items") or (),
            recent.get("filingDate") or (),
        ))
        for accession, form, items, filed_text in rows:
            if form not in CIK_FORMS:
                continue
            # The JSON's `items` is a comma-joined string. Filtered here so a
            # company's thousand filings cost one request rather than a
            # thousand index-header fetches.
            if not any(i in DEAL_ITEMS
                       for i in str(items or "").replace(" ", "").split(",")):
                continue
            try:
                filed = date.fromisoformat(filed_text)
            except (TypeError, ValueError):
                log.warning("%s: unparseable filing date %r", accession,
                            filed_text)
                continue
            if since is not None and filed < since:
                continue
            bare = accession.replace("-", "")
            base = f"{archives}/edgar/data/{str(cik).lstrip('0')}/{bare}/"
            pacer.wait()
            try:
                page = con.get(base + accession + "-index-headers.html",
                               headers=headers)
                page.raise_for_status()
            except httpx.HTTPError as exc:
                log.warning("skipping %s: %s", accession, exc)
                continue
            filing = parse_header(
                page.text, accession=accession, cik=str(cik).lstrip("0"),
                company=company, form=form, filed=filed, base=base,
            )
            if filing.is_deal_item:
                yield filing
    except httpx.HTTPError as exc:
        raise EdgarError(
            f"could not read filing history for CIK {cik}: {exc}") from exc
    finally:
        if owns:
            con.close()


def fetch_for_ciks(
    ciks: Iterable[str],
    *,
    client: httpx.Client | None = None,
    pacer: Pacer | None = None,
    read_exhibits: bool = True,
    since: date | None = None,
    on_progress: Any = None,
) -> list[Deal]:
    """Deal candidates for a list of CIKs, by asking EDGAR about each one.

    ``on_progress(index, cik, found)`` is called after each CIK so a long sweep
    can be checkpointed by the caller -- this function holds no state of its own
    and can be resumed by passing the remainder of the list.
    """
    headers = {"User-Agent": user_agent(), "Accept-Encoding": "gzip, deflate"}
    con, owns = _client(client)
    pacer = pacer or Pacer()
    out: list[Deal] = []
    try:
        for index, cik in enumerate(ciks):
            found_here = 0
            for filing in filings_for_cik(cik, client=con, pacer=pacer,
                                          since=since):
                if not filing.primary:
                    log.warning("%s has no primary document", filing.accession)
                    continue
                pacer.wait()
                try:
                    body = con.get(filing.base + filing.primary, headers=headers)
                    body.raise_for_status()
                except httpx.HTTPError as exc:
                    log.warning("skipping %s: %s", filing.accession, exc)
                    continue
                deal = extract(filing, body.text)
                if (read_exhibits and deal.is_candidate
                        and deal.value_usd is None):
                    texts = []
                    for prefix in PRICE_EXHIBITS:
                        for name in filing.documents(prefix)[:1]:
                            pacer.wait()
                            try:
                                ex = con.get(filing.base + name, headers=headers)
                                ex.raise_for_status()
                                texts.append(visible(ex.text))
                            except httpx.HTTPError as exc:
                                log.warning("exhibit %s: %s", name, exc)
                    if texts:
                        deal = extract(filing, body.text, exhibit_texts=texts)
                if deal.is_candidate:
                    out.append(deal)
                    found_here += 1
            if on_progress is not None:
                on_progress(index, cik, found_here)
    finally:
        if owns:
            con.close()
    return out
