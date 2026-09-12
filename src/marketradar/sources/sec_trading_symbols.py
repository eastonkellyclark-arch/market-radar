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

#: How a row's symbol was read, as it is stored. **Not a module constant any more,
#: and that was a real defect**: `load` wrote `SOURCE` for every row, so every
#: symbol recovered from prose was labelled as a cover-page tag. The two are not
#: interchangeable evidence -- the tag is unambiguous and the prose is a regex
#: measured at 7 of 8 -- and the whole reason the route is computed is so a consumer
#: can tell them apart. A figure's provenance has to reach the row or it does not
#: exist; the same rule as the provider and model on every LLM-produced row.
SOURCE: Final[dict[str, str]] = {
    "tag": "dei:TradingSymbol",
    "prose": "cover-page prose",
    "prose+tag": "dei:TradingSymbol and cover-page prose",
}

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
#: Note what this does *not* admit: a slash. The tag capture deliberately takes
#: ``/`` so a sentinel arrives intact, and this is what then refuses it.
SYMBOL: Final[re.Pattern[str]] = re.compile(r"^[A-Z][A-Z0-9.\-]{0,8}$")

#: The cover-page tag, in the two shapes filers' HTML uses. Matched against the
#: raw document rather than the visible text, because the symbol is an XBRL fact
#: in an attribute-bearing span and the text extraction drops it — measured: the
#: visible text of all four test filings did *not* contain the symbol while the
#: tag did.
#: The capture class includes ``/`` so that ``N/A`` is seen *whole* and rejected.
#: Without it the class stopped at the slash and the value came back as ``N`` --
#: which passes every validity check, because N is a real NYSE ticker. A sentinel
#: truncated into a valid symbol is the worst shape available here: it would have
#: mapped a company with no listing to somebody else's stock.
TAG: Final[re.Pattern[str]] = re.compile(
    r"""(?:name|id)=["'][^"']*TradingSymbol[^"']*["'][^>]*>\s*([A-Za-z0-9./\-]{1,12})"""
    r"""|TradingSymbol[^>]{0,400}?>\s*([A-Za-z0-9./\-]{1,12})\s*<""",
    re.IGNORECASE | re.DOTALL,
)

#: A symbol inside the pattern, where a dot counts **only when a share-class
#: letter follows it**.
#:
#: That lookahead is the whole trick. A first version used ``[A-Z.\-]*`` and scored
#: **1 of 6** because the boilerplate ends the sentence right after the quoted
#: symbol -- "under the symbol 'WFM.'" -- so the class character swallowed the full
#: stop and every answer came back one character long. With the lookahead it is
#: 7 of 8. ``BRK.A`` still parses; ``WFM.`` does not.
_SYM: Final[str] = r"([A-Z](?:[A-Z0-9\-]|\.(?=[A-Z]))*)"

#: The symbol as **prose**, for filings that predate the cover-page XBRL mandate.
#:
#: **Why this exists.** The `dei:TradingSymbol` tag is post-2019 only, and the
#: recovery rate shows the cliff exactly: 1 row in 2017 against 40 in 2019 and 62
#: in 2020. A decade of takeouts sits on the wrong side of it, and that is the
#: population deal multiples and the filer universe care about.
#:
#: Two other routes were tried and rejected first, measured on LinkedIn and Whole
#: Foods: **Form 25-NSE and the contemporaneous 8-K Item 3.01 do not carry the
#: symbol at all**, in raw HTML or visible text. What does carry it is the 10-K's
#: own listing sentence, which is legal boilerplate -- "listed on the New York
#: Stock Exchange under the symbol 'LNKD'".
#:
#: Deterministic, and that matters: the extraction half of the proxy reader was
#: measured at 62% and declined, so a second field needing a model would be
#: declined on the same evidence. These are regexes. 7 of 8 on a hand-picked set
#: spanning 2016-2023 -- LinkedIn, Whole Foods, Twitter, Monsanto, Rockwell
#: Collins, Activision, VMware; the miss is Time Warner, which words it
#: differently and is a `no_symbol` rather than a wrong answer.
PROSE: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"under\s+the\s+(?:ticker\s+|trading\s+)?symbols?\s*"
               r"[:“‘\"']?\s*" + _SYM),
    re.compile(r"(?:ticker|trading)\s+symbol\s*[:“‘\"']?\s*" + _SYM),
    re.compile(r"symbol\s*[:“‘\"']\s*" + _SYM),
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
    #: ``tag`` or ``prose``. Carried because they are different evidence: the XBRL
    #: tag is unambiguous, the prose is a regex over boilerplate that scores 7 of 8.
    route: str = "tag"


@dataclass(frozen=True, slots=True)
class SymbolRange:
    """One ``(cik, ticker)`` pair and the filings that evidence it."""

    cik: str
    ticker: str
    first_seen: date
    last_seen: date
    filings: int
    exchange: str | None = None
    #: Every route that produced this symbol. A range evidenced by both a tag and
    #: the prose is stronger than one resting on either.
    routes: tuple[str, ...] = ("tag",)

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


def symbol_from(html: str) -> tuple[str, str] | None:
    """``(symbol, how it was found)``, or None.

    **The tag first, then the prose, and the route is returned.** The XBRL tag is
    unambiguous and exists only after the 2019 cover-page mandate; the prose
    sentence is boilerplate and spans the whole decade. Trying the tag first means
    a post-2019 filing never depends on a regex over prose, and returning which
    route answered means a consumer can tell a tagged symbol from a parsed one --
    the same reason the resolved XBRL tag rides on every fundamentals row.

    The tag is matched against the **raw** document and the prose against the
    **visible text**, which is not a detail: measured on four known-acquired
    companies, the visible text lacked the symbol entirely while the tag carried
    it, because the fact lives in an XBRL span the text extraction drops. The prose
    is the other way round -- it is a sentence, and the raw HTML interleaves it
    with markup.
    """
    for match in TAG.finditer(html):
        raw = (match.group(1) or match.group(2) or "").strip().upper()
        if not raw or raw in NOT_A_SYMBOL:
            continue
        if SYMBOL.match(raw):
            return raw, "tag"

    from marketradar.signals.deals import visible

    text = visible(html)
    for pattern in PROSE:
        match = pattern.search(text)
        if not match:
            continue
        raw = match.group(1).strip().upper()
        if raw and raw not in NOT_A_SYMBOL and SYMBOL.match(raw):
            return raw, "prose"
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
            got = symbol_from(doc.text)
            if got is None:
                continue
            ticker, route = got
            observations.append(Observation(
                cik=padded, ticker=ticker, route=route,
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
            exchange=next((o.exchange for o in group if o.exchange), None),
            routes=tuple(sorted({o.route for o in group}))))
    return out


def source_of(rng: "SymbolRange") -> str:
    """The stored label for a range's route(s).

    Raises on a route it does not know rather than falling back to the tag label.
    A silent default here is exactly what the old module constant was, and it
    mislabelled every prose row -- so an unrecognised route is a programming error
    that should stop the load, not a row that reads plausibly and is wrong.
    """
    key = "+".join(sorted(set(rng.routes)))
    if key not in SOURCE:
        raise SymbolError(
            f"no stored label for route(s) {key!r}. Add it to SOURCE rather than "
            "letting the row inherit a label it did not earn."
        )
    return SOURCE[key]


def _lit_both() -> str:
    """The both-routes label as a SQL literal, for the conflict clause."""
    return "'" + SOURCE["prose+tag"].replace("'", "''") + "'"


def row_values(rng: "SymbolRange") -> str:
    """One range as the SQL tuple that reaches the table.

    A named function rather than an inline comprehension so a test can read what is
    actually written. That matters here specifically: the route was computed and
    then silently dropped at this exact point, and the defect was invisible in every
    test because nothing could see the statement.
    """
    def lit(value: Any) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    return (f"({lit(rng.cik)}, {lit(rng.ticker)}, "
            f"{lit(rng.exchange) if rng.exchange else 'NULL'}, "
            f"date {lit(rng.first_seen)}, date {lit(rng.last_seen)}, "
            f"{int(rng.filings)}, {lit(source_of(rng))})")


def upsert_statement(values: str) -> str:
    """The upsert, as one string a test can read.

    Built by a named function rather than inline, because the clause that was
    missing from it could not be seen from outside. Asserting on the module's own
    source text would pass on a commented-out clause; asserting on this cannot.

    Every column the upsert *widens* rather than replaces is here for the same
    reason: a second pass that observes an earlier filing must extend the range
    backwards, add to the filing count, and add to the evidence -- never reset any
    of the three.
    """
    return (
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
        # Widened, like the bounds. A row first seen by the tag and later confirmed
        # in prose is evidenced by both, and saying so is the point of storing the
        # route at all.
        "source     = case when company_ticker_history.source = excluded.source "
        "                  then excluded.source "
        f"                  else {_lit_both()} end, "
        "updated_at = now()"
    )


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
    written = 0
    for start in range(0, len(report.ranges), BATCH):
        batch = report.ranges[start:start + BATCH]
        values = ", ".join(row_values(r) for r in batch)
        con.execute(f"CALL postgres_execute('{alias}', ?)",
                    [upsert_statement(values)])
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
