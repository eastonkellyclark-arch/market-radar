from __future__ import annotations

from pathlib import Path

import pytest

from marketradar import manifest
from marketradar.manifest import ManifestError, UnknownDatasetError


def _write(tmp_path: Path, body: str, monkeypatch) -> Path:
    path = tmp_path / "manifest.toml"
    path.write_text(body, encoding="utf-8")
    monkeypatch.setenv(manifest.OVERRIDE_ENV, str(path))
    manifest.clear_cache()
    return path


def test_get_resolves_a_location(local_manifest: Path) -> None:
    ref = manifest.get("prices_eod_raw", "2026")
    assert ref.dataset == "prices_eod_raw"
    assert ref.partition == "2026"
    assert ref.backend == "local"
    assert ref.location.endswith("prices_2026.parquet")


def test_unknown_dataset_raises_rather_than_returning_none(local_manifest: Path) -> None:
    """Silently returning nothing is the failure mode this project prevents."""
    with pytest.raises(UnknownDatasetError, match="No dataset 'nope'"):
        manifest.get("nope", "2026")


def test_unknown_partition_raises_and_lists_what_exists(local_manifest: Path) -> None:
    with pytest.raises(UnknownDatasetError, match="Known partitions: 2026"):
        manifest.get("prices_eod_raw", "1999")


def test_partition_lookup_accepts_non_string(local_manifest: Path) -> None:
    """Callers hold years as ints; the manifest keys them as strings."""
    assert manifest.get("prices_eod_raw", 2026).partition == "2026"


def test_datasets_lists_everything_sorted(tmp_path: Path, monkeypatch) -> None:
    _write(
        tmp_path,
        '[b]\nall = { location = "x", backend = "local" }\n'
        '[a]\n"2026" = { location = "y", backend = "r2" }\n'
        '"2025" = { location = "z", backend = "r2" }\n',
        monkeypatch,
    )
    got = [(r.dataset, r.partition) for r in manifest.datasets()]
    assert got == [("a", "2025"), ("a", "2026"), ("b", "all")]


def test_private_backends_are_flagged(tmp_path: Path, monkeypatch) -> None:
    """The R2-vs-Releases split is a licensing boundary, so it is queryable."""
    _write(
        tmp_path,
        '[vendor]\nall = { location = "r2://b/k", backend = "r2" }\n'
        '[gov]\nall = { location = "https://example.invalid/x", backend = "github_release" }\n',
        monkeypatch,
    )
    assert manifest.get("vendor", "all").is_private is True
    assert manifest.get("gov", "all").is_private is False


def test_rejects_unknown_backend(tmp_path: Path, monkeypatch) -> None:
    _write(tmp_path, '[d]\nall = { location = "x", backend = "dropbox" }\n', monkeypatch)
    with pytest.raises(ManifestError, match="unknown backend 'dropbox'"):
        manifest.get("d", "all")


def test_rejects_missing_location(tmp_path: Path, monkeypatch) -> None:
    _write(tmp_path, '[d]\nall = { backend = "r2" }\n', monkeypatch)
    with pytest.raises(ManifestError, match="missing required key 'location'"):
        manifest.get("d", "all")


def test_rejects_malformed_toml(tmp_path: Path, monkeypatch) -> None:
    _write(tmp_path, "[d\nbroken", monkeypatch)
    with pytest.raises(ManifestError, match="Malformed manifest"):
        manifest.get("d", "all")


def test_missing_file_raises_clearly(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(manifest.OVERRIDE_ENV, str(tmp_path / "absent.toml"))
    manifest.clear_cache()
    with pytest.raises(ManifestError, match="Cannot read manifest"):
        manifest.get("d", "all")


def test_cache_invalidates_on_rewrite(tmp_path: Path, monkeypatch) -> None:
    import os
    import time

    path = _write(tmp_path, '[d]\nall = { location = "one", backend = "local" }\n', monkeypatch)
    assert manifest.get("d", "all").location == "one"

    time.sleep(0.01)
    path.write_text('[d]\nall = { location = "two", backend = "local" }\n', encoding="utf-8")
    os.utime(path, (time.time() + 1, time.time() + 1))
    assert manifest.get("d", "all").location == "two"


def test_repo_manifest_is_valid_and_respects_the_licensing_split() -> None:
    """The committed manifest must parse, and vendor data must not be public."""
    manifest.clear_cache()
    refs = manifest.datasets()
    assert refs, "the committed manifest.toml defines no datasets"

    for ref in refs:
        if ref.dataset.startswith(("prices_", "corporate_actions")):
            assert ref.is_private, (
                f"{ref.dataset}/{ref.partition} is vendor-derived but sits on "
                f"{ref.backend!r}, which is publicly readable. See CLAUDE.md."
            )


def test_record_stats_is_a_noop_without_postgres(con) -> None:
    """Local dev without Supabase is a hard requirement."""
    from marketradar.manifest import Freshness

    obs = Freshness("prices_eod_raw", "2026", 12_000, None)
    assert manifest.record_stats(obs, con=con) is False
    assert manifest.latest_stats("prices_eod_raw", "2026", con=con) is None
