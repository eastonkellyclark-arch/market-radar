"""FRED — Treasury yields and credit spreads.

Three series, once a day, from the St. Louis Fed. This is the macro line the
digest opens with: where the risk-free rate sits and what credit is charging
over it. Cheap, small, and slow-moving, so it is fetched whole each run and
upserted rather than incrementally tracked.

**Licensing.** ``DGS10`` is US Treasury data and public domain. The two
``BAMLxxx`` series are ICE BofA indices that FRED redistributes under
permission from ICE Data Indices, LLC — they are not government-produced.
Republishing those as a public Release asset is redistribution of a
third-party index, which is exactly the boundary CLAUDE.md makes a hard rule.
:func:`publish` therefore refuses to run until that call is made explicitly;
:func:`load` is unaffected and stores everything locally.

**Two freshness checks, not one.** A combined assertion over all three series
passes as long as *any* of them is current, which is precisely how a stalled
series hides. DGS10 is published on the H.15 release schedule and routinely
lags the spread series by a day, so each series is asserted separately with a
window wide enough for a holiday weekend and that lag.

No literal URL here; the endpoint resolves through
``manifest.get('fred_api', 'base')``.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final, Iterable

import duckdb
import httpx

from marketradar import manifest, storage
from marketradar.freshness import assert_fresh

log = logging.getLogger(__name__)

ENV_API_KEY: Final[str] = "MR_FRED_API_KEY"
DATASET: Final[str] = "fred_series"
SOURCE: Final[str] = "fred"

RELEASE_TAG: Final[str] = "reference-data"
ASSET_NAME: Final[str] = "fred_series.parquet"

REQUEST_TIMEOUT: Final[float] = 60.0
BATCH_SIZE: Final[int] = 500

#: How far back to pull. Ten years covers a full rate cycle, which is what the
#: later analog work wants, and is still only a few thousand rows per series.
HISTORY_START: Final[date] = date(2015, 1, 1)

#: Floors are per series because the available history is not the same length.
#: FRED serves DGS10 from 1962, but its own ``observation_start`` for both
#: BAMLxxx series is 2023-09-11 — ICE only licenses a rolling window, so
#: ``HISTORY_START`` is moot for them and a single global floor would either
#: fail the spreads every run or be too slack to catch a truncated Treasury
#: pull. Floors are lower bounds and the series only grow, so they do not need
#: revisiting. Worth knowing downstream: credit spreads have ~3 years of
#: history here, not ten, which bounds any analog work built on them.

#: Wider than the four-day default on purpose. DGS10 follows the H.15 release
#: schedule and lands a day behind the spread series, and a Friday print read
#: after a Monday holiday is already three days old before anything is wrong.
#: Seven catches a genuinely stopped feed while surviving both.
MAX_STALENESS_DAYS: Final[int] = 7


class FredError(RuntimeError):
    """FRED could not be reached, or answered with something unusable."""


@dataclass(frozen=True, slots=True)
class Series:
    series_id: str
    label: str        # what the digest prints
    units: str
    public_domain: bool
    min_rows: int


#: The ``public_domain`` flag is not decoration — :func:`publish` reads it.
SERIES: Final[tuple[Series, ...]] = (
    # ~2,900 business days available from HISTORY_START.
    Series("DGS10", "10y Treasury", "%", public_domain=True, min_rows=2_000),
    # ~790 available and growing; FRED's window opens 2023-09-11.
    Series("BAMLH0A0HYM2", "HY OAS", "%", public_domain=False, min_rows=600),
    Series("BAMLC0A0CM", "IG OAS", "%", public_domain=False, min_rows=600),
)

BY_ID: Final[dict[str, Series]] = {s.series_id: s for s in SERIES}


@dataclass(frozen=True, slots=True)
class Observation:
    series_id: str
    obs_date: date
    value: Decimal | None


def api_key() -> str:
    key = os.environ.get(ENV_API_KEY, "").strip()
    if not key:
        raise FredError(
            f"{ENV_API_KEY} is not set. FRED needs a free API key; requests "
            "without one come back 400, not 401, which is confusing enough to "
            "be worth naming here."
        )
    return key


def _parse_value(raw: Any) -> Decimal | None:
    """FRED writes ``"."`` for a date with no print. That is not zero."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or text == ".":
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _parse_date(raw: Any) -> date | None:
    try:
        return date.fromisoformat(str(raw).strip()[:10])
    except (ValueError, TypeError):
        return None


def fetch(
    series: Iterable[Series] = SERIES,
    start: date = HISTORY_START,
    client: httpx.Client | None = None,
) -> list[Observation]:
    """One request per series. Three requests, no pagination, no budget."""
    base = manifest.get("fred_api", "base").location
    key = api_key()
    owns_client = client is None
    client = client or httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=True)

    out: list[Observation] = []
    try:
        for s in series:
            out.extend(_fetch_one(client, base, key, s, start))
    finally:
        if owns_client:
            client.close()
    return out


def _fetch_one(
    client: httpx.Client, base: str, key: str, series: Series, start: date
) -> list[Observation]:
    try:
        resp = client.get(
            f"{base}/series/observations",
            params={
                "series_id": series.series_id,
                "api_key": key,
                "file_type": "json",
                "observation_start": start.isoformat(),
            },
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise FredError(
            f"{series.series_id}: FRED returned HTTP {exc.response.status_code}. "
            f"{exc.response.text[:200]}"
        ) from exc
    except httpx.HTTPError as exc:
        raise FredError(f"{series.series_id}: could not reach FRED: {exc}") from exc

    try:
        payload = resp.json()
    except ValueError as exc:
        raise FredError(f"{series.series_id}: FRED response was not JSON") from exc

    rows = payload.get("observations")
    if not isinstance(rows, list):
        raise FredError(
            f"{series.series_id}: no 'observations' array in the response. "
            "The series id may have been retired."
        )

    out: list[Observation] = []
    for row in rows:
        obs_date = _parse_date(row.get("date"))
        if obs_date is None:
            continue
        out.append(
            Observation(
                series_id=series.series_id,
                obs_date=obs_date,
                value=_parse_value(row.get("value")),
            )
        )
    log.debug("%s: %d observations", series.series_id, len(out))
    return out


def _write_parquet(observations: list[Observation], target: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    ingested = datetime.now(timezone.utc)
    schema = pa.schema(
        [
            ("series_id", pa.string()),
            ("obs_date", pa.date32()),
            ("value", pa.decimal128(18, 6)),
            ("source", pa.string()),
            ("ingested_at", pa.timestamp("us", tz="UTC")),
        ]
    )
    table = pa.table(
        {
            "series_id": [o.series_id for o in observations],
            "obs_date": [o.obs_date for o in observations],
            "value": [o.value for o in observations],
            "source": [SOURCE] * len(observations),
            "ingested_at": [ingested] * len(observations),
        },
        schema=schema,
    )
    pq.write_table(table, target, compression="zstd")


def _gh() -> str:
    import shutil

    found = shutil.which("gh") or shutil.which("gh", path=r"C:\Program Files\GitHub CLI")
    if not found:
        raise FredError("gh CLI not found; needed to publish the GitHub Release.")
    return found


def assert_series_fresh(
    rel: duckdb.DuckDBPyRelation,
    *,
    con: duckdb.DuckDBPyConnection,
    record: bool = True,
) -> list[Any]:
    """Assert every series separately, then record each observation.

    A single assertion over the union passes whenever *one* series is current,
    which is exactly how a retired or renamed series goes unnoticed. Each is
    checked on its own so a stalled one fails the run.
    """
    observed = []
    con.register("fred_rel", rel)
    for s in SERIES:
        part = con.sql(
            f"select * from fred_rel where series_id = '{s.series_id}' "
            "and value is not null"
        )
        obs = assert_fresh(
            DATASET,
            part,
            partition=s.series_id,
            min_rows=s.min_rows,
            date_column="obs_date",
            max_staleness_days=MAX_STALENESS_DAYS,
            expect_cols=("series_id", "obs_date", "value"),
        )
        observed.append(obs)
        if record:
            manifest.record_stats(obs, con=con)
    return observed


def publish(
    observations: list[Observation],
    *,
    con: duckdb.DuckDBPyConnection | None = None,
    allow_licensed: bool = False,
) -> list[Any]:
    """Write Parquet, upload as a Release asset, assert freshness per series.

    Refuses to publish the ICE BofA series to a world-readable Release until
    that is explicitly allowed. This is the licensing rule enforced in code
    rather than in a comment: a Release asset on a public repo is
    redistribution, and only the Treasury series is unambiguously ours to
    redistribute. Pass ``allow_licensed=True`` once the call has been made.
    """
    ref = manifest.get(DATASET, "all")
    if ref.backend != "github_release":
        raise FredError(
            f"{DATASET}/all resolves to backend {ref.backend!r}. FRED data "
            "belongs in a GitHub Release, not a private bucket. See the "
            "licensing rule in CLAUDE.md."
        )

    licensed = sorted({o.series_id for o in observations} - {
        s.series_id for s in SERIES if s.public_domain
    })
    if licensed and not allow_licensed:
        raise FredError(
            "Refusing to publish "
            + ", ".join(licensed)
            + " to a public GitHub Release. Those are ICE BofA indices that "
            "FRED redistributes under permission from ICE Data Indices, LLC; "
            "they are not government-produced, so republishing them is "
            "redistribution of a third-party index. Publish the Treasury "
            "series alone, or pass allow_licensed=True once that call is made."
        )

    repo = os.environ.get("MR_GITHUB_REPO")
    if not repo:
        raise FredError("MR_GITHUB_REPO is unset; needed to publish the Release.")

    con = con or storage.connect()
    gh = _gh()

    with tempfile.TemporaryDirectory(prefix="mr-fred-") as tmp:
        local = Path(tmp) / ASSET_NAME
        _write_parquet(observations, local)

        seen = subprocess.run(
            [gh, "release", "view", RELEASE_TAG, "--repo", repo],
            capture_output=True, text=True,
        )
        if seen.returncode != 0:
            created = subprocess.run(
                [gh, "release", "create", RELEASE_TAG, "--repo", repo,
                 "--title", "reference data",
                 "--notes", "Public-domain reference data republished from "
                            "government sources."],
                capture_output=True, text=True,
            )
            if created.returncode != 0:
                raise FredError(
                    f"Could not create release {RELEASE_TAG}: "
                    f"{created.stderr.strip()[:300]}"
                )

        up = subprocess.run(
            [gh, "release", "upload", RELEASE_TAG, str(local),
             "--repo", repo, "--clobber"],
            capture_output=True, text=True,
        )
        if up.returncode != 0:
            raise FredError(f"Could not upload {ASSET_NAME}: {up.stderr.strip()[:300]}")

        return assert_series_fresh(con.read_parquet(str(local)), con=con)


def _batched(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def load(
    observations: list[Observation],
    con: duckdb.DuckDBPyConnection | None = None,
) -> dict[str, int]:
    """Upsert into ``macro_series``. Idempotent on (series_id, obs_date, source).

    Upsert rather than insert-if-absent: FRED *revises* recent observations,
    so a value that already exists for a date may legitimately have changed
    and must be overwritten, not skipped.
    """
    con = con or storage.connect()
    if not storage.postgres_attached(con):
        raise FredError("No Postgres attached; cannot upsert macro_series.")

    def ex(sql: str) -> None:
        con.execute("CALL postgres_execute('pg', ?)", [sql])

    def q(sql: str) -> list[tuple]:
        return con.execute("SELECT * FROM postgres_query('pg', ?)", [sql]).fetchall()

    before = q("select count(*) from macro_series")[0][0]

    for batch in _batched(observations, BATCH_SIZE):
        values = ", ".join(
            "('{}', date '{}', {}, '{}')".format(
                o.series_id,
                o.obs_date.isoformat(),
                "NULL" if o.value is None else o.value,
                SOURCE,
            )
            for o in batch
        )
        ex(
            "insert into macro_series (series_id, obs_date, value, source) "
            f"values {values} "
            "on conflict (series_id, obs_date, source) do update set "
            "value = excluded.value, ingested_at = now()"
        )

    after = q("select count(*) from macro_series")[0][0]

    # The freshness assertion for this stage. Reads back what was actually
    # stored rather than trusting what was sent, which is the only version
    # that catches a silently failed upsert.
    stored = con.sql(
        "SELECT * FROM postgres_query('pg', "
        "'select series_id, obs_date, value from macro_series')"
    )
    assert_series_fresh(stored, con=con, record=False)

    return {
        "observations": len(observations),
        "series": len({o.series_id for o in observations}),
        "rows_before": before,
        "rows_after": after,
        "rows_inserted": after - before,
    }


def latest(
    con: duckdb.DuckDBPyConnection, series_ids: Iterable[str] | None = None
) -> dict[str, Observation]:
    """Most recent non-null print per series, for the digest's macro line."""
    ids = list(series_ids) if series_ids is not None else [s.series_id for s in SERIES]
    quoted = ", ".join(f"'{i}'" for i in ids)
    rows = con.execute(
        "SELECT * FROM postgres_query('pg', ?)",
        [
            "select distinct on (series_id) series_id, obs_date, value "
            f"from macro_series where value is not null and series_id in ({quoted}) "
            "order by series_id, obs_date desc"
        ],
    ).fetchall()
    return {
        r[0]: Observation(series_id=r[0], obs_date=r[1], value=r[2]) for r in rows
    }


def change_since(
    con: duckdb.DuckDBPyConnection, series_id: str, days: int
) -> Decimal | None:
    """Change in a series over ``days`` calendar days, in the series' units.

    Returns None rather than guessing when there is no print on or before the
    comparison date — a made-up baseline is worse than a blank in the digest.
    """
    rows = con.execute(
        "SELECT * FROM postgres_query('pg', ?)",
        [
            "with newest as ("
            "  select obs_date, value from macro_series "
            f"  where series_id = '{series_id}' and value is not null "
            "  order by obs_date desc limit 1) "
            "select n.value - ("
            "  select value from macro_series "
            f"  where series_id = '{series_id}' and value is not null "
            "    and obs_date <= n.obs_date - interval '%d days' "
            "  order by obs_date desc limit 1) "
            "from newest n" % days
        ],
    ).fetchall()
    return rows[0][0] if rows and rows[0][0] is not None else None
