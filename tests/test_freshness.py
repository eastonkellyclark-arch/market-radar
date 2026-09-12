"""The assertion has to actually fail. An assertion nobody has watched fail
is a comment."""

from __future__ import annotations

from datetime import date, timedelta

import duckdb
import pytest

from marketradar.freshness import (
    DEFAULT_MAX_STALENESS_DAYS,
    StaleDataError,
    assert_fresh,
    utc_today,
)

TODAY = date(2026, 9, 4)


def _rel(con: duckdb.DuckDBPyConnection, rows: list[tuple[str, date, float]]):
    con.execute("CREATE OR REPLACE TABLE t (ticker VARCHAR, date DATE, close DECIMAL(18,6))")
    if rows:
        con.executemany("INSERT INTO t VALUES (?, ?, ?)", rows)
    return con.table("t")


def test_passes_on_healthy_data(con: duckdb.DuckDBPyConnection) -> None:
    rel = _rel(con, [("AAPL", TODAY, 1.5), ("MSFT", TODAY, 2.5)])
    out = assert_fresh("prices", rel, min_rows=2, today=TODAY)
    assert out.row_count == 2
    assert out.max_date == TODAY
    assert out.dataset == "prices"


def test_raises_on_zero_rows(con: duckdb.DuckDBPyConnection) -> None:
    rel = _rel(con, [])
    with pytest.raises(StaleDataError, match="returned nothing"):
        assert_fresh("prices", rel, min_rows=1, today=TODAY)


def test_raises_on_short_load(con: duckdb.DuckDBPyConnection) -> None:
    """12 rows from a market-wide sweep is broken, even though it is not empty."""
    rel = _rel(con, [("AAPL", TODAY, 1.5)])
    with pytest.raises(StaleDataError, match="incomplete"):
        assert_fresh("prices", rel, min_rows=5_000, today=TODAY)


def test_raises_on_stale_max_date(con: duckdb.DuckDBPyConnection) -> None:
    old = TODAY - timedelta(days=DEFAULT_MAX_STALENESS_DAYS + 1)
    rel = _rel(con, [("AAPL", old, 1.5)])
    with pytest.raises(StaleDataError, match="feed has probably stopped"):
        assert_fresh("prices", rel, min_rows=1, today=TODAY)


def test_raises_on_missing_column(con: duckdb.DuckDBPyConnection) -> None:
    rel = _rel(con, [("AAPL", TODAY, 1.5)])
    with pytest.raises(StaleDataError, match="schema changed"):
        assert_fresh("prices", rel, min_rows=1, expect_cols=["volume"], today=TODAY)


def test_missing_column_is_reported_before_row_count(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """A schema change should report as a schema change, not as an empty load."""
    rel = _rel(con, [])
    with pytest.raises(StaleDataError, match="schema changed"):
        assert_fresh("prices", rel, min_rows=1, expect_cols=["volume"], today=TODAY)


def test_raises_when_dates_are_all_null(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("CREATE OR REPLACE TABLE t (ticker VARCHAR, date DATE)")
    con.execute("INSERT INTO t VALUES ('AAPL', NULL)")
    with pytest.raises(StaleDataError, match="every 'date' is NULL"):
        assert_fresh("prices", con.table("t"), min_rows=1, today=TODAY)


def test_raises_on_future_dates(con: duckdb.DuckDBPyConnection) -> None:
    """A date after today means the source's timezone handling is wrong."""
    rel = _rel(con, [("AAPL", TODAY + timedelta(days=2), 1.5)])
    with pytest.raises(StaleDataError, match="timezone"):
        assert_fresh("prices", rel, min_rows=1, today=TODAY)


def test_friday_data_read_on_monday_holiday_passes(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """The calendar-day threshold must survive a long weekend without crying wolf.

    Friday 2026-09-04 close, read on Monday 2026-09-07 (Labor Day).
    """
    friday, monday = date(2026, 9, 4), date(2026, 9, 7)
    rel = _rel(con, [("AAPL", friday, 1.5)])
    out = assert_fresh("prices", rel, min_rows=1, today=monday)
    assert out.max_date == friday


def test_date_column_none_skips_staleness(con: duckdb.DuckDBPyConnection) -> None:
    """Datasets with no time dimension still get a row-count floor."""
    con.execute("CREATE OR REPLACE TABLE t (cik VARCHAR, name VARCHAR)")
    con.execute("INSERT INTO t VALUES ('0000320193', 'Apple Inc.')")
    out = assert_fresh("tickers", con.table("t"), min_rows=1, date_column=None)
    assert out.max_date is None
    assert out.row_count == 1


def test_rejects_non_identifier_date_column(con: duckdb.DuckDBPyConnection) -> None:
    rel = _rel(con, [("AAPL", TODAY, 1.5)])
    with pytest.raises(ValueError, match="plain identifier"):
        assert_fresh("prices", rel, min_rows=1, date_column='date"; DROP TABLE t--')


def test_decimal_survives_the_round_trip(con: duckdb.DuckDBPyConnection) -> None:
    """$0.0002 has to work. This is why prices are not integer cents."""
    from decimal import Decimal

    con.execute("CREATE OR REPLACE TABLE t (date DATE, close DECIMAL(18,6))")
    con.execute("INSERT INTO t VALUES (DATE '2026-09-04', 0.000200)")
    assert_fresh("prices", con.table("t"), min_rows=1, today=TODAY)
    assert con.execute("SELECT close FROM t").fetchone()[0] == Decimal("0.000200")


def test_utc_today_is_a_date() -> None:
    assert isinstance(utc_today(), date)


# --- the declared location, checked where it is written -----------------


def _published_rel(con, rows: int = 5):
    """A small relation for the published-location tests.

    Named apart from the module's own _rel helper: appending a second
    definition of that name silently replaced the first and broke nine unrelated
    tests, which is the shadowing equivalent of everything else in this file.
    """
    con.execute(
        f"create or replace table published_t as select range as n, "
        f"date '2026-09-10' as date from range({rows})")
    return con.table("published_t")


def test_a_good_load_to_a_broken_location_raises(monkeypatch) -> None:
    """**Why this lives in the assertion and not in a separate command.**

    A separate command only runs when somebody remembers, and that is exactly how
    35 declared locations stayed broken for weeks: 30 xbrl partitions, three Form
    5500 years and sec_filers/all all returned 404 while every local run passed,
    because every consumer read a cache instead.

    The data here is fine. The place it claims to live is not, and that has to be
    a failure at the moment of writing rather than a discovery months later.
    """
    import duckdb

    from marketradar import manifest

    con = duckdb.connect()
    ref = manifest.DatasetRef(dataset="d", partition="p", location="http://x/a",
                              backend="github_release")
    monkeypatch.setattr(
        manifest, "verify",
        lambda r, **kw: manifest.Verification(
            r.dataset, r.partition, r.backend, r.location, manifest.MISSING,
            "HTTP 404"))
    with pytest.raises(StaleDataError, match="declared location"):
        assert_fresh("d", _published_rel(con), partition="p", min_rows=1,
                     date_column=None, published=ref)


def test_the_data_is_checked_before_the_location(monkeypatch) -> None:
    """A publish that produced an empty file should report *that*, not that the
    URL is fine. Order matters because the first error is the one read."""
    import duckdb

    from marketradar import manifest

    con = duckdb.connect()
    ref = manifest.DatasetRef(dataset="d", partition="p", location="http://x/a",
                              backend="github_release")
    called = {"n": 0}

    def counting(r, **kw):
        called["n"] += 1
        return manifest.Verification(r.dataset, r.partition, r.backend,
                                     r.location, manifest.OK)

    monkeypatch.setattr(manifest, "verify", counting)
    with pytest.raises(StaleDataError, match="expected at least"):
        assert_fresh("d", _published_rel(con, rows=2), partition="p", min_rows=1000,
                     date_column=None, published=ref)
    assert called["n"] == 0, "the location was probed before the data was checked"


def test_a_good_load_to_a_good_location_passes(monkeypatch) -> None:
    import duckdb

    from marketradar import manifest

    con = duckdb.connect()
    ref = manifest.DatasetRef(dataset="d", partition="p", location="http://x/a",
                              backend="github_release")
    monkeypatch.setattr(
        manifest, "verify",
        lambda r, **kw: manifest.Verification(
            r.dataset, r.partition, r.backend, r.location, manifest.OK))
    got = assert_fresh("d", _published_rel(con), partition="p", min_rows=1,
                       date_column=None, published=ref)
    assert got.row_count == 5


def test_an_unverifiable_location_warns_rather_than_passing_silently(
    monkeypatch, caplog
) -> None:
    """"Could not look" is a third answer. It does not fail the load -- a
    developer machine with no R2 credentials must still be able to build -- but it
    must not read as success either, which is the bug one level up."""
    import duckdb

    from marketradar import manifest

    con = duckdb.connect()
    ref = manifest.DatasetRef(dataset="d", partition="p",
                              location="r2://b/a", backend="r2")
    monkeypatch.setattr(
        manifest, "verify",
        lambda r, **kw: manifest.Verification(
            r.dataset, r.partition, r.backend, r.location,
            manifest.UNVERIFIABLE, "no credentials"))
    with caplog.at_level("WARNING"):
        got = assert_fresh("d", _published_rel(con), partition="p", min_rows=1,
                           date_column=None, published=ref)
    assert got.row_count == 5
    assert "not the same as verified" in caplog.text


def test_omitting_published_skips_the_check_entirely(monkeypatch) -> None:
    """Loaders that write locally and publish later must not be forced to have a
    live URL at assert time -- that would fail every developer machine by
    construction, which is why this is opt-in rather than always on."""
    import duckdb

    from marketradar import manifest

    con = duckdb.connect()

    def boom(*a, **kw):
        raise AssertionError("verify was called with no published ref")

    monkeypatch.setattr(manifest, "verify", boom)
    assert assert_fresh("d", _published_rel(con), partition="p", min_rows=1,
                        date_column=None).row_count == 5
