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
import time
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Iterable

import duckdb
import httpx

from marketradar import manifest, storage
from marketradar.checkpoint import Checkpoint, chunked
from marketradar.freshness import assert_fresh

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

#: Exchange test symbols. NYSE publishes ATEST* as live-looking rows; they are
#: not real listings and would otherwise reach the screens.
TEST_SYMBOL_PREFIXES: Final[tuple[str, ...]] = ("ATEST",)


def include_row(row: dict[str, Any], cutoff: date | None) -> bool:
    """Whether one universe row belongs in the sweep.

    Split out from :func:`fetch_universe` so the filtering rules can be
    tested without downloading anything.
    """
    ticker = (row.get("ticker") or "").strip().upper()
    if not ticker or any(ticker.startswith(p) for p in TEST_SYMBOL_PREFIXES):
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
        cutoff = (today or datetime.now(timezone.utc).date()) - timedelta(
            days=active_within_days
        )

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


def shape_rows(
    ticker: str, meta: TickerMeta | None, payload: Iterable[dict[str, Any]]
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
                "ingested_at": ingested,
            }
        )

        split = bar.get("splitFactor")
        div = bar.get("divCash")
        split_f = Decimal(str(split)) if split is not None else Decimal(1)
        div_f = Decimal(str(div)) if div is not None else Decimal(0)
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
                p, a = shape_rows(meta.ticker, by_ticker.get(meta.ticker), payload)
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
                ("ingested_at", pa.timestamp("us", tz="UTC")),
            ]
        )
    else:
        schema = pa.schema(
            [
                ("ticker", pa.string()),
                ("ex_date", pa.date32()),
                ("split_factor", pa.decimal128(18, 8)),
                ("div_cash", pa.decimal128(18, 8)),
                ("source", pa.string()),
            ]
        )

    columns = {name: [r.get(name) for r in rows] for name in schema.names}
    pq.write_table(pa.table(columns, schema=schema), target, compression="zstd")


# --------------------------------------------------------------------------
# publish
# --------------------------------------------------------------------------


def publish(
    staging: Path,
    partition: str,
    *,
    con: duckdb.DuckDBPyConnection | None = None,
    min_rows: int,
    max_staleness_days: int = 4,
) -> Any:
    """Merge staged chunks, publish to the manifest location, assert freshness.

    The destination is resolved through the manifest, which is what keeps
    vendor data off GitHub Releases: ``prices_*`` is declared ``r2`` and a
    test fails the build if that ever changes.
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
    # Idempotent by rewrite: Parquet has no upsert, so dedupe on the natural
    # key keeping the newest ingest and rewrite the whole partition.
    con.execute(
        f"""
        CREATE OR REPLACE VIEW merged AS
        SELECT * EXCLUDE (rn) FROM (
            SELECT *, row_number() OVER (
                PARTITION BY ticker, date, source ORDER BY ingested_at DESC
            ) AS rn
            FROM read_parquet('{pattern}')
        ) WHERE rn = 1
        """
    )
    con.execute(f"COPY merged TO '{ref.location}' (FORMAT parquet)")

    rel = con.view("merged")
    observed = assert_fresh(
        DATASET,
        rel,
        partition=partition,
        min_rows=min_rows,
        expect_cols=("ticker", "close", "volume", "source"),
        max_staleness_days=max_staleness_days,
    )
    manifest.record_stats(observed, con=con)
    return observed


def upsert_corporate_actions(
    staging: Path, con: duckdb.DuckDBPyConnection | None = None
) -> int:
    """Push staged corporate actions into Postgres, idempotently.

    ON CONFLICT DO NOTHING against the (ticker, ex_date, source) natural key,
    so re-running a sweep never duplicates a split.
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

    for ticker, ex_date, split_factor, div_cash, source in rows:
        con.execute(
            "CALL postgres_execute('pg', ?)",
            [
                "insert into corporate_actions "
                "(ticker, ex_date, split_factor, div_cash, source) values ("
                f"'{ticker}', date '{ex_date}', {split_factor}, {div_cash}, '{source}'"
                ") on conflict (ticker, ex_date, source) do nothing"
            ],
        )
    return len(rows)


def default_window(days: int = 5, today: date | None = None) -> tuple[date, date]:
    end = today or datetime.now(timezone.utc).date()
    return end - timedelta(days=days), end
