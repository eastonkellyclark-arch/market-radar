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
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Final, Literal

Backend = Literal["r2", "github_release", "supabase", "local"]

VALID_BACKENDS: Final[frozenset[str]] = frozenset(
    {"r2", "github_release", "supabase", "local"}
)

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
