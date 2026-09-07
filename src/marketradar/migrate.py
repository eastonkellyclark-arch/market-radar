"""Apply SQL migrations to Supabase.

Runs each file in ``sql/`` in filename order via DuckDB's ``postgres_execute``,
which avoids taking a Postgres driver as a dependency just to run DDL.

Migrations are expected to be idempotent — every object in ``001_init.sql``
uses ``IF NOT EXISTS`` — so re-applying is a no-op rather than an error. That
matters more than a version table at this size: there is one database and one
operator, and "run it again" should always be safe.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from marketradar import storage


class MigrationError(RuntimeError):
    """A migration could not be applied."""


def sql_dir() -> Path:
    here = Path.cwd() / "sql"
    if here.is_dir():
        return here
    packaged = Path(__file__).resolve().parents[2] / "sql"
    if packaged.is_dir():
        return packaged
    raise MigrationError("No sql/ directory found.")


def migrations() -> list[Path]:
    return sorted(sql_dir().glob("*.sql"))


def split_statements(text: str) -> list[str]:
    """Split a migration into statements, respecting strings and comments.

    A character scan rather than a regex, because both of the obvious
    shortcuts are wrong here:

      * Stripping only full-line comments leaves inline ones, and a ``--``
        comment containing a semicolon then splits one statement into two.
        ``-- set on claim; stale locks reclaimable`` did exactly that.
      * Splitting on every semicolon breaks any ``COMMENT ON ... IS '...;...'``
        whose text contains one.

    Still not a SQL parser: dollar-quoted bodies ($$...$$) are not handled,
    because no migration here defines a function. Add that when one does.
    """
    statements: list[str] = []
    buf: list[str] = []
    in_string = False
    i, n = 0, len(text)

    while i < n:
        ch = text[i]

        if in_string:
            buf.append(ch)
            if ch == "'":
                # '' inside a string is an escaped quote, not a terminator.
                if i + 1 < n and text[i + 1] == "'":
                    buf.append(text[i + 1])
                    i += 2
                    continue
                in_string = False
            i += 1
            continue

        if ch == "'":
            in_string = True
            buf.append(ch)
            i += 1
            continue

        if ch == "-" and i + 1 < n and text[i + 1] == "-":
            while i < n and text[i] != "\n":
                i += 1
            continue

        if ch == ";":
            statement = "".join(buf).strip()
            if statement:
                statements.append(statement)
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def apply(con: duckdb.DuckDBPyConnection, path: Path, *, dry_run: bool = False) -> int:
    statements = split_statements(path.read_text(encoding="utf-8"))
    for i, statement in enumerate(statements, 1):
        if dry_run:
            first = " ".join(statement.split())[:88]
            print(f"    [{i:>2}/{len(statements)}] {first}")
            continue
        escaped = statement.replace("'", "''")
        try:
            con.execute(f"CALL postgres_execute('{storage.PG_ALIAS}', '{escaped}')")
        except duckdb.Error as exc:
            snippet = " ".join(statement.split())[:120]
            raise MigrationError(
                f"{path.name} statement {i} failed: {exc}\n  SQL: {snippet}"
            ) from exc
    return len(statements)


def run(*, dry_run: bool = False) -> int:
    files = migrations()
    if not files:
        print("no migrations found in sql/")
        return 0

    if dry_run:
        print("dry run — no statements will be executed\n")
        for path in files:
            print(f"  {path.name}")
            apply(None, path, dry_run=True)  # type: ignore[arg-type]
        return 0

    if not storage.postgres_configured():
        raise MigrationError(
            f"{storage.ENV_PG_DSN} is not set (or is still a placeholder). "
            "Migrations need a real database."
        )

    con = storage.connect(enable_http=False, attach_postgres=True)
    print(f"applying {len(files)} migration(s) to {storage.PG_ALIAS}\n")
    for path in files:
        count = apply(con, path)
        print(f"  {path.name:<20} {count} statement(s) OK")

    tables = con.execute(
        f"""
        SELECT table_name FROM {storage.PG_ALIAS}.information_schema.tables
        WHERE table_schema = 'public' ORDER BY table_name
        """
    ).fetchall()
    print(f"\npublic tables now present ({len(tables)}):")
    for (name,) in tables:
        print(f"  {name}")
    return 0
