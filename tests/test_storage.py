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


# --- publishing a public asset ------------------------------------------


def test_the_release_tag_comes_from_the_manifest_location() -> None:
    """One source of truth. A separately configured tag is a second thing to get
    wrong, and the location already says where the asset lives."""
    assert storage.release_tag(
        "https://github.com/o/r/releases/download/xbrl/x.parquet") == "xbrl"
    assert storage.release_tag(
        "https://github.com/o/r/releases/download/form5500/y.parquet"
    ) == "form5500"
    with pytest.raises(storage.PublishError, match="not a GitHub Release"):
        storage.release_tag("r2://bucket/key.parquet")


def test_a_private_backend_is_refused_before_anything_uploads(tmp_path,
                                                             monkeypatch) -> None:
    """**The licensing boundary, enforced rather than remembered.**

    Publishing an r2 or supabase partition to a public Release would be
    redistribution of vendor data -- Tiingo and Stooq personal terms forbid it,
    and a public repo's Release assets are world-readable. The manifest's
    ``backend`` column exists to make that visible; this is where it has to bite.
    """
    from marketradar import manifest

    local = tmp_path / "x.parquet"
    local.write_bytes(b"x" * 100)
    monkeypatch.setenv(storage.ENV_REPO, "o/r")
    ref = manifest.DatasetRef(dataset="prices_eod_raw", partition="2024",
                              location="r2://bucket/2024.parquet", backend="r2")
    called = {"n": 0}

    def runner(*a, **kw):
        called["n"] += 1
        raise AssertionError("a private partition reached the uploader")

    with pytest.raises(storage.PublishError, match="private"):
        storage.publish_release_asset(ref, local, runner=runner)
    assert called["n"] == 0


def test_an_empty_file_is_refused(tmp_path, monkeypatch) -> None:
    """Uploading a zero-byte asset would make the declared location resolve and
    hold nothing, which is worse than a 404: the verifier would pass."""
    from marketradar import manifest

    local = tmp_path / "empty.parquet"
    local.write_bytes(b"")
    monkeypatch.setenv(storage.ENV_REPO, "o/r")
    ref = manifest.DatasetRef(
        dataset="d", partition="p",
        location="https://github.com/o/r/releases/download/t/empty.parquet",
        backend="github_release")
    with pytest.raises(storage.PublishError, match="missing or empty"):
        storage.publish_release_asset(ref, local)


def test_the_release_is_created_when_it_does_not_exist(tmp_path,
                                                      monkeypatch) -> None:
    """The `xbrl` and `form5500` releases did not exist at all, which is why 33
    assets pointed at nothing. An upload that assumes the release is there fails
    in a way that looks like a permissions problem."""
    from marketradar import manifest

    local = tmp_path / "x.parquet"
    local.write_bytes(b"x" * 100)
    monkeypatch.setenv(storage.ENV_REPO, "o/r")
    ref = manifest.DatasetRef(
        dataset="d", partition="p",
        location="https://github.com/o/r/releases/download/newtag/x.parquet",
        backend="github_release")
    seen: list[list[str]] = []

    class Result:
        def __init__(self, code: int) -> None:
            self.returncode = code
            self.stderr = ""
            self.stdout = ""

    def runner(cmd, **kw):
        seen.append(cmd)
        if cmd[1:3] == ["release", "view"]:
            return Result(1)          # not found
        return Result(0)

    import shutil as _shutil

    monkeypatch.setattr(_shutil, "which", lambda name: "/usr/bin/gh")
    assert storage.publish_release_asset(ref, local, runner=runner) == "newtag"
    verbs = [c[1:3] for c in seen]
    assert ["release", "view"] in verbs
    assert ["release", "create"] in verbs
    assert ["release", "upload"] in verbs
    # --clobber, so republishing a corrected partition replaces rather than
    # silently keeping the old bytes under the same URL.
    upload = next(c for c in seen if c[1:3] == ["release", "upload"])
    assert "--clobber" in upload
