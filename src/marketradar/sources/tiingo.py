"""Tiingo — source of record for prices and corporate actions.

Stores **raw** OHLCV. Tiingo returns both raw and adjusted columns in the
same payload, and the adjusted ones are deliberately discarded: adjusted
history is retroactively rewritten by every split, so storing it would make
year-partitioned Parquet drift from its source the first time a sub-$1 name
does a reverse split. ``splitFactor`` and ``divCash`` go to the
``corporate_actions`` table instead, and adjustment is a query-time join.

The sweep is chunked and checkpointed because it has to be. ~12,000 tickers
against a 10,000 requests/hour cap means a full sweep spans more than one
rate-limit window and takes over an hour. Resume is the default; restarting
is an explicit flag.

No URL appears in this file. Endpoints resolve through
``manifest.get('tiingo_api', 'base')``, per the hard rule in CLAUDE.md.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import random
import re
import time
import zipfile
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Iterable

import duckdb
import httpx

from marketradar import manifest, storage
from marketradar.checkpoint import Checkpoint, chunked
from marketradar.freshness import StaleDataError, assert_fresh, utc_today

log = logging.getLogger(__name__)

ENV_TOKEN: Final[str] = "MR_TIINGO_API_KEY"

DATASET: Final[str] = "prices_eod_raw"

#: Published Power limits: 10,000/hour and 100,000/day. Pacing below the
#: hourly figure leaves room for retries and for anything else that runs.
DEFAULT_RATE_PER_HOUR: Final[int] = 9_000
DEFAULT_CHUNK_SIZE: Final[int] = 100
MAX_RETRIES: Final[int] = 5
REQUEST_TIMEOUT: Final[float] = 30.0

#: US listed exchanges.
#:
#: Note AMEX and NYSE MKT are the same venue, NYSE American: Tiingo splits it
#: across both codes (298 + 34 = 332 active). Both are kept; a count of 34
#: under "NYSE MKT" alone is a labelling artefact, not a dropped exchange.
#:
#: OTC is deliberately excluded, and that exclusion is now a *decision* rather
#: than an unknown. Tiingo carries 17,618 active OTC symbols (PINK 16,301,
#: OTCMKTS 857, OTCQB 211, OTCGREY 190, OTCD 31, OTCQX 15, OTCCE 10, OTCBB 3).
#: Including them would take the universe from ~14,000 to ~31,700 and roughly
#: double sweep time, so it is a cost/benefit call for the sub-$1 band rather
#: than a coverage gap. See docs/build-spec.md §0.
LISTED_EXCHANGES: Final[frozenset[str]] = frozenset(
    {"NYSE", "NASDAQ", "NYSE MKT", "NYSE ARCA", "AMEX", "BATS"}
)

#: Present in the source file and available if the OTC band is switched on.
#:
#: Deferred by decision on 2026-09-07, not by oversight: hold until the listed
#: sweep has run clean for one week, then fold these into LISTED_EXCHANGES.
#: Doing so takes the universe from ~14,100 to ~31,700 and the sweep from
#: ~95 to ~211 minutes, so raise the workflow's timeout-minutes at the same
#: time. See docs/build-spec.md §0.
OTC_EXCHANGES: Final[frozenset[str]] = frozenset(
    {"PINK", "OTCMKTS", "OTCQB", "OTCQX", "OTCGREY", "OTCD", "OTCCE", "OTCBB"}
)


class TiingoError(RuntimeError):
    """Tiingo could not be reached, or answered with something unusable."""


@dataclass(frozen=True, slots=True)
class TickerMeta:
    ticker: str
    exchange: str
    asset_type: str
    start_date: date | None
    end_date: date | None


@dataclass
class SweepResult:
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    rows: int = 0
    actions: int = 0
    retries: int = 0
    rate_limited: int = 0
    resumed_chunks: int = 0
    #: Tickers already done before this process started. Excluded from the
    #: rate calculation — otherwise a resumed run looks impossibly fast,
    #: because it credits itself with work a previous process did.
    resumed_attempted: int = 0
    elapsed: float = 0.0
    failures: list[tuple[str, str]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.failures is None:
            self.failures = []


# --------------------------------------------------------------------------
# rate limiting
# --------------------------------------------------------------------------


class Pacer:
    """Spaces requests to stay under a per-hour budget.

    A fixed sleep would either waste the quota or blow through it. This
    holds a target interval and only sleeps for the remainder, so time
    already spent waiting on the network counts toward the gap.
    """

    def __init__(self, per_hour: int = DEFAULT_RATE_PER_HOUR) -> None:
        if per_hour < 1:
            raise ValueError("per_hour must be >= 1")
        self.interval = 3600.0 / per_hour
        self._last = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        gap = self.interval - (now - self._last)
        if gap > 0:
            time.sleep(gap)
        self._last = time.monotonic()


def _backoff_seconds(attempt: int, retry_after: str | None) -> float:
    """Honour Retry-After when the server sends one; otherwise exponential.

    Jitter matters more than it looks: without it every stalled request
    retries at the same instant and the burst re-triggers the limit.
    """
    if retry_after:
        try:
            return max(1.0, min(float(retry_after), 300.0))
        except ValueError:
            pass
    return min(2.0**attempt, 60.0) + random.uniform(0, 1.0)


# --------------------------------------------------------------------------
# fetch
# --------------------------------------------------------------------------


def _token() -> str:
    token = os.environ.get(ENV_TOKEN, "")
    if not token or "dummy" in token.lower():
        raise TiingoError(
            f"{ENV_TOKEN} is not set (or is still a placeholder). "
            "Tiingo is the source of record; it cannot be skipped."
        )
    return token


def _client() -> httpx.Client:
    base = manifest.get("tiingo_api", "base").location
    return httpx.Client(
        base_url=base,
        timeout=REQUEST_TIMEOUT,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Token {_token()}",
        },
    )


#: A symbol whose last bar is older than this is treated as delisted.
#:
#: 30 days, not 7. An SEC trading suspension runs 10 business days — about
#: 14 calendar days — so a 7-day window drops a suspended name mid-suspension.
#: The filter is recomputed from a freshly downloaded universe on every run,
#: so a resumed ticker returns on its own; the wider window simply means it
#: never left. Cost measured against the real file: +88 symbols, +0.6%, about
#: 36 seconds of extra sweep. Cheap insurance against a hole in exactly the
#: names most likely to be interesting.
ACTIVE_WITHIN_DAYS: Final[int] = 30

#: Exchange test symbols. Every US venue publishes a handful of live-looking
#: rows that carry quotes and volume but are not securities, and they reach
#: the screens looking exactly like real moves. ZVZZT topped the liquid $10+
#: loser list at -86.75% on a fabricated $7.6M average dollar volume before
#: this was widened past NYSE's family.
#:
#: * ``ATEST``   -- NYSE, with ``ATEST-A`` .. ``ATEST-Z`` class variants
#: * ``Z?ZZT``   -- NASDAQ: ZAZZT, ZBZZT, ZCZZT, ZJZZT, ZVZZT, ZWZZT, ZXZZT
#: * ``ZTEST``, ``TEST`` -- Cboe/BATS
#:
#: Excluded at the universe boundary rather than in the screens: these are not
#: securities, so storing their bars is not faithfulness to the feed, and
#: dropping them here also keeps them out of the request budget and out of
#: every downstream consumer at once.
#:
#: An optional ``-``/``.`` suffix is allowed so class variants match, but a
#: bare prefix match is not, so a real ticker merely *starting* with these
#: letters survives.
#: The exchanges each run their own test-symbol family, and the first pass
#: only knew NASDAQ's. The action detector found the rest by their prices:
#: ZBZX contributed 26 "unexplained moves" and PTEST-Z printed a 0.05 -> 25.00
#: jump, because a test symbol's quote is arbitrary by design. Listed
#: explicitly rather than by substring: matching "TEST" anywhere would
#: silently drop a real ticker, and a dropped ticker is invisible.
TEST_SYMBOL: Final[re.Pattern[str]] = re.compile(
    r"^(?:"
    # NASDAQ and NYSE: an optional single-letter prefix on TEST.
    r"[A-Z]?TEST"
    # NASDAQ's Z_ZZT family: ZAZZT, ZBZZT, ZVZZT, ZXZZT ...
    r"|Z[A-Z]ZZT"
    # Cboe BZX/BYX/EDGA/EDGX.
    r"|ZBZX|ZBZY|ZTST|ZEXIT|ZIEXT"
    # IEX.
    r"|ZIEXT|IEXTEST"
    r")(?:[-.].*)?$"
)

#: The same families as a RE2 pattern, for the publish-side filter. DuckDB
#: cannot call a Python regex, and keeping the two spellings adjacent is the
#: only thing that will keep them in step -- a test asserts they agree on
#: every symbol in :data:`TEST_SYMBOL_EXAMPLES`.
TEST_SYMBOL_SQL: Final[str] = (
    r"^(?:[A-Z]?TEST|Z[A-Z]ZZT|ZBZX|ZBZY|ZTST|ZEXIT|ZIEXT|IEXTEST)([-.].*)?$"
)

#: Symbols the two spellings above must agree on. Every one of the excluded
#: entries was observed in our own price history or in the action audit.
TEST_SYMBOL_EXAMPLES: Final[tuple[tuple[str, bool], ...]] = (
    ("TEST", True), ("ATEST", True), ("ZTEST", True), ("PTEST-Z", True),
    ("NTEST", True), ("CTEST", True), ("ATEST.A", True),
    ("ZVZZT", True), ("ZXZZT", True), ("ZBZX", True), ("ZBZY", True),
    ("ZTST", True), ("ZEXIT", True), ("ZIEXT", True), ("IEXTEST", True),
    # Real tickers that must survive. TESS and TESSCO start with the letters
    # but are not test symbols; a substring match would have eaten both.
    ("TSLA", False), ("TESS", False), ("TESSCO", False), ("ZTS", False),
    ("ZBRA", False), ("TEAM", False), ("AZTA", False), ("ZION", False),
    ("ZTO", False), ("ZS", False), ("PTC", False), ("TXT", False),
)


def include_row(row: dict[str, Any], cutoff: date | None) -> bool:
    """Whether one universe row belongs in the sweep.

    Split out from :func:`fetch_universe` so the filtering rules can be
    tested without downloading anything.
    """
    ticker = (row.get("ticker") or "").strip().upper()
    if not ticker or TEST_SYMBOL.match(ticker):
        return False
    if (row.get("exchange") or "").strip().upper() not in LISTED_EXCHANGES:
        return False
    if (row.get("assetType") or "").strip().lower() not in ("stock", "etf"):
        return False
    if cutoff is not None:
        end = _parse_date(row.get("endDate"))
        if end is None or end < cutoff:
            return False
    return True


def download_universe_rows() -> list[dict[str, Any]]:
    """Every row of the supported-ticker file, unfiltered.

    Split out from :func:`fetch_universe` because reconciliation has to tell
    "Tiingo does not carry this symbol" apart from "our own listed-only filter
    drops it", and the filtered universe erases that distinction. Nothing that
    sweeps prices should use this — it includes OTC, delisted, funds, and
    every other row in the file.
    """
    url = manifest.get("tiingo_supported_tickers", "all").location
    try:
        resp = httpx.get(url, timeout=120.0, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise TiingoError(f"Could not download the ticker universe: {exc}") from exc

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        name = zf.namelist()[0]
        text = zf.read(name).decode("utf-8", errors="replace")

    return list(csv.DictReader(io.StringIO(text)))


def fetch_universe(
    active_within_days: int | None = ACTIVE_WITHIN_DAYS,
    today: date | None = None,
    rows: list[dict[str, Any]] | None = None,
) -> list[TickerMeta]:
    """Download the supported-ticker list.

    A bulk zip, free, and outside the request budget — so the universe never
    costs quota.

    Filtered to US listed common stock and ETFs. By default also filtered to
    *currently trading* symbols: Tiingo lists 24,357 US listed stock/ETF
    symbols, of which only ~14,000 have traded in the last week. The rest are
    delisted. Sweeping them nightly would spend 42% of a finite request
    budget re-confirming that dead tickers are still dead.

    Pass ``active_within_days=None`` to get the full historical universe,
    which is what a backfill wants. Pass ``rows`` to filter an already
    downloaded file rather than fetching it again.
    """
    rows = download_universe_rows() if rows is None else rows

    cutoff = None
    if active_within_days is not None:
        # Trading date: the cutoff is compared against each row's endDate,
        # which is a session date, so anchoring it on the UTC clock shifts the
        # whole "currently trading" boundary by a day every night.
        from marketradar.clock import market_today

        cutoff = (today or market_today()) - timedelta(days=active_within_days)

    out: list[TickerMeta] = []
    for row in rows:
        if not include_row(row, cutoff):
            continue
        out.append(
            TickerMeta(
                ticker=(row.get("ticker") or "").strip().upper(),
                exchange=(row.get("exchange") or "").strip().upper(),
                asset_type=(row.get("assetType") or "").strip().lower(),
                start_date=_parse_date(row.get("startDate")),
                end_date=_parse_date(row.get("endDate")),
            )
        )
    return out


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def fetch_prices(
    client: httpx.Client,
    ticker: str,
    start: date,
    end: date,
    result: SweepResult,
    pacer: Pacer,
) -> list[dict[str, Any]]:
    """One ticker, one date range, with real backoff on 429."""
    path = f"/tiingo/daily/{ticker}/prices"
    params = {"startDate": start.isoformat(), "endDate": end.isoformat()}

    for attempt in range(MAX_RETRIES):
        pacer.wait()
        try:
            resp = client.get(path, params=params)
        except httpx.HTTPError as exc:
            if attempt == MAX_RETRIES - 1:
                raise TiingoError(f"{ticker}: {exc}") from exc
            result.retries += 1
            time.sleep(_backoff_seconds(attempt, None))
            continue

        if resp.status_code == 429:
            result.rate_limited += 1
            result.retries += 1
            delay = _backoff_seconds(attempt, resp.headers.get("Retry-After"))
            log.warning("429 on %s, backing off %.1fs", ticker, delay)
            time.sleep(delay)
            continue

        if resp.status_code == 404:
            return []  # Tiingo knows the symbol but has no data for the range

        if resp.status_code >= 500:
            if attempt == MAX_RETRIES - 1:
                raise TiingoError(f"{ticker}: HTTP {resp.status_code}")
            result.retries += 1
            time.sleep(_backoff_seconds(attempt, resp.headers.get("Retry-After")))
            continue

        if resp.status_code != 200:
            raise TiingoError(f"{ticker}: HTTP {resp.status_code} {resp.text[:120]}")

        try:
            return resp.json() or []
        except ValueError as exc:
            raise TiingoError(f"{ticker}: response was not JSON") from exc

    raise TiingoError(f"{ticker}: still rate-limited after {MAX_RETRIES} attempts")


#: Gap below which two of Tiingo's ranges are the same listing.
#:
#: The file frequently splits one continuous series across two rows for a
#: venue change: AIEQ ends 2024-01-26, a Friday, and resumes 2024-01-29, the
#: Monday. Treating that as a relisting would fragment ~100 healthy symbols.
#: Measured against the real file, the count of multi-listing active symbols
#: is 516 at zero tolerance, 418 at seven days, and 416 at thirty -- it goes
#: flat after a week, because a genuine recycle leaves a hole of months or
#: years, never a weekend.
LISTING_MERGE_TOLERANCE_DAYS: Final[int] = 7


def listing_spans(
    rows: Iterable[dict[str, Any]],
    tolerance_days: int = LISTING_MERGE_TOLERANCE_DAYS,
) -> dict[str, list[tuple[date, date]]]:
    """Disjoint listing periods per ticker, from Tiingo's own universe file.

    Tiingo knows a recycled symbol is two things -- it carries two rows with
    non-overlapping ranges -- and this recovers that. Overlapping or
    near-adjacent ranges are merged, because those are one listing quoted on
    two venues rather than two companies.
    """
    ranges: dict[str, list[tuple[date, date]]] = {}
    for row in rows:
        ticker = (row.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        start = _parse_iso(row.get("startDate"))
        if start is None:
            continue
        end = _parse_iso(row.get("endDate")) or date.max
        ranges.setdefault(ticker, []).append((start, end))

    tol = timedelta(days=tolerance_days)
    out: dict[str, list[tuple[date, date]]] = {}
    for ticker, spans in ranges.items():
        spans.sort()
        merged = [spans[0]]
        for start, end in spans[1:]:
            last_start, last_end = merged[-1]
            if start <= last_end + tol:
                merged[-1] = (last_start, max(last_end, end))
            else:
                merged.append((start, end))
        out[ticker] = merged
    return out


def _parse_iso(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except (ValueError, TypeError, AttributeError):
        return None


def listing_for(spans: list[tuple[date, date]] | None, day: date) -> date | None:
    """Which listing a bar belongs to, identified by that listing's first day.

    The identifier is the span's start date rather than an ordinal (1, 2, 3)
    on purpose. Partitions are immutable, and an ordinal renumbers every
    earlier bar the moment Tiingo adds a listing that predates the ones we
    already know about -- so ten years of stored history would silently change
    meaning. A start date only moves if that specific listing's start moves.

    Returns None when the bar falls outside every known span, which is a real
    state and must not be collapsed into "the first listing": that is exactly
    the silent default this whole change exists to remove.
    """
    if not spans:
        return None
    for start, end in spans:
        if start <= day <= end:
            return start
    return None


# --------------------------------------------------------------------------
# shaping
# --------------------------------------------------------------------------


def _dec(value: Any) -> Decimal | None:
    """Prices as Decimal, quantised to six places. Never float, never cents.

    Sub-penny quotes are the point: $0.000200 has to survive, and the sub-$1
    band is a headline feature of the screens.
    """
    if value is None:
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.000001"))
    except (ArithmeticError, ValueError):
        return None


#: Corporate-action scale. Twelve places, and quantised rather than passed
#: through, because a reverse split ratio is frequently non-terminating:
#: 1-for-15 arrives as 0.0666666667 and 1-for-18 as 0.0555555556, both ten
#: places. The column was decimal128(18,8) and pyarrow refused them outright
#: -- "Rescaling Decimal value would cause data loss" -- which is the right
#: instinct and the wrong place to discover it, since it only shows up once
#: the history is deep enough to contain such a split. Twelve matches the
#: width the screens already widen to for the adjustment division.
_ACTION_SCALE: Final[Decimal] = Decimal("0.000000000001")


def _factor(value: Any, default: Decimal) -> Decimal:
    """A split factor or dividend, quantised to the stored scale."""
    if value is None:
        return default
    try:
        return Decimal(str(value)).quantize(_ACTION_SCALE)
    except (ArithmeticError, ValueError):
        return default


def shape_rows(
    ticker: str,
    meta: TickerMeta | None,
    payload: Iterable[dict[str, Any]],
    spans: list[tuple[date, date]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split a Tiingo payload into raw price rows and corporate actions.

    The ``adj*`` columns are dropped on purpose. Storing adjusted prices is
    what would make history mutable.
    """
    prices: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    ingested = datetime.now(timezone.utc)

    for bar in payload:
        day = _parse_date(bar.get("date"))
        if day is None:
            continue

        prices.append(
            {
                "ticker": ticker,
                "date": day,
                "open": _dec(bar.get("open")),
                "high": _dec(bar.get("high")),
                "low": _dec(bar.get("low")),
                "close": _dec(bar.get("close")),
                "volume": int(bar.get("volume") or 0),
                "exchange": (meta.exchange.lower() if meta else None),
                "security_type": (meta.asset_type if meta else None),
                "source": "tiingo",
                # Which company this bar belongs to. A ticker is not an
                # entity across time: 356 active symbols carry two different
                # companies inside a ten-year pull, and without this the two
                # concatenate into one series.
                "listing_id": listing_for(spans, day),
                "ingested_at": ingested,
            }
        )

        split = bar.get("splitFactor")
        div = bar.get("divCash")
        split_f = _factor(split, Decimal(1))
        div_f = _factor(div, Decimal(0))
        if split_f != 1 or div_f != 0:
            actions.append(
                {
                    "ticker": ticker,
                    "ex_date": day,
                    "split_factor": split_f,
                    "div_cash": div_f,
                    "source": "tiingo",
                }
            )

    return prices, actions


# --------------------------------------------------------------------------
# sweep
# --------------------------------------------------------------------------


def sweep(
    tickers: list[TickerMeta],
    start: date,
    end: date,
    *,
    staging: Path,
    run_id: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    rate_per_hour: int = DEFAULT_RATE_PER_HOUR,
    restart: bool = False,
    checkpoint_dir: Path | None = None,
    progress: bool = True,
    spans: dict[str, list[tuple[date, date]]] | None = None,
) -> SweepResult:
    """Fetch every ticker, chunk by chunk, resuming by default.

    Each chunk's rows are written to their own Parquet in ``staging`` and the
    checkpoint is updated before the next chunk starts. A kill at chunk 90 of
    120 costs at most the chunk in flight.
    """
    ordered = sorted(tickers, key=lambda t: t.ticker)
    groups = chunked(ordered, chunk_size)
    staging.mkdir(parents=True, exist_ok=True)

    cp = Checkpoint.load_or_create(
        f"tiingo_{run_id}", run_id, len(groups), directory=checkpoint_dir
    )
    if restart:
        cp.clear()
        for stale in staging.glob("chunk_*.parquet"):
            stale.unlink()

    result = SweepResult(resumed_chunks=cp.done_count)
    result.rows = cp.sum_of("rows")
    result.actions = cp.sum_of("actions")
    result.succeeded = cp.sum_of("succeeded")
    result.failed = cp.sum_of("failed")
    result.attempted = cp.sum_of("attempted")
    result.resumed_attempted = result.attempted

    pending = cp.pending()
    if cp.done_count:
        print(
            f"   resuming: {cp.done_count}/{len(groups)} chunks already done "
            f"({result.rows:,} rows), {len(pending)} to go"
        )

    pacer = Pacer(rate_per_hour)
    by_ticker = {t.ticker: t for t in ordered}
    con = duckdb.connect()
    started = time.monotonic()

    for index in pending:
        group = groups[index]
        rows: list[dict[str, Any]] = []
        actions: list[dict[str, Any]] = []
        ok = bad = 0

        with _client() as client:
            for meta in group:
                result.attempted += 1
                try:
                    payload = fetch_prices(
                        client, meta.ticker, start, end, result, pacer
                    )
                except TiingoError as exc:
                    bad += 1
                    result.failures.append((meta.ticker, str(exc)[:120]))
                    continue
                p, a = shape_rows(
                    meta.ticker, by_ticker.get(meta.ticker), payload,
                    spans=spans.get(meta.ticker) if spans else None,
                )
                rows.extend(p)
                actions.extend(a)
                ok += 1

        chunk_file = staging / f"chunk_{index:05d}.parquet"
        if rows:
            _write_parquet(con, rows, chunk_file)
        actions_file = staging / f"actions_{index:05d}.parquet"
        if actions:
            _write_parquet(con, actions, actions_file)

        result.succeeded += ok
        result.failed += bad
        result.rows += len(rows)
        result.actions += len(actions)

        # Persisted before the next chunk begins. This is the whole design.
        cp.mark_done(
            index,
            attempted=len(group),
            succeeded=ok,
            failed=bad,
            rows=len(rows),
            actions=len(actions),
            file=chunk_file.name if rows else None,
        )

        if progress:
            done = cp.done_count
            pct = done / len(groups) * 100
            print(
                f"   chunk {done:>4}/{len(groups)} ({pct:5.1f}%)  "
                f"+{len(rows):>6,} rows  ok={ok:>3} fail={bad}",
                flush=True,
            )

    result.elapsed = time.monotonic() - started
    return result


def _write_parquet(
    con: duckdb.DuckDBPyConnection, rows: list[dict[str, Any]], target: Path
) -> None:
    """Write rows with an explicit schema. Never let types be inferred."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    if "close" in rows[0]:
        price = pa.decimal128(18, 6)
        schema = pa.schema(
            [
                ("ticker", pa.string()),
                ("date", pa.date32()),
                ("open", price),
                ("high", price),
                ("low", price),
                ("close", price),
                ("volume", pa.int64()),
                ("exchange", pa.string()),
                ("security_type", pa.string()),
                ("source", pa.string()),
                ("listing_id", pa.date32()),
                ("ingested_at", pa.timestamp("us", tz="UTC")),
            ]
        )
    else:
        schema = pa.schema(
            [
                ("ticker", pa.string()),
                ("ex_date", pa.date32()),
                ("split_factor", pa.decimal128(18, 12)),
                ("div_cash", pa.decimal128(18, 12)),
                ("source", pa.string()),
            ]
        )

    columns = {name: [r.get(name) for r in rows] for name in schema.names}
    pq.write_table(pa.table(columns, schema=schema), target, compression="zstd")


# --------------------------------------------------------------------------
# publish
# --------------------------------------------------------------------------


#: A closed year's boundary sessions. The last US trading day of a year is
#: always 29, 30 or 31 December -- 31st unless it falls on a weekend, in which
#: case the Friday before. The first is 2, 3 or 4 January by the same logic.
#: Asserting against these ranges catches a truncated historical pull without
#: taking a market-calendar dependency for two facts that never move.
LAST_SESSION_ON_OR_AFTER: Final[int] = 29      # December
FIRST_SESSION_ON_OR_BEFORE: Final[int] = 8     # January, with slack


def _assert_partition_fresh(
    rel: Any,
    *,
    partition: str,
    min_rows: int,
    max_staleness_days: int,
    today: date | None = None,
) -> Any:
    """Freshness for a year partition, under the contract that year deserves.

    A closed year cannot be *fresh*: 2020's newest bar is five years old and
    always will be. Skipping the assertion there would be the wrong fix, since
    a truncated historical pull -- half a year fetched, or a range that quietly
    stopped in June -- is exactly what needs to fail loudly. So only the
    wall-clock test is dropped. Row count, columns, and both year boundaries
    are still asserted, which is a *stronger* check than the live partition
    gets: a closed year has a known shape and we can hold it to it.

    The current year keeps the ordinary contract, because for it staleness is
    the real signal.
    """
    today = today or utc_today()
    year = int(partition)

    if year >= today.year:
        return assert_fresh(
            DATASET,
            rel,
            partition=partition,
            min_rows=min_rows,
            expect_cols=("ticker", "close", "volume", "source"),
            max_staleness_days=max_staleness_days,
        )

    # Closed year: assert everything except wall-clock staleness.
    observed = assert_fresh(
        DATASET,
        rel,
        partition=partition,
        min_rows=min_rows,
        date_column=None,
        expect_cols=("ticker", "date", "close", "volume", "source"),
    )

    row = rel.query("rel", "SELECT min(date) AS lo, max(date) AS hi FROM rel").fetchone()
    lo, hi = row[0], row[1]
    if lo is None or hi is None:
        raise StaleDataError(
            f"{DATASET}/{partition}: {observed.row_count:,} rows but no dates, "
            "so the partition's span cannot be established."
        )

    if hi.year != year or lo.year != year:
        raise StaleDataError(
            f"{DATASET}/{partition}: spans {lo.isoformat()}..{hi.isoformat()}, "
            f"which is not entirely within {year}. A partition holds one year; "
            "publish() filters staged rows by year, so this means the "
            "already-published file is wrong."
        )

    if hi < date(year, 12, LAST_SESSION_ON_OR_AFTER):
        raise StaleDataError(
            f"{DATASET}/{partition}: newest bar is {hi.isoformat()}, but "
            f"{year} ran to the end of December. The pull stopped early -- a "
            f"complete year ends on the 29th, 30th or 31st. "
            f"{observed.row_count:,} rows is not evidence to the contrary; a "
            "truncated year is still a large number of rows."
        )

    if lo > date(year, 1, FIRST_SESSION_ON_OR_BEFORE):
        raise StaleDataError(
            f"{DATASET}/{partition}: oldest bar is {lo.isoformat()}, but "
            f"{year} began trading in the first week of January. The pull "
            "started late; the front of the year is missing."
        )

    # assert_fresh was called with date_column=None to skip the wall-clock
    # test, which also means it did not compute a max date. Put the one
    # measured above back on the observation, or dataset_stats records NULL
    # for every historical partition and the freshness history has a hole in
    # it exactly where the backfill is.
    return replace(observed, max_date=hi)


def staged_years(con: duckdb.DuckDBPyConnection, staging: Path) -> list[str]:
    """Which year partitions this staging directory has rows for.

    A sweep window that crosses New Year produces two, and a backfill produces
    as many as it covers.
    """
    pattern = (staging / "chunk_*.parquet").as_posix()
    rows = con.execute(
        f"SELECT DISTINCT year(date) AS y FROM read_parquet('{pattern}') "
        "WHERE date IS NOT NULL ORDER BY y"
    ).fetchall()
    return [str(int(r[0])) for r in rows]


def publish_all(
    staging: Path,
    *,
    con: duckdb.DuckDBPyConnection | None = None,
    min_rows_factor: float = 0.5,
    max_staleness_days: int = 4,
    restate: bool = False,
) -> list[Any]:
    """Publish every year present in staging, one partition each.

    A ten-year backfill is a single pass over the universe -- one Tiingo
    request per ticker returns the whole range -- but it lands in eleven
    partitions. Splitting here rather than sweeping once per year is the
    difference between 14,124 requests and 141,240.

    ``min_rows_factor`` is applied to each year's own staged count, so a year
    where a ticker listed halfway through is not held to the same floor as a
    full one.
    """
    con = con or storage.connect()
    pattern = (staging / "chunk_*.parquet").as_posix()
    observed: list[Any] = []

    for partition in staged_years(con, staging):
        staged = int(
            con.execute(
                f"SELECT count(*) FROM read_parquet('{pattern}') "
                f"WHERE year(date) = {int(partition)}"
            ).fetchone()[0]
        )
        observed.append(
            publish(
                staging,
                partition,
                con=con,
                min_rows=max(1, int(staged * min_rows_factor)),
                max_staleness_days=max_staleness_days,
                restate=restate,
            )
        )
    return observed

def _q(value: str) -> str:
    """Escape a single-quoted SQL literal. Locations come from the
    manifest, which is trusted config, but COPY targets cannot be bound
    as parameters so they are escaped rather than trusted twice."""
    return value.replace("'", "''")


class PartitionShrankError(TiingoError):
    """A publish would have made a partition smaller than it already was.

    Almost always means the merge did not happen and the sweep window was
    about to overwrite accumulated history. Overridable, because a deliberate
    restatement is a real thing, but never silently.
    """


def _existing_rows(con: duckdb.DuckDBPyConnection, location: str) -> int | None:
    """Row count already published at ``location``, or None if there is none.

    A missing object is the normal first-publish case, not an error, and is
    indistinguishable from an unreadable one at this layer — both mean "no
    prior history to merge", and the shrink guard below is what makes that
    safe to assume.
    """
    try:
        return int(
            con.execute(
                "SELECT count(*) FROM read_parquet(?)", [location]
            ).fetchone()[0]
        )
    except duckdb.Error as exc:
        log.info("no existing partition at %s (%s)", location, str(exc)[:120])
        return None


def publish(
    staging: Path,
    partition: str,
    *,
    con: duckdb.DuckDBPyConnection | None = None,
    min_rows: int,
    max_staleness_days: int = 4,
    restate: bool = False,
) -> Any:
    """Merge published + staged, rewrite the whole partition, assert freshness.

    Parquet has no upsert, so a partition is always rewritten whole. The thing
    that matters is *what goes into the rewrite*: the previously published file
    as well as this sweep's chunks. Rewriting from staging alone silently
    discards every session outside the sweep window, which is how a
    year-partitioned store ends up holding two days.

    Dedupe is on ``(ticker, date, source)`` keeping the newest ``ingested_at``,
    so a re-run of an overlapping window replaces those bars rather than
    duplicating them, and history outside the window is carried forward
    untouched.

    The merge streams through a local file rather than reading and writing the
    same remote object in one statement. DuckDB's COPY is lazy, and pointing
    the read side of a query at the object the write side is replacing is a
    good way to produce a truncated file. It also keeps memory flat: a full
    year is millions of rows and neither side is ever materialised in RAM.

    Raises :class:`PartitionShrankError` if the merged result would be smaller
    than what is already published. That is the assertion this function
    needed on day one — the overwrite bug produced a *valid, fresh, smaller*
    partition every night, and every other check passed it.
    """
    ref = manifest.get(DATASET, partition)
    if ref.backend not in manifest.PRIVATE_BACKENDS:
        raise TiingoError(
            f"{DATASET}/{partition} resolves to backend {ref.backend!r}, which is "
            "publicly readable. Tiingo data is vendor-derived and must go to R2. "
            "See the licensing rule in CLAUDE.md."
        )

    con = con or storage.connect()
    chunks = sorted(staging.glob("chunk_*.parquet"))
    if not chunks:
        raise TiingoError(f"No staged chunks in {staging}; nothing to publish.")

    pattern = (staging / "chunk_*.parquet").as_posix()
    prior_rows = _existing_rows(con, ref.location)

    # A partition holds exactly one year. Filtering here rather than trusting
    # the caller matters for a sweep that crosses New Year and for a backfill
    # that spans a decade -- both stage rows for several years at once, and
    # without this the first partition written would swallow all of them.
    staged_sql = (
        f"SELECT * FROM read_parquet('{pattern}') "
        f"WHERE year(date) = {int(partition)}"
    )

    if prior_rows is None or restate:
        source_sql = staged_sql
        if restate:
            log.warning(
                "restating %s/%s from staging alone; %s existing rows will be "
                "replaced, not merged",
                DATASET, partition,
                "0" if prior_rows is None else f"{prior_rows:,}",
            )
        else:
            log.info("first publish of %s/%s", DATASET, partition)
    else:
        # Column sets are compared before the union so schema drift reports as
        # schema drift. UNION ALL BY NAME would otherwise fill a renamed or
        # dropped column with NULLs and publish it looking healthy.
        staged_cols = {c.lower() for c in con.sql(
            f"{staged_sql} LIMIT 0").columns}
        published_cols = {c.lower() for c in con.sql(
            "SELECT * FROM read_parquet(?) LIMIT 0", params=[ref.location]).columns}
        if staged_cols != published_cols:
            raise TiingoError(
                f"{DATASET}/{partition}: staged and published schemas differ. "
                f"only in staging: {sorted(staged_cols - published_cols)}; "
                f"only in published: {sorted(published_cols - staged_cols)}. "
                "Refusing to merge -- resolve the schema change deliberately."
            )
        source_sql = (
            f"{staged_sql} UNION ALL BY NAME "
            f"SELECT * FROM read_parquet('{_q(ref.location)}')"
        )

    # Test symbols are excluded here as well as in the universe filter, and
    # on both sides of the merge. The universe filter only governs what a
    # *future* sweep fetches; rows already published carry forward through
    # every merge until something drops them. ZBZX, ZTST and PTEST-Z reached
    # the partitions before the filter knew about the Cboe and NYSE families,
    # and a test symbol's quote is arbitrary by design -- PTEST-Z printed
    # 0.05 -> 25.00, which is a +49,900% day in the screens.
    merged_sql = f"""
        SELECT * EXCLUDE (rn) FROM (
            SELECT *, row_number() OVER (
                PARTITION BY ticker, date, source ORDER BY ingested_at DESC
            ) AS rn
            FROM ({source_sql})
            WHERE NOT regexp_matches(upper(ticker), '{TEST_SYMBOL_SQL}')
        ) WHERE rn = 1
    """

    local = staging / "merged.parquet"
    if local.exists():
        local.unlink()
    con.execute(f"COPY ({merged_sql}) TO '{local.as_posix()}' (FORMAT parquet)")

    merged_rel = con.read_parquet(local.as_posix())
    merged_rows = int(
        con.execute(
            "SELECT count(*) FROM read_parquet(?)", [local.as_posix()]
        ).fetchone()[0]
    )

    if prior_rows is not None and merged_rows < prior_rows and not restate:
        raise PartitionShrankError(
            f"{DATASET}/{partition}: publishing would take the partition from "
            f"{prior_rows:,} rows to {merged_rows:,}, a loss of "
            f"{prior_rows - merged_rows:,}. A sweep adds sessions; it does not "
            "remove them. This is what an overwrite-instead-of-merge looks "
            "like. If the shrink is deliberate -- a restatement, or purging "
            "symbols that should never have been swept -- pass restate=True, "
            "which publishes exactly what is staged and merges nothing."
        )

    con.execute(
        f"COPY (SELECT * FROM read_parquet('{local.as_posix()}')) "
        f"TO '{_q(ref.location)}' (FORMAT parquet)"
    )
    log.info(
        "%s/%s: %s -> %s rows",
        DATASET, partition,
        "first publish" if prior_rows is None else f"{prior_rows:,}",
        f"{merged_rows:,}",
    )

    observed = _assert_partition_fresh(
        merged_rel,
        partition=partition,
        min_rows=min_rows,
        max_staleness_days=max_staleness_days,
    )
    manifest.record_stats(observed, con=con)
    return observed


#: Rows per INSERT. One statement per row cost this table 90% of its
#: contents: a 10-year backfill stages ~254,000 actions, and 254,000
#: sequential round trips do not finish. 500 keeps the statement well inside
#: any parameter limit while turning that into ~510 round trips.
ACTION_BATCH: Final[int] = 500


def upsert_corporate_actions(
    staging: Path, con: duckdb.DuckDBPyConnection | None = None
) -> int:
    """Push staged corporate actions into Postgres, idempotently.

    ON CONFLICT DO NOTHING against the (ticker, ex_date, source) natural key,
    so re-running a sweep never duplicates a split.

    **Returns what landed, not what was attempted, and raises if they
    differ.** The previous version issued one round trip per row and then
    returned ``len(rows)`` regardless of outcome, so a run that inserted a
    tenth of its rows reported complete success. The result was a
    ``corporate_actions`` table holding 365 splits where the staged parquet
    held 3,724 -- and since the volatility screens adjust from that table, a
    1-for-20 reverse split (AYTU, 2023-01-06) sat in the data reading as a
    genuine +1,751% move. Nothing warned, because the only number reported
    was the one we hoped for.

    The verification is an anti-join rather than a count comparison: equal
    totals can still be the wrong rows.
    """
    files = sorted(staging.glob("actions_*.parquet"))
    if not files:
        return 0

    con = con or storage.connect()
    if not storage.postgres_attached(con):
        log.warning("No Postgres attached; %d corporate actions not stored.", len(files))
        return 0

    pattern = (staging / "actions_*.parquet").as_posix()
    rows = con.execute(
        f"""
        SELECT DISTINCT ticker, ex_date, split_factor, div_cash, source
        FROM read_parquet('{pattern}')
        """
    ).fetchall()
    if not rows:
        return 0

    def lit(value: Any) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    for start in range(0, len(rows), ACTION_BATCH):
        batch = rows[start:start + ACTION_BATCH]
        values = ", ".join(
            f"({lit(ticker)}, date {lit(ex_date)}, {split_factor}, "
            f"{div_cash}, {lit(source)})"
            for ticker, ex_date, split_factor, div_cash, source in batch
        )
        con.execute(
            "CALL postgres_execute('pg', ?)",
            [
                "insert into corporate_actions "
                "(ticker, ex_date, split_factor, div_cash, source) values "
                f"{values} on conflict (ticker, ex_date, source) do nothing"
            ],
        )

    missing = con.execute(
        f"""
        SELECT count(*) FROM (
            SELECT DISTINCT ticker, ex_date, source
            FROM read_parquet('{pattern}')
        ) staged
        ANTI JOIN (
            SELECT ticker, ex_date, source FROM postgres_query('pg',
                'select ticker, ex_date, source from corporate_actions')
        ) stored USING (ticker, ex_date, source)
        """
    ).fetchone()[0]
    if missing:
        raise TiingoError(
            f"{missing:,} of {len(rows):,} staged corporate actions are not in "
            "corporate_actions after the upsert. Refusing to report success: "
            "the volatility screens adjust from this table, and a missing "
            "reverse split reads as a real -95% day."
        )
    return len(rows)


def default_window(days: int = 5, today: date | None = None) -> tuple[date, date]:
    """The sweep window, anchored on the trading date rather than UTC.

    See :mod:`marketradar.clock`: the nightly cron runs after the UTC rollover
    but before the ET one, so a UTC-derived end date is a day ahead of the
    session that just closed.
    """
    from marketradar.clock import market_today

    end = today or market_today()
    return end - timedelta(days=days), end
