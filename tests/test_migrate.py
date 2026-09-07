"""Coverage for the migration runner, offline.

Applying migrations needs a live database. Splitting them does not, and the
splitter is where the bug was.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from marketradar import migrate, storage
from marketradar.migrate import MigrationError, split_statements

SQL = Path(__file__).resolve().parents[1] / "sql"


def _body(name: str = "001_init.sql") -> str:
    """The executable SQL, comments stripped and whitespace collapsed.

    Content assertions must not read the prose. The header comment explains
    *why* there is no halfvec column, and a naive substring search over the
    raw file fails on the explanation rather than on the schema.
    """
    text = (SQL / name).read_text(encoding="utf-8")
    return " ".join(" ; ".join(split_statements(text)).split()).lower()


# --- the splitter ---------------------------------------------------------

def test_splits_on_semicolons() -> None:
    assert split_statements("select 1; select 2;") == ["select 1", "select 2"]


def test_trailing_statement_without_semicolon_is_kept() -> None:
    assert split_statements("select 1;\nselect 2") == ["select 1", "select 2"]


def test_full_line_comments_are_stripped() -> None:
    out = split_statements("-- a comment\nselect 1;\n-- another\nselect 2;")
    assert out == ["select 1", "select 2"]


def test_inline_comment_containing_a_semicolon_does_not_split() -> None:
    """The actual bug.

    ``locked_at timestamptz,  -- set on claim; stale locks reclaimable``
    split one CREATE TABLE into two statements, and the second half was
    sent to Postgres as garbage.
    """
    sql = """
    create table t (
        a int,
        b timestamptz,   -- set on claim; stale locks reclaimable
        c text
    );
    """
    out = split_statements(sql)
    assert len(out) == 1
    assert "create table t" in out[0]
    assert "stale locks" not in out[0]
    assert out[0].rstrip().endswith(")")


def test_semicolon_inside_a_string_literal_does_not_split() -> None:
    sql = "comment on table t is 'first; second';"
    out = split_statements(sql)
    assert out == ["comment on table t is 'first; second'"]


def test_double_quote_escape_inside_a_string() -> None:
    sql = "comment on table t is 'it''s fine; really';\nselect 1;"
    out = split_statements(sql)
    assert len(out) == 2
    assert out[0] == "comment on table t is 'it''s fine; really'"


def test_double_dash_inside_a_string_is_not_a_comment() -> None:
    sql = "insert into t values ('a--b'); select 1;"
    out = split_statements(sql)
    assert len(out) == 2
    assert "a--b" in out[0]


def test_empty_input_yields_nothing() -> None:
    assert split_statements("") == []
    assert split_statements("-- only a comment\n") == []
    assert split_statements(";;;") == []


# --- the committed migration ---------------------------------------------

def test_init_migration_exists_and_parses() -> None:
    path = SQL / "001_init.sql"
    assert path.is_file()
    statements = split_statements(path.read_text(encoding="utf-8"))
    assert len(statements) == 20, f"expected 20 statements, got {len(statements)}"


def test_every_statement_is_idempotent() -> None:
    """Re-running a migration must be a no-op, not an error."""
    text = (SQL / "001_init.sql").read_text(encoding="utf-8")
    for statement in split_statements(text):
        head = " ".join(statement.split()).lower()
        if head.startswith(("create table", "create index", "create unique index")):
            assert "if not exists" in head, f"not idempotent: {head[:80]}"


def test_all_five_tables_are_created() -> None:
    text = _body()
    for table in (
        "companies",
        "signals",
        "job_queue",
        "corporate_actions",
        "dataset_stats",
    ):
        assert f"create table if not exists {table}" in text


def test_no_embeddings_or_halfvec() -> None:
    """Add when something needs it, not before."""
    text = _body()
    for banned in ("halfvec", "vector(", "pgvector", "embedding"):
        assert banned not in text, f"{banned!r} should not be in the schema yet"


def test_ticker_is_indexed_but_never_unique() -> None:
    """Share classes and recycled tickers. CIK or id only."""
    text = _body()
    assert "create index if not exists companies_ticker_idx" in text
    assert "unique index if not exists companies_ticker" not in text
    assert "ticker text unique" not in text


def test_cik_is_unique_but_allows_nulls() -> None:
    """Private companies have no CIK, and there are many of them."""
    text = _body()
    assert "create unique index if not exists companies_cik_key" in text
    assert "where cik is not null" in text


def test_idempotency_constraints_are_present() -> None:
    text = _body()
    assert "unique index if not exists signals_natural_key" in text
    assert "unique index if not exists job_queue_pending_key" in text
    assert "corporate_actions_natural_key unique" in text


def test_job_queue_has_locked_at_and_last_error() -> None:
    text = _body()
    assert "locked_at" in text
    assert "last_error" in text


# --- guard rails ----------------------------------------------------------

def test_migrate_refuses_without_a_real_database(monkeypatch) -> None:
    monkeypatch.delenv(storage.ENV_PG_DSN, raising=False)
    with pytest.raises(MigrationError, match="not set"):
        migrate.run()


def test_migrate_refuses_a_placeholder_dsn(monkeypatch) -> None:
    monkeypatch.setenv(storage.ENV_PG_DSN, "postgresql://dummy:dummy@localhost:5432/x")
    with pytest.raises(MigrationError, match="placeholder"):
        migrate.run()


def test_dry_run_needs_no_database(monkeypatch, capsys) -> None:
    monkeypatch.delenv(storage.ENV_PG_DSN, raising=False)
    assert migrate.run(dry_run=True) == 0
    assert "001_init.sql" in capsys.readouterr().out
