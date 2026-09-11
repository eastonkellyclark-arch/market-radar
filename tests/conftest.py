"""Shared fixtures.

Everything here works with no network, no credentials, and no Supabase.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path

import duckdb
import pytest

from marketradar import manifest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Credentials that would change behaviour if they happened to be set in the
# developer's shell. Cleared for every test so the suite is deterministic.
_LEAKY_ENV = (
    "MR_POSTGRES_DSN",
    "MR_R2_ACCOUNT_ID",
    "MR_R2_ACCESS_KEY_ID",
    "MR_R2_SECRET_ACCESS_KEY",
    "MR_MANIFEST_OVERRIDE",
)


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip credentials, and stop the CLI from putting them back.

    Clearing the environment is not enough on its own: ``mr`` calls
    ``load_dotenv()``, which reads the developer's real ``.env`` off disk and
    repopulates everything this fixture just removed. That let a CLI test
    fire the real selftest at R2 and GitHub. Neutering the loader is what
    actually makes "no network, no credentials" true.
    """
    from marketradar import cli

    for name in _LEAKY_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cli, "load_dotenv", lambda path=None: 0)
    manifest.clear_cache()


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    """A provably offline connection: httpfs is never loaded."""
    from marketradar import storage

    return storage.connect(enable_http=False, attach_postgres=False)


@pytest.fixture
def prices_parquet(tmp_path: Path, con: duckdb.DuckDBPyConnection) -> Path:
    """A small prices partition shaped like the real thing.

    Prices are DECIMAL(18,6), not cents: the sub-$1 band is a headline feature
    and $0.000200 has to survive a round trip.
    """
    target = tmp_path / "prices_2026.parquet"
    today = date.today()
    con.execute(
        """
        CREATE OR REPLACE TABLE t AS
        SELECT
            'T' || lpad((i % 50)::VARCHAR, 4, '0')            AS ticker,
            (CAST($today AS DATE) - ((i // 50))::INTEGER)     AS date,
            (0.000200 + i * 0.000001)::DECIMAL(18,6)          AS open,
            (0.000300 + i * 0.000001)::DECIMAL(18,6)          AS high,
            (0.000100 + i * 0.000001)::DECIMAL(18,6)          AS low,
            (0.000250 + i * 0.000001)::DECIMAL(18,6)          AS close,
            (1000 + i)::BIGINT                                AS volume,
            'nasdaq'                                          AS exchange,
            'stock'                                           AS security_type,
            'test'                                            AS source
        FROM range(500) AS r(i)
        """,
        {"today": today},
    )
    con.execute("COPY t TO ? (FORMAT parquet)", [str(target)])
    return target


@pytest.fixture
def local_manifest(tmp_path: Path, prices_parquet: Path, monkeypatch) -> Path:
    """A manifest pointing at the local fixture, via the override env var."""
    path = tmp_path / "manifest.toml"
    location = prices_parquet.as_posix()
    path.write_text(
        "[prices_eod_raw]\n"
        f'2026 = {{ location = "{location}", backend = "local" }}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv(manifest.OVERRIDE_ENV, str(path))
    manifest.clear_cache()
    return path


@pytest.fixture
def yesterday() -> date:
    return date.today() - timedelta(days=1)


# --- the dashboard's fully loaded system --------------------------------
#
# Shared because three test modules need the same thing and each one guessing
# at it went wrong the same way: a thin placeholder row does not render, it
# raises KeyError, and a body check that dies in its fixture proves nothing.
#
# "Fully loaded" is a specific claim -- it is what the panel table in
# docs/build-spec.md says its `state` column describes -- so it lives in one
# place and every panel here resolves *live* off it. A field is present only
# because some probe or body renderer reads it.

#: Eleven years of partitions at a realistic row count. A sweep-only partition
#: is tens of thousands of rows, and half these panels legitimately read
#: `waiting` off one.
DEEP_PRICES: dict[str, dict[str, object]] = {
    str(year): {"rows": 1_500_000, "max_date": "2026-09-08"}
    for year in range(2016, 2027)
}

_PARTICIPANT_SERIES = [{"year": 2022, "participants": 120},
                       {"year": 2023, "participants": 118},
                       {"year": 2024, "participants": 115}]

#: One sponsor, with every field the private and mature bodies read.
PRIVATE_ROW: dict[str, object] = {
    "ein": "111111111", "sponsor_name": "OLD MACHINE SHOP INC", "state": "TX",
    "naics": "332710", "plans": 1, "participants_max": 115,
    "participants_sum": 115, "is_dfe": False, "trend": "flat",
    "status": "filing", "pct_change": -0.04, "first_year": 2022,
    "last_year": 2024, "years_filed": 3, "pending_years": 0, "gap_years": 0,
    "series": _PARTICIPANT_SERIES,
}

MATURE_ROW: dict[str, object] = dict(
    PRIVATE_ROW, city="AUSTIN", oldest_plan_eff=date(1979, 1, 1),
    age_years=47.7, participants_last=148, active_last=115, active_sum=115,
)

PRIVATE_STATS: dict[str, object] = {
    "plan_year": 2024, "sponsors": 4, "private": 3, "dfe": 1, "listed": 0,
    "ambiguous": 0, "completeness": "",
}

#: ``years`` carries three, because the mature probe wants two *complete* plan
#: years before it will call a trend a trend.
MATURE_STATS: dict[str, object] = {
    "plan_year": 2024, "sponsors": 4, "candidates": 1, "completeness": "",
    "min_age": 40, "max_participants": 500, "years": (2022, 2023, 2024),
}


def loaded_context(**overrides):
    """A dashboard Context with every source present and nothing empty.

    Not a pytest fixture: the spec test resolves panels against it at module
    level to compare them with the doc, and a fixture cannot be read from a
    helper the way this is.
    """
    from datetime import datetime, timezone

    from marketradar.dashboard import shell

    base: dict[str, object] = dict(
        generated_at=datetime(2026, 9, 10, 21, 0, tzinfo=timezone.utc),
        postgres=True,
        macro={"DGS10": "2026-09-03"},
        prices=DEEP_PRICES,
        entities={"companies": 8_005, "tickers": 10_412},
        filings={"edgar_filing": {"count": 172,
                                 "latest": "2026-09-08 20:02:00"}},
        recent_filings=[
            {"form_type": "8-K", "company": "ACME CORP", "cik": "0000001",
             "filed_at": "2026-09-08 14:00:00",
             "accession": "0000001-26-000001"},
            {"form_type": "4", "company": "BETA INC", "cik": "0000002",
             "filed_at": "2026-09-08 13:00:00",
             "accession": "0000002-26-000002"},
        ],
        clusters=[
            {"role": "insider", "symbol": "BIG", "issuer_cik": "0000001",
             "issuer_name": "ACME CORP", "n_buyers": 3, "value": 250_000,
             "first": "2026-09-02", "last": "2026-09-04", "buyers": [],
             "fund_like": False, "fund_why": "", "planned_buys": 0},
            {"role": "ten_percent", "symbol": "DWN", "issuer_cik": "0000002",
             "issuer_name": "BETA INC", "n_buyers": 2, "value": 4_000_000,
             "first": "2026-09-03", "last": "2026-09-03", "buyers": [],
             "fund_like": True, "fund_why": "name matches a fund pattern",
             "planned_buys": 0},
        ],
        deals=[
            {"deal_type": "merger", "agree": True, "value_usd": 1_200_000_000,
             "value_basis": "stated", "value_text": "$1.2 billion",
             "target_financials": "included", "exhibit_signal": True,
             "text_signal": "merger", "filer_role": "acquirer",
             "counterparty": "TARGET CO", "company": "ACME CORP",
             "consideration": "cash", "items": "1.01", "filed": "2026-09-08"},
            {"deal_type": "spac", "agree": False, "value_usd": None,
             "value_basis": "none", "value_text": None,
             "target_financials": "rule_305_promised", "exhibit_signal": False,
             "text_signal": None, "filer_role": "target",
             "counterparty": None, "company": "BETA INC",
             "consideration": "stock", "items": "2.01", "filed": "2026-09-07"},
        ],
        outcomes=[
            {"study": "8-K deals", "slice": "all", "horizon": 1, "n": 1200,
             "median_ret": 0.011, "median_excess": -0.0236,
             "mean_excess": -0.02, "win_rate": 0.48, "median_run_up": 0.004,
             "n_suspect": 3, "events": 1200, "priced": 800},
        ],
        review=[
            {"sponsor_name": "OLD MACHINE SHOP INC", "ein": "111111111",
             "matched_name": "OLD MACHINE SHOP CORP", "matched_cik": "0000003",
             "naics": "332710", "state": "TX", "candidates": 2,
             "match_basis": "normalized name", "status": "pending"},
        ],
        review_counts={"pending": 1, "confirmed": 0, "rejected": 0},
    )
    base.update(overrides)
    ctx = shell.Context(**base)
    # render() stashes these for the two private probes. Resolving outside a
    # render has to do the same, or both panels read as waiting.
    object.__setattr__(ctx, "_private_stats", PRIVATE_STATS)
    object.__setattr__(ctx, "_mature_stats", MATURE_STATS)
    return ctx


def panel_slot(page: str, pid: str) -> str:
    """What one panel renders into its ``<div class="slot">``.

    Anchored on ``id="panel-<id>"``, which the article carries and nothing
    else does -- **not** on ``data-panel="<id>"``, which appears twice: once
    on the sidebar link and once on the article, with the whole nav ahead of
    the whole column.

    That is not a hypothetical. ``test_a_live_panel_always_has_a_body`` sliced
    on ``data-panel`` and was written when it appeared once. Adding the
    sidebar made every lookup land in the nav and run on to the first
    ``</article>`` in the page, so for nineteen of the twenty panels the test
    was inspecting the *health* panel and passing on it. Nothing failed; the
    check just stopped being a check -- which is the same way the page itself
    went dead under 659 green assertions.

    So the anchor is asserted unique. A page change that duplicates it fails
    here rather than quietly widening the slice again.
    """
    anchor = f'id="panel-{pid}"'
    found = page.count(anchor)
    assert found == 1, (
        f"{anchor} appears {found} times in the page, so this cannot say "
        f"which one is {pid}'s panel. Every slot lookup in the suite depends "
        "on it being unique."
    )
    article = page.split(anchor, 1)[1].split("</article>", 1)[0]
    assert '<div class="slot">' in article, f"{pid} rendered no slot at all"
    return article.split('<div class="slot">', 1)[1].rsplit("</div>", 1)[0]
