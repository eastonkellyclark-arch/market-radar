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
