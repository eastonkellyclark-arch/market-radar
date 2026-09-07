"""SEC ``company_tickers.json`` — the CIK ↔ ticker map that seeds identity.

Public-domain government data, so unlike prices this goes to a **GitHub
Release**, not R2. That is the licensing split in CLAUDE.md running in the
public direction for the first time with real data: vendor-derived Parquet
stays in a private bucket, government data may be republished.

SEC requires a descriptive User-Agent carrying a real contact address and
blocks traffic without one, so every request sends ``MR_SEC_USER_AGENT``.
There is one request here — this is a single small file, not a per-CIK loop.

No literal URL in this module; the endpoint resolves through
``manifest.get('sec_files', 'company_tickers')``.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Final

import duckdb
import httpx

from marketradar import manifest, storage
from marketradar.entities.resolve import normalize, normalize_cik, normalize_ticker
from marketradar.freshness import assert_fresh

log = logging.getLogger(__name__)

ENV_USER_AGENT: Final[str] = "MR_SEC_USER_AGENT"
DATASET: Final[str] = "sec_company_tickers"
SOURCE: Final[str] = "sec_company_tickers"

#: Release the public-domain reference data is published under.
RELEASE_TAG: Final[str] = "reference-data"
ASSET_NAME: Final[str] = "sec_company_tickers.parquet"

#: SEC lists roughly 10,000 filers with listed securities. A load returning
#: far fewer means the file changed shape or the request was throttled.
MIN_ROWS: Final[int] = 5_000

REQUEST_TIMEOUT: Final[float] = 60.0


#: Rows per multi-row INSERT. Large enough that the round trip stops
#: dominating, small enough that one bad row does not fail 10,000 others.
BATCH_SIZE: Final[int] = 500


class SecError(RuntimeError):
    """SEC could not be reached, or answered with something unusable."""


def _q(value: str) -> str:
    """Escape a single-quoted SQL literal."""
    return value.replace("'", "''")


def _batched(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


@dataclass(frozen=True, slots=True)
class Filer:
    cik: str
    ticker: str
    name: str
    normalized: str


def user_agent() -> str:
    """SEC blocks requests without a descriptive UA and a real contact."""
    ua = os.environ.get(ENV_USER_AGENT, "").strip()
    if not ua or "dummy" in ua.lower() or "example.com" in ua.lower():
        raise SecError(
            f"{ENV_USER_AGENT} is not set to a real contact address. SEC blocks "
            "traffic without a descriptive User-Agent and it must reach a human."
        )
    if "@" not in ua:
        raise SecError(
            f"{ENV_USER_AGENT}={ua!r} has no email address in it. SEC asks for a "
            "contact, not just a product name."
        )
    return ua


def fetch() -> list[Filer]:
    """Download and shape the CIK ↔ ticker map.

    One request. Prefer bulk files over per-CIK loops — one download beats
    8,000 calls, and stays far under the 10 req/sec ceiling by construction.
    """
    url = manifest.get("sec_files", "company_tickers").location
    try:
        resp = httpx.get(
            url,
            headers={"User-Agent": user_agent(), "Accept-Encoding": "gzip, deflate"},
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 403:
            raise SecError(
                "SEC returned 403. This is almost always the User-Agent: it must "
                f"be descriptive and carry a real contact address. Current "
                f"{ENV_USER_AGENT}={os.environ.get(ENV_USER_AGENT, '')!r}"
            ) from exc
        raise SecError(f"SEC returned HTTP {exc.response.status_code}") from exc
    except httpx.HTTPError as exc:
        raise SecError(f"Could not reach SEC: {exc}") from exc

    try:
        payload = resp.json()
    except ValueError as exc:
        raise SecError("SEC response was not JSON") from exc

    return shape(payload)


def shape(payload: Any) -> list[Filer]:
    """Turn SEC's positional-dict JSON into rows.

    The file is ``{"0": {"cik_str": 320193, "ticker": "AAPL", "title": ...}}``
    — one entry per **(cik, ticker) pair**, so a company with share classes
    appears more than once. That repetition is the data, not a duplicate, and
    is why the ticker mapping is its own table.
    """
    entries = payload.values() if isinstance(payload, dict) else payload
    out: list[Filer] = []
    seen: set[tuple[str, str]] = set()

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        cik = normalize_cik(entry.get("cik_str"))
        ticker = normalize_ticker(entry.get("ticker"))
        name = (entry.get("title") or "").strip()
        if not cik or not ticker or not name:
            continue
        key = (cik, ticker)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            Filer(cik=cik, ticker=ticker, name=name, normalized=normalize(name))
        )
    return out


def _write_parquet(filers: list[Filer], target: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    ingested = datetime.now(timezone.utc)
    schema = pa.schema(
        [
            ("cik", pa.string()),
            ("ticker", pa.string()),
            ("name", pa.string()),
            ("normalized", pa.string()),
            ("source", pa.string()),
            ("ingested_at", pa.timestamp("us", tz="UTC")),
        ]
    )
    table = pa.table(
        {
            "cik": [f.cik for f in filers],
            "ticker": [f.ticker for f in filers],
            "name": [f.name for f in filers],
            "normalized": [f.normalized for f in filers],
            "source": [SOURCE] * len(filers),
            "ingested_at": [ingested] * len(filers),
        },
        schema=schema,
    )
    pq.write_table(table, target, compression="zstd")


def _gh() -> str:
    import shutil

    found = shutil.which("gh") or shutil.which("gh", path=r"C:\Program Files\GitHub CLI")
    if not found:
        raise SecError("gh CLI not found; needed to publish the GitHub Release.")
    return found


def publish(filers: list[Filer], *, con: duckdb.DuckDBPyConnection | None = None) -> Any:
    """Write Parquet, upload as a Release asset, assert freshness.

    Refuses a private backend — the mirror image of the guard in the Tiingo
    loader. Government data on R2 is not a licensing problem, but it is a
    mistake: it costs storage and hides public data behind credentials.
    """
    ref = manifest.get(DATASET, "all")
    if ref.backend != "github_release":
        raise SecError(
            f"{DATASET}/all resolves to backend {ref.backend!r}. SEC data is "
            "public domain and belongs in a GitHub Release, not a private "
            "bucket. See the licensing rule in CLAUDE.md."
        )

    repo = os.environ.get("MR_GITHUB_REPO")
    if not repo:
        raise SecError("MR_GITHUB_REPO is unset; needed to publish the Release.")

    con = con or storage.connect()
    gh = _gh()

    with tempfile.TemporaryDirectory(prefix="mr-sec-") as tmp:
        local = Path(tmp) / ASSET_NAME
        _write_parquet(filers, local)

        seen = subprocess.run(
            [gh, "release", "view", RELEASE_TAG, "--repo", repo],
            capture_output=True, text=True,
        )
        if seen.returncode != 0:
            created = subprocess.run(
                [gh, "release", "create", RELEASE_TAG, "--repo", repo,
                 "--title", "reference data",
                 "--notes", "Public-domain reference data republished from SEC "
                            "and other government sources. Regenerated by "
                            "`mr sec-tickers`."],
                capture_output=True, text=True,
            )
            if created.returncode != 0:
                raise SecError(
                    f"Could not create release {RELEASE_TAG}: "
                    f"{created.stderr.strip()[:300]}"
                )

        up = subprocess.run(
            [gh, "release", "upload", RELEASE_TAG, str(local),
             "--repo", repo, "--clobber"],
            capture_output=True, text=True,
        )
        if up.returncode != 0:
            raise SecError(
                f"Could not upload {ASSET_NAME}: {up.stderr.strip()[:300]}"
            )

        rel = con.read_parquet(str(local))
        observed = assert_fresh(
            DATASET,
            rel,
            partition="all",
            min_rows=MIN_ROWS,
            date_column=None,  # a snapshot, no time dimension
            expect_cols=("cik", "ticker", "name", "normalized"),
        )

    manifest.record_stats(observed, con=con)
    return observed


def load(filers: list[Filer], con: duckdb.DuckDBPyConnection | None = None) -> dict[str, int]:
    """Upsert into ``companies`` and ``company_tickers``.

    Idempotent on CIK. Running twice changes nothing: companies conflict on
    the partial unique index over cik, ticker pairs conflict on their natural
    key and only bump ``last_seen``.
    """
    con = con or storage.connect()
    if not storage.postgres_attached(con):
        raise SecError("No Postgres attached; cannot upsert companies.")

    by_cik: dict[str, Filer] = {}
    for f in filers:
        by_cik.setdefault(f.cik, f)

    def ex(sql: str) -> None:
        con.execute("CALL postgres_execute('pg', ?)", [sql])

    def q(sql: str) -> list[tuple]:
        return con.execute("SELECT * FROM postgres_query('pg', ?)", [sql]).fetchall()

    before_companies = q("select count(*) from companies")[0][0]
    before_tickers = q("select count(*) from company_tickers")[0][0]

    # Batched, not row-by-row. The first version sent one statement per row:
    # ~10k companies plus ~12k ticker pairs is 22,000 network round trips to
    # Supabase, which did not finish inside ten minutes. Multi-row VALUES
    # turns that into a few dozen statements.
    for batch in _batched(list(by_cik.values()), BATCH_SIZE):
        values = ", ".join(
            "('{}', '{}', '{}', '{}', true)".format(
                f.cik, _q(f.ticker), _q(f.name), _q(f.normalized)
            )
            for f in batch
        )
        ex(
            "insert into companies (cik, ticker, name, normalized, is_public) "
            f"values {values} "
            "on conflict (cik) where cik is not null do update set "
            "name = excluded.name, normalized = excluded.normalized, "
            "is_public = true, updated_at = now()"
        )

    for batch in _batched(filers, BATCH_SIZE):
        pairs = ", ".join(
            "('{}', '{}')".format(f.cik, _q(f.ticker)) for f in batch
        )
        ex(
            "insert into company_tickers (company_id, ticker, source) "
            f"select c.id, v.ticker, '{SOURCE}' "
            f"from (values {pairs}) as v(cik, ticker) "
            "join companies c on c.cik = v.cik "
            "on conflict (company_id, ticker, source) do update set "
            "last_seen = current_date"
        )

    after_companies = q("select count(*) from companies")[0][0]
    after_tickers = q("select count(*) from company_tickers")[0][0]

    return {
        "filers": len(filers),
        "distinct_ciks": len(by_cik),
        "companies_before": before_companies,
        "companies_after": after_companies,
        "companies_inserted": after_companies - before_companies,
        "tickers_before": before_tickers,
        "tickers_after": after_tickers,
        "tickers_inserted": after_tickers - before_tickers,
    }
