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


# --- a declared location nobody checks ----------------------------------


def test_a_missing_release_asset_is_missing_not_ok(monkeypatch) -> None:
    """**The failure this exists for, and it was real.**

    Found 2026-09-12: all 30 ``xbrl_fundamentals`` partitions, three
    ``form5500_sponsors`` years and ``sec_filers/all`` declared Release URLs that
    returned 404. Nothing had ever failed, because every consumer read a local
    cache in ``.cache/`` instead. This module exists so that nothing hardcodes a
    location, and for weeks nothing checked that the locations resolved.
    """
    import httpx

    ref = manifest.DatasetRef(dataset="d", partition="p",
                              location="https://example.test/a.parquet",
                              backend="github_release")

    def gone(*a, **kw):
        class R:
            status_code = 404
            headers: dict = {}
        return R()

    monkeypatch.setattr(httpx, "get", gone)
    monkeypatch.setattr(manifest, "PROPAGATION_WAIT", 0.0)
    got = manifest.verify(ref)
    assert got.status == manifest.MISSING
    assert got.broken
    assert "404" in got.detail


def test_a_present_asset_is_ok_on_a_range_request(monkeypatch) -> None:
    """One byte, not a download: 61 locations should not cost megabytes, and the
    question is whether the asset is there rather than whether it parses."""
    import httpx

    seen: dict = {}

    def present(url, **kw):
        seen.update(kw.get("headers") or {})

        class R:
            status_code = 206
            headers: dict = {}
        return R()

    monkeypatch.setattr(httpx, "get", present)
    ref = manifest.DatasetRef(dataset="d", partition="p",
                              location="https://example.test/a.parquet",
                              backend="github_release")
    assert manifest.verify(ref).status == manifest.OK
    assert seen.get("Range") == "bytes=0-0"


def test_a_propagation_404_is_retried_before_being_called_missing(
    monkeypatch,
) -> None:
    """Measured 2026-09-12: GitHub answers 404 for under a minute after an upload
    reports ``state: uploaded``. Without the retry this reported five partitions
    missing *immediately after publishing them* -- and a check that cries wolf
    exactly when somebody is watching is a check that gets ignored.
    """
    import httpx

    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1

        class R:
            status_code = 404 if calls["n"] == 1 else 206
            headers: dict = {}
        return R()

    monkeypatch.setattr(httpx, "get", flaky)
    monkeypatch.setattr(manifest, "PROPAGATION_WAIT", 0.0)
    ref = manifest.DatasetRef(dataset="d", partition="p",
                              location="https://example.test/a.parquet",
                              backend="github_release")
    assert manifest.verify(ref).status == manifest.OK
    assert calls["n"] == 2


def test_a_non_404_failure_is_not_retried(monkeypatch) -> None:
    """A 403 is not propagation. Retrying it wastes a request to learn the same
    thing, which is the Cerebras-402 lesson in another place."""
    import httpx

    calls = {"n": 0}

    def forbidden(*a, **kw):
        calls["n"] += 1

        class R:
            status_code = 403
            headers: dict = {}
        return R()

    monkeypatch.setattr(httpx, "get", forbidden)
    monkeypatch.setattr(manifest, "PROPAGATION_WAIT", 0.0)
    ref = manifest.DatasetRef(dataset="d", partition="p",
                              location="https://example.test/a.parquet",
                              backend="github_release")
    assert manifest.verify(ref).status == manifest.MISSING
    assert calls["n"] == 1


def test_unverifiable_is_not_the_same_as_ok(monkeypatch) -> None:
    """**The distinction that stops this reproducing the bug one level up.**

    An R2 partition on a machine with no credentials cannot be checked. Reporting
    that as ``ok`` would be a green check that means "we did not look", which is
    precisely the shape of the failure being fixed.
    """
    for name in ("MR_R2_ACCOUNT_ID", "MR_R2_ACCESS_KEY_ID",
                 "MR_R2_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(name, raising=False)
    ref = manifest.DatasetRef(dataset="prices_eod_raw", partition="2024",
                              location="r2://bucket/x.parquet", backend="r2")
    got = manifest.verify(ref)
    assert got.status == manifest.UNVERIFIABLE
    assert not got.broken
    assert manifest.UNVERIFIABLE != manifest.OK
    text = "\n".join(manifest.verify_lines([got]))
    assert "not** the same" in text or "not the same" in text


def test_a_vendor_api_entry_is_skipped_rather_than_probed() -> None:
    """A ``vendor_api`` row records where an upstream endpoint lives so a source
    module can honour the no-hardcoded-URL rule. Probing it would mean calling a
    vendor to check the manifest, which is not a thing to do on a schedule."""
    ref = manifest.DatasetRef(dataset="tiingo_api", partition="prices",
                              location="https://api.tiingo.test/x",
                              backend="vendor_api")
    got = manifest.verify(ref)
    assert got.status == manifest.SKIPPED
    assert not got.broken


def test_every_backend_in_the_manifest_has_a_check() -> None:
    """Otherwise a new backend would silently report ``unverifiable`` forever,
    which looks like a credentials problem and is a missing implementation."""
    backends = {r.backend for r in manifest.datasets()}
    handled = {"github_release", "r2", "supabase", "local", "vendor_api"}
    assert backends <= handled, f"no verify branch for {backends - handled}"


def test_the_report_leads_with_what_is_broken() -> None:
    results = [
        manifest.Verification("a", "1", "github_release", "u", manifest.OK),
        manifest.Verification("b", "2", "github_release", "u",
                              manifest.MISSING, "HTTP 404"),
    ]
    text = "\n".join(manifest.verify_lines(results))
    assert "hold nothing" in text
    assert "b/2" in text
    assert "cache will mask it" in text
