"""Form 4 ownership documents: parsing, and who actually filed them.

Parses the **ownership XML**, not the index page. The index tells you a Form 4
exists; the XML tells you what happened, and what happened is almost entirely
carried by the transaction code. Scraping the human-readable page would mean
re-deriving a field the filer already stated.

**Transaction codes are the whole game.** Only ``P`` — an open-market purchase
— is a decision to buy. The high-volume codes are compensation:

    P   open-market purchase        <- conviction
    S   open-market sale
    A   grant or award              <- compensation, not a decision
    M   option exercise             <- scheduled, not a decision
    F   shares withheld for tax     <- automatic, not even a trade
    G   gift
    C   conversion
    D   disposition to the issuer

Every vesting date produces a burst of A, M and F filings from several
insiders at once. Counting those as a cluster turns the payroll calendar into
a buy signal, which is why cluster detection filters to ``P`` in the
non-derivative table and nothing else.

**Roles are not interchangeable.** A 10% holder adding to a position and a CFO
buying are different signals: one is a fund adjusting an allocation, the other
is someone with the accounts in front of them. A filer can hold several roles
at once, and this keeps all of them rather than collapsing to a label.

**Amendments supersede.** A 4/A restates an earlier filing and carries its own
accession number, so counting both double-counts the transaction. The
amendment names the period it corrects; :func:`supersede` uses that.

Cluster detection is deliberately not in this module yet — the transaction
code distribution comes first.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Iterable, Iterator
from xml.etree import ElementTree as ET

import httpx

from marketradar import manifest
from marketradar.signals.edgar_rss import (
    EdgarError,
    Pacer,
    REQUEST_TIMEOUT,
    normalize_accession,
    user_agent,
)

log = logging.getLogger(__name__)

KIND: Final[str] = "form4"
SOURCE: Final[str] = "edgar_form4"

#: Open-market purchase. The only code that is a decision to buy.
CODE_PURCHASE: Final[str] = "P"

#: Codes that are compensation or mechanics rather than conviction. Named so
#: the exclusion is greppable and so nobody has to remember why M is not a buy.
COMPENSATION_CODES: Final[frozenset[str]] = frozenset({"A", "M", "F", "G", "C", "D"})

_OWNERSHIP = re.compile(r"<ownershipDocument>.*?</ownershipDocument>", re.S)


class Form4Error(RuntimeError):
    """A Form 4 could not be parsed."""


# --- the document -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Owner:
    cik: str | None
    name: str
    is_director: bool = False
    is_officer: bool = False
    is_ten_percent: bool = False
    is_other: bool = False
    officer_title: str = ""

    @property
    def is_insider(self) -> bool:
        """An officer or director: someone with the accounts in front of them.

        Deliberately not the same question as :attr:`is_ten_percent`. A filer
        can be both, and when they are, this is still true -- the CEO who also
        holds 12% is an insider who happens to be large, not a fund.
        """
        return self.is_director or self.is_officer

    @property
    def roles(self) -> tuple[str, ...]:
        out = []
        if self.is_officer:
            out.append("officer")
        if self.is_director:
            out.append("director")
        if self.is_ten_percent:
            out.append("ten_percent")
        if self.is_other:
            out.append("other")
        return tuple(out)


@dataclass(frozen=True, slots=True)
class Transaction:
    code: str
    shares: Decimal | None
    price: Decimal | None
    acquired_disposed: str          # "A" or "D"
    security_title: str
    transaction_date: date | None
    is_derivative: bool

    @property
    def is_open_market_purchase(self) -> bool:
        """Code P in the non-derivative table. Nothing else counts."""
        return self.code == CODE_PURCHASE and not self.is_derivative

    @property
    def value(self) -> Decimal | None:
        if self.shares is None or self.price is None:
            return None
        return self.shares * self.price


@dataclass(frozen=True, slots=True)
class Form4:
    accession: str | None
    document_type: str              # "4" or "4/A"
    period_of_report: date | None
    issuer_cik: str | None
    issuer_name: str
    issuer_symbol: str
    owners: list[Owner] = field(default_factory=list)
    transactions: list[Transaction] = field(default_factory=list)
    #: The filer ticked the Rule 10b5-1(c) affirmative-defence box. Present on
    #: every modern Form 4, in both boolean spellings.
    #:
    #: Document-level, which is a real limitation: on a filing carrying several
    #: transactions it says "at least one of these was under a plan", not which
    #: one. Treat it as a flag on the filing, never as proof about a specific
    #: row.
    aff10b5_one: bool = False
    #: A footnote mentions 10b5-1. Some filers disclose the plan that way
    #: instead of, or as well as, the box. Weaker evidence and unattributed --
    #: the footnote may well be attached to a sale rather than the purchase.
    mentions_10b5_1: bool = False

    @property
    def is_planned(self) -> bool:
        """Scheduled under a pre-arranged plan, on the filer's own say-so.

        A purchase set up months ago is not a decision made today. This is the
        flag, not the footnote: the footnote is unattributed and would sweep in
        filings whose plan language is about an unrelated sale.
        """
        return self.aff10b5_one

    @property
    def is_amendment(self) -> bool:
        return self.document_type.upper().endswith("/A")

    @property
    def purchases(self) -> list[Transaction]:
        return [t for t in self.transactions if t.is_open_market_purchase]

    @property
    def has_open_market_purchase(self) -> bool:
        return any(t.is_open_market_purchase for t in self.transactions)


# --- parsing ------------------------------------------------------------


def _value(node: Any, tag: str) -> str:
    """Most Form 4 fields are ``<tag><value>x</value></tag>``, but not all.

    ``transactionCode`` sits bare inside ``transactionCoding`` while
    ``transactionShares`` wraps its number, and a footnote-only field has the
    element with no value at all. One accessor handles the three shapes so a
    caller never has to know which it is looking at.
    """
    found = node.find(tag)
    if found is None:
        return ""
    inner = found.find("value")
    if inner is not None:
        return (inner.text or "").strip()
    return (found.text or "").strip()


def _flag(node: Any, tag: str) -> bool:
    raw = _value(node, tag).strip().lower()
    return raw in ("1", "true", "y", "yes")


def _decimal(raw: str) -> Decimal | None:
    if not raw:
        return None
    try:
        return Decimal(raw.replace(",", ""))
    except InvalidOperation:
        return None


def _date(raw: str) -> date | None:
    try:
        return date.fromisoformat(raw.strip()[:10])
    except (ValueError, AttributeError):
        return None


def extract_xml(document: str) -> str | None:
    """The ownership XML out of a full submission text file.

    A submission wraps several documents in SGML; only one is the ownership
    form. Slicing it out beats parsing the wrapper, which is not XML and does
    not pretend to be.
    """
    match = _OWNERSHIP.search(document)
    return match.group(0) if match else None


def parse(document: str, accession: str | None = None) -> Form4:
    """One ownership document to a :class:`Form4`.

    Accepts either the raw ownership XML or a whole submission file.
    """
    xml = document if document.lstrip().startswith("<ownershipDocument")\
        else extract_xml(document)
    if not xml:
        raise Form4Error("no <ownershipDocument> in the submission")

    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise Form4Error(f"ownership document was not valid XML: {exc}") from exc

    issuer = root.find("issuer")
    owners: list[Owner] = []
    for node in root.findall("reportingOwner"):
        ident = node.find("reportingOwnerId")
        rel = node.find("reportingOwnerRelationship")
        owners.append(
            Owner(
                cik=(_value(ident, "rptOwnerCik") or None) if ident is not None else None,
                name=_value(ident, "rptOwnerName") if ident is not None else "",
                is_director=_flag(rel, "isDirector") if rel is not None else False,
                is_officer=_flag(rel, "isOfficer") if rel is not None else False,
                is_ten_percent=(
                    _flag(rel, "isTenPercentOwner") if rel is not None else False
                ),
                is_other=_flag(rel, "isOther") if rel is not None else False,
                officer_title=_value(rel, "officerTitle") if rel is not None else "",
            )
        )

    transactions: list[Transaction] = []
    for table, derivative in (("nonDerivativeTable", False),
                              ("derivativeTable", True)):
        section = root.find(table)
        if section is None:
            continue
        tag = "derivativeTransaction" if derivative else "nonDerivativeTransaction"
        for node in section.findall(tag):
            coding = node.find("transactionCoding")
            amounts = node.find("transactionAmounts")
            if coding is None:
                continue
            transactions.append(
                Transaction(
                    code=_value(coding, "transactionCode").strip().upper(),
                    shares=_decimal(
                        _value(amounts, "transactionShares") if amounts is not None else ""
                    ),
                    price=_decimal(
                        _value(amounts, "transactionPricePerShare")
                        if amounts is not None else ""
                    ),
                    acquired_disposed=(
                        _value(amounts, "transactionAcquiredDisposedCode").strip().upper()
                        if amounts is not None else ""
                    ),
                    security_title=_value(node, "securityTitle"),
                    transaction_date=_date(_value(node, "transactionDate")),
                    is_derivative=derivative,
                )
            )

    return Form4(
        accession=normalize_accession(accession),
        document_type=_value(root, "documentType") or "4",
        period_of_report=_date(_value(root, "periodOfReport")),
        issuer_cik=(_value(issuer, "issuerCik") or None) if issuer is not None else None,
        issuer_name=_value(issuer, "issuerName") if issuer is not None else "",
        issuer_symbol=(
            _value(issuer, "issuerTradingSymbol") if issuer is not None else ""
        ),
        owners=owners,
        transactions=transactions,
        aff10b5_one=_flag(root, "aff10b5One"),
        mentions_10b5_1="10b5-1" in xml,
    )


# --- amendments ---------------------------------------------------------


def supersede(filings: Iterable[Form4]) -> list[Form4]:
    """Drop originals that a 4/A in the same batch restates.

    An amendment carries its own accession, so both survive a naive dedupe and
    the transaction is counted twice. Superseding keys on
    (issuer, owner set, period): a 4/A restates one filer's report for one
    period at one issuer, and that triple is what identifies it.

    An amendment with no original present is kept -- the original is simply
    outside the batch, and dropping the correction as well would lose the
    filing entirely.
    """
    def key(f: Form4) -> tuple:
        return (
            f.issuer_cik,
            tuple(sorted(o.cik or o.name for o in f.owners)),
            f.period_of_report,
        )

    amended = {key(f) for f in filings if f.is_amendment}
    return [f for f in filings if f.is_amendment or key(f) not in amended]


# --- reading a day ------------------------------------------------------


#: Both are Form 4s. EDGAR lists an amendment under its own form type, so a
#: filter of exactly "4" silently excludes every 4/A -- which is how a week's
#: distribution came back reporting zero amendments when there were 106.
FORM4_TYPES: Final[tuple[str, ...]] = ("4", "4/A")


def daily_index_paths(day: date, form_types: str | Iterable[str] = FORM4_TYPES,
                      client: httpx.Client | None = None) -> list[str]:
    """Archive paths for every filing of the given form types on one day.

    The bulk file, deliberately. Reading a week back through the current-events
    feed is impossible -- it holds only a few hundred filings -- and doing it
    per-CIK would be thousands of requests to answer a question one file
    already answers.
    """
    base = manifest.get("edgar", "daily_index").location
    quarter = (day.month - 1) // 3 + 1
    url = f"{base}/{day.year}/QTR{quarter}/form.{day:%Y%m%d}.idx"
    headers = {"User-Agent": user_agent(), "Accept-Encoding": "gzip, deflate"}
    owns = client is None
    client = client or httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=True)
    try:
        resp = client.get(url, headers=headers)
        if resp.status_code == 404:
            log.info("no daily index for %s (weekend or holiday)", day)
            return []
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise EdgarError(f"could not read the daily index for {day}: {exc}") from exc
    finally:
        if owns:
            client.close()

    wanted = (
        {form_types.upper()} if isinstance(form_types, str)
        else {f.upper() for f in form_types}
    )
    out: list[str] = []
    for line in resp.text.splitlines():
        parts = line.split()
        if not parts or parts[0].upper() not in wanted:
            continue
        path = parts[-1]
        if path.endswith(".txt"):
            out.append(path)
    return out


def fetch_documents(
    paths: Iterable[str],
    client: httpx.Client | None = None,
    pacer: Pacer | None = None,
) -> Iterator[tuple[str, str]]:
    """Yield (accession, submission text), paced under SEC's ceiling."""
    base = manifest.get("edgar", "archives").location
    headers = {"User-Agent": user_agent(), "Accept-Encoding": "gzip, deflate"}
    owns = client is None
    client = client or httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=True)
    pacer = pacer or Pacer()
    try:
        for path in paths:
            pacer.wait()
            try:
                resp = client.get(f"{base}/{path}", headers=headers)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                log.warning("skipping %s: %s", path, exc)
                continue
            yield normalize_accession(path) or path, resp.text
    finally:
        if owns:
            client.close()
