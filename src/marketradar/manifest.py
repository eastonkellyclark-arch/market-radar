"""Dataset registry — the only place that knows where data lives.

Two kinds of fact, deliberately kept apart:

*Location* is configuration. Where a dataset physically sits. Changes rarely,
deliberately, by a human, and belongs in git where it is diffable and
revertable. Lives in ``manifest.toml``.

*Freshness* is observation. Row counts and max dates, written by loaders every
night and read back for diagnostics. Belongs in Postgres, appended rather than
overwritten so the history stays queryable. Lives in ``dataset_stats``.

Nothing outside this module reads either one directly, and nothing anywhere in
the codebase hardcodes a data URL. Moving a dataset is an edit to
``manifest.toml`` and nothing else.

This module imports only the standard library. It must stay importable with no
network, no credentials, and no Supabase.
"""

from __future__ import annotations

import os
import time
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Final, Literal

Backend = Literal["r2", "github_release", "supabase", "local", "vendor_api"]

VALID_BACKENDS: Final[frozenset[str]] = frozenset(
    {"r2", "github_release", "supabase", "local", "vendor_api"}
)

#: Backends that are not storage at all. A ``vendor_api`` entry records where
#: an upstream API lives, so that source modules can honour the
#: no-hardcoded-URL rule without pretending an endpoint is a dataset. It is
#: never read as Parquet.
NON_STORAGE_BACKENDS: Final[frozenset[str]] = frozenset({"vendor_api"})

# Backends whose contents are vendor-derived and must never be published
# anywhere public. See the licensing rule in CLAUDE.md.
PRIVATE_BACKENDS: Final[frozenset[str]] = frozenset({"r2", "supabase"})

MANIFEST_FILENAME: Final[str] = "manifest.toml"
OVERRIDE_ENV: Final[str] = "MR_MANIFEST_OVERRIDE"


class ManifestError(RuntimeError):
    """Base class for manifest failures."""


class UnknownDatasetError(ManifestError):
    """Asked for a dataset or partition the manifest does not define.

    Raised rather than returning ``None`` on purpose. A caller that silently
    receives nothing is exactly the failure mode this project exists to
    prevent.
    """


@dataclass(frozen=True, slots=True)
class DatasetRef:
    """Where one partition of one dataset lives."""

    dataset: str
    partition: str
    location: str
    backend: Backend

    @property
    def is_private(self) -> bool:
        """True when this data must not be published to a public location."""
        return self.backend in PRIVATE_BACKENDS


@dataclass(frozen=True, slots=True)
class Freshness:
    """An observation about a dataset partition at a point in time."""

    dataset: str
    partition: str
    row_count: int
    max_date: date | None


def manifest_path() -> Path:
    """Locate ``manifest.toml``.

    Resolution order: the ``MR_MANIFEST_OVERRIDE`` env var (tests and local
    dev), then the nearest ``manifest.toml`` at or above the working
    directory, then the one shipped alongside the package.
    """
    override = os.environ.get(OVERRIDE_ENV)
    if override:
        return Path(override).expanduser().resolve()

    here = Path.cwd().resolve()
    for candidate in (here, *here.parents):
        found = candidate / MANIFEST_FILENAME
        if found.is_file():
            return found

    # Installed-package fallback: src/marketradar/manifest.py -> repo root.
    packaged = Path(__file__).resolve().parents[2] / MANIFEST_FILENAME
    if packaged.is_file():
        return packaged

    raise ManifestError(
        f"No {MANIFEST_FILENAME} found above {here}, and none packaged. "
        f"Set {OVERRIDE_ENV} to point at one."
    )


_CACHE: dict[Path, tuple[float, dict[str, Any]]] = {}


def clear_cache() -> None:
    """Drop the parsed-manifest cache. For tests that rewrite the file."""
    _CACHE.clear()


def _load(path: Path | None = None) -> dict[str, Any]:
    """Parse the manifest, caching on mtime so repeated reads are cheap."""
    path = path or manifest_path()
    try:
        mtime = path.stat().st_mtime
    except OSError as exc:
        raise ManifestError(f"Cannot read manifest at {path}: {exc}") from exc

    cached = _CACHE.get(path)
    if cached and cached[0] == mtime:
        return cached[1]

    with path.open("rb") as fh:
        try:
            data = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ManifestError(f"Malformed manifest at {path}: {exc}") from exc

    _validate(data, path)
    _CACHE[path] = (mtime, data)
    return data


def _validate(data: dict[str, Any], path: Path) -> None:
    for dataset, partitions in data.items():
        if not isinstance(partitions, dict):
            raise ManifestError(
                f"{path}: [{dataset}] must contain partition tables, got "
                f"{type(partitions).__name__}"
            )
        for partition, entry in partitions.items():
            where = f"{path}: [{dataset}.{partition}]"
            if not isinstance(entry, dict):
                raise ManifestError(f"{where} must be a table")
            for key in ("location", "backend"):
                if key not in entry:
                    raise ManifestError(f"{where} is missing required key {key!r}")
            backend = entry["backend"]
            if backend not in VALID_BACKENDS:
                raise ManifestError(
                    f"{where} has unknown backend {backend!r}; "
                    f"expected one of {sorted(VALID_BACKENDS)}"
                )


def get(dataset: str, partition: str) -> DatasetRef:
    """Resolve one dataset partition to a location.

    Raises:
        UnknownDatasetError: if the dataset or partition is not defined.
    """
    data = _load()

    if dataset not in data:
        known = ", ".join(sorted(data)) or "<manifest is empty>"
        raise UnknownDatasetError(
            f"No dataset {dataset!r} in {manifest_path()}. Known datasets: {known}"
        )

    partitions = data[dataset]
    key = str(partition)
    if key not in partitions:
        known = ", ".join(sorted(partitions)) or "<none>"
        raise UnknownDatasetError(
            f"Dataset {dataset!r} has no partition {key!r}. Known partitions: {known}"
        )

    entry = partitions[key]
    return DatasetRef(
        dataset=dataset,
        partition=key,
        location=entry["location"],
        backend=entry["backend"],
    )


def datasets() -> list[DatasetRef]:
    """Every partition the manifest defines, sorted."""
    data = _load()
    return [
        get(dataset, partition)
        for dataset in sorted(data)
        for partition in sorted(data[dataset])
    ]


def record_stats(freshness: Freshness, con: Any = None) -> bool:
    """Append a freshness observation to ``dataset_stats``.

    Returns True if it was persisted, False if no Postgres is configured.

    Deliberately does *not* raise when Postgres is absent: local dev without
    Supabase is a hard requirement, and stats are diagnostics rather than a
    correctness gate. The correctness gate is ``freshness.assert_fresh``,
    which reads the data itself and needs no database at all.
    """
    from marketradar import storage  # local import keeps this module dependency-free

    con = con if con is not None else storage.connect(enable_http=False)
    if not storage.postgres_attached(con):
        return False

    con.execute(
        f"INSERT INTO {storage.PG_ALIAS}.dataset_stats "
        "(dataset, partition, row_count, max_date) VALUES (?, ?, ?, ?)",
        [
            freshness.dataset,
            freshness.partition,
            freshness.row_count,
            freshness.max_date,
        ],
    )
    return True


def latest_stats(dataset: str, partition: str, con: Any = None) -> Freshness | None:
    """Most recent observation for a partition, or None if unavailable."""
    from marketradar import storage

    con = con if con is not None else storage.connect(enable_http=False)
    if not storage.postgres_attached(con):
        return None

    row = con.execute(
        f"SELECT row_count, max_date FROM {storage.PG_ALIAS}.dataset_stats "
        "WHERE dataset = ? AND partition = ? ORDER BY observed_at DESC LIMIT 1",
        [dataset, partition],
    ).fetchone()
    if row is None:
        return None
    return Freshness(dataset, partition, int(row[0]), row[1])


# --- does the declared location actually hold anything? -----------------
#
# **The manifest went unverified for weeks and that is the whole point of it.**
# Found 2026-09-12: all 30 `xbrl_fundamentals` partitions declared a
# `github_release` URL, the release did not exist, and every one of those URLs
# returned 404. Nothing noticed, because every consumer had a local cache in
# `.cache/xbrl/out` and read that instead.
#
# This file exists so that nothing hardcodes a location. A declared location
# nobody checks is the same silent-guard pattern as `--drop-zips` removing the
# drift test and `upsert_corporate_actions` returning `len(rows)`: the mechanism
# was in place, the check was not, and the failure looked like success.

#: The location exists and is readable.
OK: Final[str] = "ok"

#: The location is declared and there is nothing there. The failure this exists
#: to catch.
MISSING: Final[str] = "missing"

#: Cannot be checked from here -- a private backend with no credentials
#: configured. **Distinct from ``ok``**, deliberately: "we could not look" and
#: "we looked and it is there" are different facts, and collapsing them would
#: reproduce the bug one level up.
UNVERIFIABLE: Final[str] = "unverifiable"

#: Not storage. A ``vendor_api`` entry records where an upstream API lives so a
#: source module can honour the no-hardcoded-URL rule; probing it would mean
#: calling a vendor to check the manifest, which is not a thing to do on a
#: schedule.
SKIPPED: Final[str] = "skipped"

VERIFY_STATUSES: Final[tuple[str, ...]] = (OK, MISSING, UNVERIFIABLE, SKIPPED)

#: Waited once before calling a Release asset missing. GitHub answers 404 for a
#: short window after an upload reports .
PROPAGATION_WAIT: Final[float] = 4.0


@dataclass(frozen=True, slots=True)
class Verification:
    """What was found at one declared location."""

    dataset: str
    partition: str
    backend: str
    location: str
    status: str
    detail: str = ""

    @property
    def broken(self) -> bool:
        return self.status == MISSING


def verify(ref: DatasetRef, *, con: Any = None, timeout: float = 30.0) -> Verification:
    """Check that ``ref.location`` holds something, per backend.

    Each backend is checked the way it is read, not by a generic existence probe:
    a Release asset by HTTP, an R2 object through DuckDB's httpfs with the
    configured credentials, a Supabase entry by asking Postgres whether the table
    is there. A check that does not resemble the read path can pass while the read
    fails.
    """
    base = dict(dataset=ref.dataset, partition=ref.partition,
                backend=ref.backend, location=ref.location)
    if ref.backend == "vendor_api":
        return Verification(status=SKIPPED, detail="an upstream endpoint, not "
                                                  "storage", **base)
    if ref.backend == "local":
        path = Path(ref.location)
        return Verification(
            status=OK if path.is_file() else MISSING,
            detail="" if path.is_file() else "no such file", **base)
    if ref.backend == "github_release":
        import httpx

        # A one-byte range request rather than a download: this checks the asset
        # is there, not that it parses, and 62 locations should not cost 4 MB.
        #
        # Retried once on a 404, because GitHub serves 404 for a short window
        # after an upload completes. Measured 2026-09-12: assets reporting
        # `state: uploaded` answered 404 for under a minute and then 206. Without
        # the retry this reports five missing partitions immediately after
        # publishing them -- which is exactly when somebody runs it, and a check
        # that cries wolf when you are watching is a check that gets ignored.
        last = ""
        for attempt in range(2):
            if attempt:
                time.sleep(PROPAGATION_WAIT)
            try:
                resp = httpx.get(ref.location, follow_redirects=True,
                                 timeout=timeout,
                                 headers={"Range": "bytes=0-0",
                                          "Cache-Control": "no-cache"})
            except httpx.HTTPError as exc:
                return Verification(status=UNVERIFIABLE,
                                    detail=f"network: {exc}"[:120], **base)
            if resp.status_code in (200, 206):
                return Verification(status=OK, **base)
            last = f"HTTP {resp.status_code}"
            if resp.status_code != 404:
                break
        return Verification(status=MISSING, detail=last, **base)
    if ref.backend == "r2":
        if not _r2_configured():
            return Verification(
                status=UNVERIFIABLE,
                detail="no R2 credentials in this environment", **base)
        from marketradar import storage

        con = con or storage.connect()
        try:
            con.execute(
                f"select 1 from read_parquet('{ref.location}') limit 1"
            ).fetchone()
        except Exception as exc:
            return Verification(status=MISSING, detail=str(exc)[:120], **base)
        return Verification(status=OK, **base)
    if ref.backend == "supabase":
        from marketradar import storage

        con = con or storage.connect()
        if not storage.postgres_attached(con):
            return Verification(status=UNVERIFIABLE,
                                detail="Postgres not attached", **base)
        try:
            con.execute(
                f"select 1 from {storage.PG_ALIAS}.{ref.location} limit 1"
            ).fetchone()
        except Exception as exc:
            return Verification(status=MISSING, detail=str(exc)[:120], **base)
        return Verification(status=OK, **base)
    return Verification(status=UNVERIFIABLE,
                        detail=f"no check for backend {ref.backend!r}", **base)


def _r2_configured() -> bool:
    return all(os.environ.get(name) for name in
               ("MR_R2_ACCOUNT_ID", "MR_R2_ACCESS_KEY_ID",
                "MR_R2_SECRET_ACCESS_KEY"))


def verify_all(*, con: Any = None, datasets_only: tuple[str, ...] = ()
               ) -> list[Verification]:
    """Every declared partition, checked. Ordered as the manifest declares them."""
    refs = datasets()
    if datasets_only:
        refs = [r for r in refs if r.dataset in datasets_only]
    return [verify(ref, con=con) for ref in refs]


def verify_lines(results: list[Verification]) -> list[str]:
    """A report that leads with what is broken."""
    counts = {s: sum(1 for r in results if r.status == s)
              for s in VERIFY_STATUSES}
    out = [f"manifest: {len(results)} declared locations"]
    for status in VERIFY_STATUSES:
        if counts[status]:
            out.append(f"  {status:<14}{counts[status]:>4}")
    broken = [r for r in results if r.broken]
    if broken:
        out.append("")
        out.append(f"  {len(broken)} declared location(s) hold nothing:")
        for r in broken:
            out.append(f"    {r.dataset}/{r.partition:<10} {r.backend:<15} "
                       f"{r.detail}")
        out.append("")
        out.append("  This is the failure the manifest exists to prevent. A local")
        out.append("  cache will mask it on the machine that built the data and")
        out.append("  nowhere else.")
    unver = [r for r in results if r.status == UNVERIFIABLE]
    if unver:
        out.append("")
        out.append(f"  {len(unver)} could not be checked here -- **not** the same")
        out.append("  as checked and fine:")
        for r in unver[:6]:
            out.append(f"    {r.dataset}/{r.partition:<10} {r.detail}")
        if len(unver) > 6:
            out.append(f"    ... and {len(unver) - 6} more")
    return out
