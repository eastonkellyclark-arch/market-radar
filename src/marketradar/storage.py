"""DuckDB session, Parquet-over-HTTP, and the optional Postgres attach.

Everything reads through :func:`marketradar.manifest.get`. There is not a
single literal data URL in this module, and there must never be one — a test
in ``tests/test_repo_invariants.py`` enforces that for ``sources/``.

Credentials and Postgres are both optional. A connection with neither is fully
usable against local files, because local dev without Supabase is a hard
requirement.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Final

import duckdb

from marketradar import manifest

log = logging.getLogger(__name__)

#: Alias the Supabase Postgres database is attached under.
PG_ALIAS: Final[str] = "pg"

# Environment variables. Real values live in Actions secrets or a local .env
# that is never committed; see .env.example for the shape.
ENV_R2_ACCOUNT: Final[str] = "MR_R2_ACCOUNT_ID"
ENV_R2_KEY_ID: Final[str] = "MR_R2_ACCESS_KEY_ID"
ENV_R2_SECRET: Final[str] = "MR_R2_SECRET_ACCESS_KEY"
ENV_PG_DSN: Final[str] = "MR_POSTGRES_DSN"

# A value copied straight from .env.example is not a configured service.
_PLACEHOLDER: Final[re.Pattern[str]] = re.compile(
    r"dummy|example|changeme|your-|localhost", re.I
)


class StorageError(RuntimeError):
    """Something went wrong setting up or using the query engine."""


def connect(
    *,
    enable_http: bool = True,
    attach_postgres: bool | None = None,
    read_only_pg: bool = False,
) -> duckdb.DuckDBPyConnection:
    """Open a configured in-process DuckDB connection.

    Args:
        enable_http: load ``httpfs`` so remote Parquet can be range-read.
            Set False in tests to keep the connection provably offline.
        attach_postgres: attach Supabase under :data:`PG_ALIAS`. Default None
            means "attach if ``MR_POSTGRES_DSN`` is set", so the same code
            path works with and without a database.
        read_only_pg: attach Postgres read-only.

    Neither httpfs nor Postgres failing is fatal here. A missing extension
    surfaces when a remote read is actually attempted, with DuckDB's own
    error; a missing database surfaces via :func:`postgres_attached`. Failing
    at connect time would make every offline unit test require a network.
    """
    con = duckdb.connect()

    if enable_http:
        _enable_httpfs(con)
        _configure_r2(con)

    if attach_postgres is None:
        # Auto-detect. A placeholder DSN copied from .env.example counts as
        # "not configured" — otherwise every fresh checkout tries to attach a
        # database at localhost and dies before doing any work.
        attach_postgres = postgres_configured()
        if attach_postgres:
            try:
                _attach_postgres(con, read_only=read_only_pg)
            except StorageError as exc:
                # Auto-detected, so degrade rather than crash. Anything that
                # genuinely needs Postgres checks postgres_attached() first.
                log.warning("Postgres not attached: %s", exc)
    elif attach_postgres:
        # Explicitly requested: failing silently would hide a real problem.
        _attach_postgres(con, read_only=read_only_pg)

    return con


def postgres_configured() -> bool:
    """True when MR_POSTGRES_DSN holds something that looks real."""
    dsn = os.environ.get(ENV_PG_DSN, "")
    if not dsn:
        return False
    return not _PLACEHOLDER.search(dsn)


def _enable_httpfs(con: duckdb.DuckDBPyConnection) -> None:
    """Load httpfs, installing it only if it is not already cached."""
    try:
        con.execute("LOAD httpfs")
        return
    except duckdb.Error:
        pass  # not installed yet; try to fetch it

    try:
        con.execute("INSTALL httpfs")
        con.execute("LOAD httpfs")
    except duckdb.Error as exc:
        log.warning(
            "httpfs unavailable (%s). Local files still work; remote reads "
            "will fail with DuckDB's own error when attempted.",
            exc,
        )


def _configure_r2(con: duckdb.DuckDBPyConnection) -> None:
    """Register an R2 secret when all three credentials are present.

    DuckDB 1.5 supports ``TYPE r2`` natively and scopes it to ``r2://``, so a
    manifest location of ``r2://bucket/key`` resolves without any further
    configuration.
    """
    account = os.environ.get(ENV_R2_ACCOUNT)
    key_id = os.environ.get(ENV_R2_KEY_ID)
    secret = os.environ.get(ENV_R2_SECRET)

    if not (account and key_id and secret):
        missing = [
            name
            for name, value in (
                (ENV_R2_ACCOUNT, account),
                (ENV_R2_KEY_ID, key_id),
                (ENV_R2_SECRET, secret),
            )
            if not value
        ]
        log.debug("R2 not configured; missing %s", ", ".join(missing))
        return

    try:
        con.execute(
            "CREATE OR REPLACE SECRET mr_r2 "
            "(TYPE r2, KEY_ID ?, SECRET ?, ACCOUNT_ID ?)",
            [key_id, secret, account],
        )
    except duckdb.Error as exc:  # pragma: no cover - needs real credentials
        raise StorageError(f"Could not register R2 secret: {exc}") from exc


def _attach_postgres(con: duckdb.DuckDBPyConnection, *, read_only: bool) -> None:
    dsn = os.environ.get(ENV_PG_DSN)
    if not dsn:
        raise StorageError(
            f"attach_postgres was requested but {ENV_PG_DSN} is not set."
        )

    try:
        con.execute("INSTALL postgres")
        con.execute("LOAD postgres")
        suffix = ", READ_ONLY" if read_only else ""
        # ATTACH does not accept a bound parameter, so the DSN is inlined.
        # Single quotes are doubled; a DSN cannot legally contain one, but
        # building SQL by concatenation without escaping is how that stops
        # being true.
        escaped = dsn.replace("'", "''")
        con.execute(f"ATTACH '{escaped}' AS {PG_ALIAS} (TYPE postgres{suffix})")
    except duckdb.Error as exc:
        raise StorageError(f"Could not attach Postgres as {PG_ALIAS}: {exc}") from exc


def postgres_attached(con: duckdb.DuckDBPyConnection) -> bool:
    """True when Supabase is attached under :data:`PG_ALIAS`."""
    try:
        rows = con.execute(
            "SELECT 1 FROM duckdb_databases() WHERE database_name = ?", [PG_ALIAS]
        ).fetchall()
    except duckdb.Error:
        return False
    return bool(rows)


def read_dataset(
    dataset: str,
    partition: str,
    con: duckdb.DuckDBPyConnection | None = None,
) -> duckdb.DuckDBPyRelation:
    """Resolve a dataset through the manifest and return it as a relation.

    The only supported way to read project data. Raises
    :class:`~marketradar.manifest.UnknownDatasetError` for anything the
    manifest does not define.
    """
    ref = manifest.get(dataset, partition)
    con = con if con is not None else connect()
    return read_ref(ref, con)


def read_ref(
    ref: manifest.DatasetRef,
    con: duckdb.DuckDBPyConnection | None = None,
) -> duckdb.DuckDBPyRelation:
    """Read an already-resolved :class:`~marketradar.manifest.DatasetRef`."""
    con = con if con is not None else connect()

    if ref.backend == "supabase":
        raise StorageError(
            f"{ref.dataset}/{ref.partition} lives in Postgres, not Parquet. "
            f"Query {PG_ALIAS}.{ref.location} directly."
        )

    try:
        return con.read_parquet(ref.location)
    except duckdb.Error as exc:
        raise StorageError(
            f"Could not read {ref.dataset}/{ref.partition} from "
            f"{ref.backend}: {exc}"
        ) from exc


def describe_connection(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """What this connection can actually do. Used by ``mr manifest``."""
    loaded = {
        row[0]
        for row in con.execute(
            "SELECT extension_name FROM duckdb_extensions() WHERE loaded"
        ).fetchall()
    }
    return {
        "duckdb_version": duckdb.__version__,
        "httpfs": "httpfs" in loaded,
        "postgres": postgres_attached(con),
        "r2_configured": all(
            os.environ.get(name)
            for name in (ENV_R2_ACCOUNT, ENV_R2_KEY_ID, ENV_R2_SECRET)
        ),
    }


# --- publishing a public asset ------------------------------------------


class PublishError(RuntimeError):
    """An asset could not be uploaded to its declared location."""


#: Environment variable naming the repo to publish to, e.g. ``owner/name``.
ENV_REPO: Final[str] = "MR_GITHUB_REPO"


def release_tag(location: str) -> str:
    """The Release tag a ``github_release`` location downloads from.

    Parsed from the manifest rather than configured separately: the location is
    already the single source of truth for where the asset lives, and a second
    copy of the tag is a second thing to get wrong.
    """
    marker = "/releases/download/"
    if marker not in location:
        raise PublishError(
            f"{location!r} is not a GitHub Release download URL, so there is no "
            "tag to infer. Check the manifest backend."
        )
    rest = location.split(marker, 1)[1]
    tag, _, _asset = rest.partition("/")
    if not tag:
        raise PublishError(f"{location!r} has no release tag in it")
    return tag


def publish_release_asset(
    ref: Any,
    local: Path,
    *,
    notes: str = "",
    runner: Any = None,
) -> str:
    """Upload ``local`` to the Release that ``ref.location`` points at.

    **This exists because the upload used to be a `gh` command typed by hand**,
    outside the codebase and therefore outside every check. 35 declared locations
    held nothing for weeks as a direct result: the files were built correctly,
    written locally, and never uploaded, and no code path could tell.

    Refuses a private backend outright. Publishing an ``r2`` or ``supabase``
    partition to a public Release would cross the licensing boundary in the one
    direction that matters -- the manifest's ``backend`` column exists to make
    that visible and this is the place it has to be enforced rather than
    remembered.
    """
    import shutil
    import subprocess

    if getattr(ref, "is_private", False):
        raise PublishError(
            f"{ref.dataset}/{ref.partition} has backend {ref.backend!r}, which is "
            "private. Publishing it to a public GitHub Release would be "
            "redistribution of vendor data -- see the licensing rule in "
            "CLAUDE.md. The backend column exists to stop exactly this."
        )
    if ref.backend != "github_release":
        raise PublishError(
            f"{ref.dataset}/{ref.partition} has backend {ref.backend!r}; only "
            "github_release is published this way."
        )
    if not local.is_file() or local.stat().st_size == 0:
        raise PublishError(f"{local} is missing or empty; nothing to upload")

    repo = os.environ.get(ENV_REPO, "").strip()
    if not repo:
        raise PublishError(f"{ENV_REPO} is not set, so there is no repo to "
                           "publish to")
    gh = shutil.which("gh")
    if gh is None:
        raise PublishError(
            "the GitHub CLI (`gh`) is not on PATH. It is the upload path for "
            "public Release assets; install it or upload by hand and re-run with "
            "the verification."
        )
    run = runner or subprocess.run
    tag = release_tag(ref.location)

    seen = run([gh, "release", "view", tag, "--repo", repo],
               capture_output=True, text=True)
    if seen.returncode != 0:
        made = run(
            [gh, "release", "create", tag, "--repo", repo,
             "--title", tag,
             "--notes", notes or ("Public-domain data republished from a "
                                  "government source.")],
            capture_output=True, text=True,
        )
        if made.returncode != 0:
            raise PublishError(
                f"could not create release {tag}: {made.stderr.strip()[:300]}")

    up = run([gh, "release", "upload", tag, str(local), "--repo", repo,
              "--clobber"], capture_output=True, text=True)
    if up.returncode != 0:
        raise PublishError(
            f"could not upload {local.name} to {tag}: "
            f"{up.stderr.strip()[:300]}")
    log.info("published %s to release %s", local.name, tag)
    return tag
