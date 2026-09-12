"""The ticker a company traded under *while it existed*, from its own filings.

**The problem this solves is the binding one.** Measured 2026-09-12: of 2,265
filers whose 10-K history has ended, **2,141 (94.3%) have no ticker at all** in
`company_tickers`, and only 85 have a ticker but no prices. Every question that
starts "what happened to the companies that were acquired" — takeout premiums,
forward returns, deal multiples keyed on a symbol — is blocked by the map, not by
the price history, by a factor of 25.

And no current-state source has it. `company_tickers.json` is a snapshot of what
trades *now*; SEC's submissions JSON returns an empty `tickers` array for a
delisted company, verified against Twitter, Activision, VMware and Seagen. The
`companyfacts` and `companyconcept` APIs carry only *numeric* concepts, so
`dei:TradingSymbol` is absent from both — it is a text fact.

What does have it is the filing itself. `dei:TradingSymbol` is tagged on the cover
page of every filing since the cover-page XBRL mandate, and it returns TWTR, ATVI,
VMW and SGEN for those four. Free, deterministic, and keyed on the CIK that SEC
assigns and never reuses.

**The range understates, in both directions, and that is stated rather than
hidden.** A row's bounds are the earliest and latest *filing date* on which the
symbol was observed — not when it started or stopped trading. A company traded
under its symbol before the first filing that mentions it and usually after the
last. Same shape as Form 5500's `PLAN_EFF_DATE`, which gives the oldest plan a
sponsor still files rather than the company's age: the error runs one way, so the
figure is usable as a join bound and unusable as a reported fact. It renders as
"seen between", never "traded from".

**Both directions are many-to-one over time**, which is why the key carries both
the CIK and the symbol. A CIK changes ticker on a rename or a share-class change; a
ticker changes CIK on recycling — 356 active symbols carry two different companies
inside a ten-year pull, and SGEN is the case in point, since a different issuer took
the symbol after Seagen was acquired in 2023. A recovered ticker that overwrote a
recycled one would leave a map that looks complete and silently resolves the wrong
company, which is where we started.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Final, Iterable

from marketradar import manifest
from marketradar.freshness import assert_fresh

log = logging.getLogger(__name__)

DATASET: Final[str] = "company_ticker_history"
SOURCE: Final[str] = "dei:TradingSymbol"

#: Rows per round trip. 500, the same as `upsert_corporate_actions`, for the same
#: reason: one round trip per row does not finish at this scale.
BATCH: Final[int] = 500

#: Forms whose cover page carries the symbol. Annual and quarterly reports and
#: 8-Ks all tag it; the periodic reports are preferred because they are evenly
#: spaced, which is what makes a range out of points.
COVER_FORMS: Final[tuple[str, ...]] = ("10-K", "10-Q", "8-K", "20-F", "40-F")

#: Filings sampled per CIK. Three is the shape of the answer rather than a budget:
#: the earliest and latest bound the range, and a middle one catches a symbol that
#: changed inside the window. More would tighten the boundary and not change which
#: symbols are found — and the boundary is already declared as a bound rather than
#: a date, so precision there buys nothing.
SAMPLE_PER_CIK: Final[int] = 3

#: A symbol, as the cover page writes it. 1–9 characters, uppercase, with the dot
#: and hyphen that share classes use. Deliberately strict: this column is joined
#: against price data, where a malformed symbol matches nothing rather than
#: erroring.
SYMBOL: Final[re.Pattern[str]] = re.compile(r"^[A-Z][A-Z0-9.\-]{0,8}$")

#: The cover-page tag, in the two shapes filers' HTML uses. Matched against the
#: raw document rather than the visible text, because the symbol is an XBRL fact
#: in an attribute-bearing span and the text extraction drops it — measured: the
#: visible text of all four test filings did *not* contain the symbol while the
#: tag did.
TAG: Final[re.Pattern[str]] = re.compile(
    r"""(?:name|id)=["'][^"']*TradingSymbol[^"']*["'][^>]*>\s*([A-Za-z0-9.\-]{1,9})"""
    r"""|TradingSymbol[^>]{0,400}?>\s*([A-Za-z0-9.\-]{1,9})\s*<""",
    re.IGNORECASE | re.DOTALL,
)

#: Tokens a cover page sometimes puts where the symbol goes when there is none.
#: Rejected explicitly: a company with no listed symbol is a real answer and must
#: not become the string "N/A" in a column that is joined against price data.
NOT_A_SYMBOL: Final[frozenset[str]] = frozenset({
    "N/A", "NA", "NONE", "N.A.", "NOTAPPLICABLE", "TBD",
})


class SymbolError(RuntimeError):
    """A symbol sweep could not be completed."""


@dataclass(frozen=True, slots=True)
class Observation:
    """One symbol, seen on one filing."""

    cik: str
    ticker: str
    filed: date
    form: str
    accession: str
    exchange: str | None = None


@dataclass(frozen=True, slots=True)
class SymbolRange:
    """One ``(cik, ticker)`` pair and the filings that evidence it."""

    cik: str
    ticker: str
    first_seen: date
    last_seen: date
    filings: int
    exchange: str | None = None

    @property
    def is_point(self) -> bool:
        """One observation. A point, not a range, and a consumer should know."""
        return self.filings == 1 or self.first_seen == self.last_seen

    def describe(self) -> str:
        """How this renders. **Never "traded from".**"""
        if self.is_point:
            return f"{self.ticker} seen on {self.first_seen}"
        return f"{self.ticker} seen between {self.first_seen} and {self.last_seen}"


@dataclass(frozen=True, slots=True)
class SweepReport:
    """What a sweep found, and what it could not."""

    ciks: int
    requests: int
    observations: int
    ranges: list[SymbolRange] = field(default_factory=list)
    #: CIKs whose filings carried no parsable symbol. Counted, not dropped: a
    #: filer with no cover-page tag is a gap in the source, and the share of them
    #: is how the map's coverage is judged.
    no_symbol: list[str] = field(default_factory=list)
    #: CIKs that errored. Separate from no_symbol, because one is the data and the
    #: other is this run.
    failed: list[tuple[str, str]] = field(default_factory=list)

    def lines(self) -> list[str]:
        found = len({r.cik for r in self.ranges})
        total = self.ciks or 1
        out = [
            f"trading symbols: {self.ciks:,} CIKs swept in {self.requests:,} "
            "requests",
            f"  resolved        {found:>6,}  {found / total * 100:5.1f}%",
            f"  no symbol       {len(self.no_symbol):>6,}  "
            f"{len(self.no_symbol) / total * 100:5.1f}%  the cover page carried "
            "no parsable tag",
            f"  failed          {len(self.failed):>6,}  a fact about this run, "
            "not about the filer",
            f"  (cik, ticker)   {len(self.ranges):>6,}  rows -- more than CIKs "
            "where a company changed symbol",
        ]
        points = sum(1 for r in self.ranges if r.is_point)
        if points:
            out.append(f"  of those, {points:,} rest on a single filing, so their "
                       "range is a point rather than a span")
        multi = {}
        for r in self.ranges:
            multi.setdefault(r.ticker, set()).add(r.cik)
        recycled = {t: c for t, c in multi.items() if len(c) > 1}
        if recycled:
            out.append(f"  {len(recycled):,} symbol(s) map to more than one CIK -- "
                       "recycling, which is why every lookup carries a date:")
            for ticker, ciks in sorted(recycled.items())[:6]:
                out.append(f"    {ticker:<8} {', '.join(sorted(ciks))}")
        return out


def symbol_from(html: str) -> str | None:
    """The cover-page trading symbol, or None.

    Parsed from the raw document rather than the visible text. Measured on four
    filings from companies known to have been acquired: the *visible text* of all
    four lacked the symbol while the tag carried it, because the fact lives in an
    XBRL span the text extraction drops.
    """
    for match in TAG.finditer(html):
        raw = (match.group(1) or match.group(2) or "").strip().upper()
        if not raw or raw in NOT_A_SYMBOL:
            continue
        if SYMBOL.match(raw):
            return raw
    return None


def _sample(filings: list[dict[str, Any]], per_cik: int) -> list[dict[str, Any]]:
    """Earliest, latest, and a middle filing.

    Not the newest N. A range needs its ends, and a symbol that changed inside the
    window is only visible if something between them is read — which is the whole
    reason this samples rather than taking the most recent filings.
    """
    if len(filings) <= per_cik:
        return filings
    ordered = sorted(filings, key=lambda f: f["filed"])
    picks = [ordered[0], ordered[-1]]
    if per_cik > 2:
        step = max(1, len(ordered) // (per_cik - 1))
        for i in range(step, len(ordered) - 1, step):
            if len(picks) >= per_cik:
                break
            picks.append(ordered[i])
    return picks


def cover_filings(cik: str, *, client: Any, pacer: Any = None,
                  forms: Iterable[str] = COVER_FORMS) -> list[dict[str, Any]]:
    """Every filing for one CIK whose cover page should carry a symbol."""
    from marketradar.signals.edgar_rss import user_agent

    headers = {"User-Agent": user_agent(), "Accept-Encoding": "gzip, deflate"}
    if pacer is not None:
        pacer.wait()
    # Resolved through the manifest, never literal. Both endpoints are already
    # declared -- `sec_submissions/company` and `edgar/archives` -- so this module
    # holds no URL of its own, which is the whole point of that file.
    endpoint = manifest.get("sec_submissions", "company").location
    resp = client.get(endpoint.format(cik=f"{int(cik):010d}"), headers=headers)
    resp.raise_for_status()
    recent = (resp.json().get("filings") or {}).get("recent") or {}
    wanted = set(forms)
    out: list[dict[str, Any]] = []
    for i, form in enumerate(recent.get("form") or []):
        if form not in wanted:
            continue
        doc = (recent.get("primaryDocument") or [None] * (i + 1))[i]
        filed = (recent.get("filingDate") or [None] * (i + 1))[i]
        acc = (recent.get("accessionNumber") or [None] * (i + 1))[i]
        if not (doc and filed and acc):
            continue
        out.append({"form": form, "filed": filed, "accession": acc,
                    "document": doc})
    return out


def fetch(
    ciks: Iterable[str],
    *,
    client: Any,
    pacer: Any = None,
    per_cik: int = SAMPLE_PER_CIK,
) -> SweepReport:
    """Recover symbols for a list of CIKs. ``(1 + per_cik)`` requests each.

    Chunked and resumable is the caller's job here: this returns what it found and
    raises only on a programming error, so a sweep over thousands of CIKs can
    checkpoint around it.
    """
    observations: list[Observation] = []
    no_symbol: list[str] = []
    failed: list[tuple[str, str]] = []
    requests = 0
    asked = 0

    from marketradar.signals.edgar_rss import user_agent

    headers = {"User-Agent": user_agent(), "Accept-Encoding": "gzip, deflate"}
    for cik in ciks:
        asked += 1
        padded = str(cik).strip().lstrip("0").rjust(10, "0")
        try:
            filings = cover_filings(cik, client=client, pacer=pacer)
            requests += 1
        except Exception as exc:                       # network, 403, malformed
            failed.append((padded, f"submissions: {str(exc)[:80]}"))
            continue
        if not filings:
            no_symbol.append(padded)
            continue

        found = 0
        archives = manifest.get("edgar", "archives").location
        for filing in _sample(filings, per_cik):
            url = (f"{archives}/edgar/data/{int(cik)}/"
                   f"{filing['accession'].replace('-', '')}/{filing['document']}")
            try:
                if pacer is not None:
                    pacer.wait()
                doc = client.get(url, headers=headers)
                requests += 1
                doc.raise_for_status()
            except Exception as exc:
                failed.append((padded, f"{filing['accession']}: {str(exc)[:60]}"))
                continue
            ticker = symbol_from(doc.text)
            if ticker is None:
                continue
            observations.append(Observation(
                cik=padded, ticker=ticker,
                filed=datetime.strptime(filing["filed"], "%Y-%m-%d").date(),
                form=filing["form"], accession=filing["accession"]))
            found += 1
        if not found:
            no_symbol.append(padded)

    return SweepReport(
        # What was asked for, not what came back. A denominator that shrank to the
        # successes would make every coverage figure read 100%.
        ciks=asked,
        requests=requests, observations=len(observations),
        ranges=collapse(observations), no_symbol=no_symbol, failed=failed)


def collapse(observations: Iterable[Observation]) -> list[SymbolRange]:
    """Observations to one row per ``(cik, ticker)``, with its bounds.

    Grouped on both, never on the CIK alone. A CIK that changed symbol produces two
    rows with their own bounds, which is the point: collapsing to "the" ticker for a
    CIK would pick one arbitrarily, and which one it picked would decide whether a
    price join found anything.
    """
    by_pair: dict[tuple[str, str], list[Observation]] = {}
    for obs in observations:
        by_pair.setdefault((obs.cik, obs.ticker), []).append(obs)
    out: list[SymbolRange] = []
    for (cik, ticker), group in sorted(by_pair.items()):
        dates = sorted(o.filed for o in group)
        out.append(SymbolRange(
            cik=cik, ticker=ticker, first_seen=dates[0], last_seen=dates[-1],
            filings=len(group),
            exchange=next((o.exchange for o in group if o.exchange), None)))
    return out


def load(report: SweepReport, con: Any, *, alias: str = "pg",
         min_rows: int = 1) -> Any:
    """Upsert the recovered ranges and assert the result is real.

    Upsert widens rather than replaces: a later sweep that observes an earlier
    filing must extend the range backwards, not reset it. `least`/`greatest` on the
    existing bounds do that, and the filing count accumulates — which is what makes
    a second pass over more filings an improvement rather than an overwrite.
    """
    # **Sent as raw SQL through `postgres_execute`, not as a DuckDB insert.**
    #
    # DuckDB's Postgres extension implements `insert ... values` as a COPY, and a
    # COPY does not apply column defaults -- so `id bigserial` arrives NULL and the
    # not-null constraint rejects every row. It fails loudly, which is the good
    # case, but it fails on the first batch of any table with a serial key. Raw SQL
    # goes to Postgres intact and the default applies.
    #
    # Batched because a 3,744-CIK sweep is thousands of rows and one round trip per
    # row does not finish -- the same lesson `upsert_corporate_actions` learned,
    # where sequential round trips reported success having written a tenth of what
    # they attempted.
    def lit(value: Any) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    written = 0
    for start in range(0, len(report.ranges), BATCH):
        batch = report.ranges[start:start + BATCH]
        values = ", ".join(
            f"({lit(r.cik)}, {lit(r.ticker)}, "
            f"{lit(r.exchange) if r.exchange else 'NULL'}, "
            f"date {lit(r.first_seen)}, date {lit(r.last_seen)}, "
            f"{int(r.filings)}, {lit(SOURCE)})"
            for r in batch
        )
        con.execute(
            f"CALL postgres_execute('{alias}', ?)",
            [
                "insert into company_ticker_history "
                "(cik, ticker, exchange, first_seen, last_seen, filings, source) "
                f"values {values} "
                "on conflict (cik, ticker) do update set "
                "first_seen = least(company_ticker_history.first_seen, "
                "                   excluded.first_seen), "
                "last_seen  = greatest(company_ticker_history.last_seen, "
                "                      excluded.last_seen), "
                "filings    = company_ticker_history.filings "
                "             + excluded.filings, "
                "exchange   = coalesce(excluded.exchange, "
                "                      company_ticker_history.exchange), "
                "updated_at = now()"
            ],
        )
        written += len(batch)
    rel = con.sql(
        f"select cik, ticker, first_seen, last_seen, filings "
        f"from {alias}.company_ticker_history")
    # Explicit, and the last statement that matters, per the hard rule.
    observed = assert_fresh(
        DATASET, rel, partition="all", min_rows=min_rows,
        # A symbol's bounds are filing dates, and the newest filing in a sweep of
        # *delisted* companies is years old by construction. Staleness here would
        # measure how long ago those companies were acquired.
        date_column=None,
        expect_cols=("cik", "ticker", "first_seen", "last_seen", "filings"),
    )
    manifest.record_stats(observed, con=con)
    return observed
