"""Storage must work with no network and no credentials."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from marketradar import storage
from marketradar.manifest import DatasetRef, UnknownDatasetError
from marketradar.storage import StorageError


def test_connect_works_offline() -> None:
    con = storage.connect(enable_http=False, attach_postgres=False)
    assert con.execute("SELECT 42").fetchone()[0] == 42


def test_connect_does_not_attach_postgres_by_default(con) -> None:
    assert storage.postgres_attached(con) is False


def test_attach_postgres_without_dsn_raises_clearly() -> None:
    with pytest.raises(StorageError, match="MR_POSTGRES_DSN is not set"):
        storage.connect(enable_http=False, attach_postgres=True)


def test_read_dataset_round_trips_through_the_manifest(
    local_manifest: Path, con: duckdb.DuckDBPyConnection
) -> None:
    rel = storage.read_dataset("prices_eod_raw", "2026", con=con)
    assert rel.query("rel", "SELECT count(*) FROM rel").fetchone()[0] == 500
    assert "ticker" in rel.columns and "close" in rel.columns


def test_read_dataset_rejects_unknown_dataset(local_manifest: Path, con) -> None:
    with pytest.raises(UnknownDatasetError):
        storage.read_dataset("not_a_dataset", "2026", con=con)


def test_read_ref_reports_a_bad_location_loudly(con) -> None:
    ref = DatasetRef("x", "all", "/nope/missing.parquet", "local")
    with pytest.raises(StorageError, match="Could not read x/all"):
        storage.read_ref(ref, con=con)


def test_supabase_backend_is_not_read_as_parquet(con) -> None:
    ref = DatasetRef("corporate_actions", "all", "corporate_actions", "supabase")
    with pytest.raises(StorageError, match="lives in Postgres, not Parquet"):
        storage.read_ref(ref, con=con)


def test_decimal_precision_survives_parquet(local_manifest: Path, con) -> None:
    """DECIMAL(18,6) through a real Parquet round trip, not just in memory."""
    rel = storage.read_dataset("prices_eod_raw", "2026", con=con)
    dtype = dict(zip(rel.columns, rel.types))["close"]
    assert "DECIMAL(18,6)" in str(dtype).upper()


def test_describe_connection_reports_capabilities(con) -> None:
    caps = storage.describe_connection(con)
    assert caps["postgres"] is False
    assert caps["r2_configured"] is False
    assert caps["httpfs"] is False  # enable_http=False
    assert caps["duckdb_version"]


def test_r2_secret_is_registered_when_credentials_exist(monkeypatch) -> None:
    """Proves the R2 path is wired; the credentials are deliberately fake.

    T1 established that DuckDB resolves r2:// to the right endpoint and
    issues a signed request. Only the authenticated round trip is untested,
    and that needs real credentials.
    """
    monkeypatch.setenv(storage.ENV_R2_ACCOUNT, "dummy-account")
    monkeypatch.setenv(storage.ENV_R2_KEY_ID, "dummy-key")
    monkeypatch.setenv(storage.ENV_R2_SECRET, "dummy-secret")

    con = storage.connect(enable_http=True, attach_postgres=False)
    names = [r[0] for r in con.execute("SELECT name FROM duckdb_secrets()").fetchall()]
    assert "mr_r2" in names
