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
