"""Offline coverage for the volatility screen.

Every case is hand-built prices in an in-memory DuckDB. No network, no
Postgres, no manifest — the adjustment maths is the risky part and it has to
be checkable without any of that.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import duckdb
import pytest

from marketradar.freshness import StaleDataError
from marketradar.screens import volatility as vol

PX_DDL = """
create table px (
    ticker varchar, date date, close decimal(18,6), volume bigint,
    security_type varchar, exchange varchar
)
"""
ACT_DDL = """
create table act (
    ticker varchar, ex_date date, split_factor decimal(18,8), div_cash decimal(18,8)
)
"""


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    c = duckdb.connect()
    c.execute(PX_DDL)
    c.execute(ACT_DDL)
    return c


def add_price(c, ticker, day, close, volume=1_000_000, kind="stock", exch="NASDAQ"):
    c.execute(
        "insert into px values (?, ?, ?, ?, ?, ?)",
        [ticker, day, Decimal(close), volume, kind, exch],
    )


def add_action(c, ticker, ex_date, split_factor="1", div_cash="0"):
    c.execute(
        "insert into act values (?, ?, ?, ?)",
        [ticker, ex_date, Decimal(split_factor), Decimal(div_cash)],
    )


def run(c, **kw):
    return vol.screen(c, prices=c.table("px"), actions=c.table("act"), **kw)


def only(result, **match):
    """The one list matching every keyword."""
    found = [
        sl for sl in result.lists
        if all(getattr(sl, k) == v for k, v in match.items())
    ]
    assert len(found) == 1, f"expected one list for {match}, got {len(found)}"
    return found[0]


D3, D4 = date(2026, 9, 3), date(2026, 9, 4)


# --- the tick-count case ------------------------------------------------


def test_sub_penny_move_is_fifty_percent_and_one_tick(con) -> None:
    """The canonical case: percent alone would call this a monster move."""
    add_price(con, "PENNY", D3, "0.000200")
    add_price(con, "PENNY", D4, "0.000300")

    result = run(con, sanity_floor="0.0001")
    row = only(result, security_type="stock", band="sub$1",
               direction="gainers", liquidity="all").rows[0]

    assert row.pct_move == Decimal("50.000000")
    assert row.tick_move == Decimal("1.000000")
    assert row.tick_size == Decimal("0.000100")


def test_tick_size_is_a_penny_at_or_above_one_dollar(con) -> None:
    add_price(con, "BUCK", D3, "1.000000")
    add_price(con, "BUCK", D4, "1.010000")

    row = only(run(con), security_type="stock", band="$1-10",
               direction="gainers", liquidity="all").rows[0]
    assert row.tick_size == Decimal("0.010000")
    assert row.tick_move == Decimal("1.000000")


# --- adjustment ---------------------------------------------------------


def test_reverse_split_is_adjusted_not_reported_as_a_crash(con) -> None:
    """1:10 reverse split. Unadjusted this is +920%; adjusted it is +2%."""
    add_price(con, "RS", D3, "0.050000")
    add_price(con, "RS", D4, "0.510000")
    add_action(con, "RS", D4, split_factor="0.1")

    row = only(run(con, sanity_floor="0.0001"), security_type="stock",
               band="sub$1", direction="gainers", liquidity="all").rows[0]

    assert row.adj_prev_close == Decimal("0.500000")
    assert row.pct_move == Decimal("2.000000")
    assert row.split_factor == Decimal("0.100000000000")


def test_forward_split_is_adjusted(con) -> None:
    """2:1 split. Unadjusted this is -50%; adjusted it is flat."""
    add_price(con, "FS", D3, "100.000000")
    add_price(con, "FS", D4, "50.000000")
    add_action(con, "FS", D4, split_factor="2")

    rel = vol.moves(con, prices=con.table("px"), actions=con.table("act"))
    row = rel.fetchall()[0]
    assert row[6] == Decimal("50.000000")  # adj_prev_close
    assert row[7] == Decimal("0.000000")   # pct_move


def test_split_inside_a_price_gap_is_still_applied(con) -> None:
    """A halt or a missed load must not swallow the split.

    An equality join on ex_date = date would drop this one and report an
    uncorrected -50%.
    """
    add_price(con, "GAP", date(2026, 9, 1), "100.000000")
    add_price(con, "GAP", date(2026, 9, 4), "50.000000")
    add_action(con, "GAP", date(2026, 9, 2), split_factor="2")

    rel = vol.moves(con, prices=con.table("px"), actions=con.table("act"))
    row = rel.fetchall()[0]
    assert row[6] == Decimal("50.000000")
    assert row[7] == Decimal("0.000000")


def test_dividends_are_flagged_not_netted_by_default(con) -> None:
    """An ex-dividend drop is a real price move; hiding it hides a real fall."""
    add_price(con, "DIV", D3, "100.000000")
    add_price(con, "DIV", D4, "98.000000")
    add_action(con, "DIV", D4, div_cash="2")

    row = only(run(con), security_type="stock", band="$10+",
               direction="losers", liquidity="all").rows[0]
    assert row.pct_move == Decimal("-2.000000")
    assert row.is_ex_div is True
    assert row.div_cash == Decimal("2.000000")


def test_adjust_dividends_gives_the_total_return_view(con) -> None:
    add_price(con, "DIV", D3, "100.000000")
    add_price(con, "DIV", D4, "98.000000")
    add_action(con, "DIV", D4, div_cash="2")

    result = run(con, adjust_dividends=True)
    assert not only(result, security_type="stock", band="$10+",
                    direction="losers", liquidity="all").rows
    row = only(result, security_type="stock", band="$10+",
               direction="gainers", liquidity="all").rows
    assert row == []  # exactly flat, so it belongs to neither list


# --- bands --------------------------------------------------------------


@pytest.mark.parametrize(
    "prev, expected",
    [("0.500000", "sub$1"), ("5.000000", "$1-10"), ("50.000000", "$10+")],
)
def test_band_comes_from_the_pre_move_price(con, prev, expected) -> None:
    add_price(con, "T", D3, prev)
    add_price(con, "T", D4, str(Decimal(prev) * 3))

    rel = vol.moves(con, prices=con.table("px"), actions=con.table("act"))
    assert rel.fetchall()[0][4] == expected


def test_bands_are_separate_lists_not_a_filter(con) -> None:
    add_price(con, "A", D3, "0.500000")
    add_price(con, "A", D4, "0.600000")
    add_price(con, "B", D3, "5.000000")
    add_price(con, "B", D4, "6.000000")
    add_price(con, "C", D3, "50.000000")
    add_price(con, "C", D4, "60.000000")

    result = run(con)
    got = {
        sl.band: [m.ticker for m in sl.rows]
        for sl in result.lists
        if sl.direction == "gainers" and sl.liquidity == "all"
        and sl.security_type == "stock" and sl.rows
    }
    assert got == {"sub$1": ["A"], "$1-10": ["B"], "$10+": ["C"]}


# --- stocks vs ETFs -----------------------------------------------------


def test_etfs_go_to_their_own_lists_and_are_not_dropped(con) -> None:
    """A 3x ETF must not be able to outrank a stock, and must still appear."""
    add_price(con, "STK", D3, "100.000000", kind="stock")
    add_price(con, "STK", D4, "110.000000", kind="stock")
    add_price(con, "LEV", D3, "100.000000", kind="etf")
    add_price(con, "LEV", D4, "130.000000", kind="etf")

    result = run(con)
    stocks = only(result, security_type="stock", band="$10+",
                  direction="gainers", liquidity="all")
    etfs = only(result, security_type="etf", band="$10+",
                direction="gainers", liquidity="all")

    assert [m.ticker for m in stocks.rows] == ["STK"]
    assert [m.ticker for m in etfs.rows] == ["LEV"]


# --- liquidity ----------------------------------------------------------


def test_liquid_set_is_parallel_and_does_not_remove_from_all(con) -> None:
    add_price(con, "THIN", D3, "10.000000", volume=1)
    add_price(con, "THIN", D4, "12.000000", volume=1)
    add_price(con, "DEEP", D3, "10.000000", volume=10_000_000)
    add_price(con, "DEEP", D4, "11.000000", volume=10_000_000)

    # min_adv_sessions=1: these fixtures carry two sessions by design, and
    # the point under test is that the gated set is parallel rather than
    # subtractive. The session floor has its own test below.
    result = run(con, min_adv_sessions=1)
    every = only(result, security_type="stock", band="$10+",
                 direction="gainers", liquidity="all")
    liquid = only(result, security_type="stock", band="$10+",
                  direction="gainers", liquidity="liquid")

    assert [m.ticker for m in every.rows] == ["THIN", "DEEP"]
    assert [m.ticker for m in liquid.rows] == ["DEEP"]


# --- the sanity floor ---------------------------------------------------


def test_floor_excludes_and_counts_rather_than_silently_dropping(con) -> None:
    add_price(con, "DUST", D3, "0.005000")
    add_price(con, "DUST", D4, "0.009000")
    add_price(con, "REAL", D3, "10.000000")
    add_price(con, "REAL", D4, "11.000000")

    result = run(con)
    assert result.floor_excluded == 1
    assert result.moves_screened == 1
    assert "sanity floor" in "\n".join(vol.render(result))


def test_floor_is_not_a_liquidity_filter(con) -> None:
    """A one-share stock above the floor still screens."""
    add_price(con, "TINY", D3, "10.000000", volume=1)
    add_price(con, "TINY", D4, "12.000000", volume=1)

    result = run(con)
    assert result.floor_excluded == 0
    assert only(result, security_type="stock", band="$10+",
                direction="gainers", liquidity="all").rows[0].ticker == "TINY"


# --- direction ----------------------------------------------------------


def test_losers_never_contains_a_gainer(con) -> None:
    """Regression: sorting ascending on a short list is not a loser list."""
    add_price(con, "UP", D3, "10.000000")
    add_price(con, "UP", D4, "11.000000")
    add_price(con, "FLAT", D3, "20.000000")
    add_price(con, "FLAT", D4, "20.000000")

    result = run(con)
    losers = only(result, security_type="stock", band="$10+",
                  direction="losers", liquidity="all")
    gainers = only(result, security_type="stock", band="$10+",
                   direction="gainers", liquidity="all")

    assert losers.rows == []
    assert [m.ticker for m in gainers.rows] == ["UP"]


def test_top_n_is_respected(con) -> None:
    for i in range(30):
        add_price(con, f"T{i:02d}", D3, "10.000000")
        add_price(con, f"T{i:02d}", D4, str(Decimal("10") + Decimal(i) / 10))

    result = run(con, top_n=20)
    assert len(only(result, security_type="stock", band="$10+",
                    direction="gainers", liquidity="all").rows) == 20


# --- freshness ----------------------------------------------------------


def test_empty_input_raises_rather_than_exiting_green(con) -> None:
    with pytest.raises(StaleDataError):
        run(con)


def test_single_day_of_prices_has_no_move_and_raises(con) -> None:
    """One bar is not a move. This must fail, not return empty lists."""
    add_price(con, "ONE", D4, "10.000000")
    with pytest.raises(StaleDataError):
        run(con)


# --- types --------------------------------------------------------------


def test_output_is_quantized_to_the_storage_scale(con) -> None:
    add_price(con, "Q", D3, "3.000000")
    add_price(con, "Q", D4, "4.000000")
    add_action(con, "Q", D4, split_factor="3")

    rel = vol.moves(con, prices=con.table("px"), actions=con.table("act"))
    types = {name: str(t) for name, t in zip(rel.columns, rel.types)}
    assert types["adj_prev_close"] == "DECIMAL(18,6)"
    assert types["pct_move"] == "DECIMAL(18,6)"
    assert types["tick_move"] == "DECIMAL(18,6)"
    # 3.000000 / 3 is exact; the widened intermediate is what protects the
    # cases that are not.
    assert rel.fetchall()[0][6] == Decimal("1.000000")


# --- the ADV window -----------------------------------------------------


def test_adv_is_a_trailing_window_not_the_whole_partition(con) -> None:
    """The bug the backfill would have switched on.

    A name that traded $50M/day two years ago and $200k/day now must not pass
    a $5M gate on its own history. With the old whole-partition average it
    did, and nothing failed -- the number was valid, just answering a
    question nobody asked.
    """
    for n in range(40):
        add_price(con, "FADED", date(2026, 1, 1) + timedelta(days=n),
                  "10.000000", volume=5_000_000 if n < 10 else 1_000)

    rel = vol_moves(con)
    rows = {r["date"]: r for r in rel}
    early = rows[date(2026, 1, 5)]
    late = rows[date(2026, 2, 9)]

    assert early["avg_dollar_volume"] > late["avg_dollar_volume"] * 100, (
        "the window is not trailing -- old volume is still in the average"
    )


def vol_moves(con, **kw):
    rel = vol.moves(con, prices=con.table("px"), actions=con.table("act"), **kw)
    return [dict(zip(rel.columns, r)) for r in rel.fetchall()]


def test_the_window_is_capped_at_its_length(con) -> None:
    for n in range(50):
        add_price(con, "LONG", date(2026, 1, 1) + timedelta(days=n),
                  "10.000000", volume=1_000_000)
    sessions = {r["adv_sessions"] for r in vol_moves(con)}
    assert max(sessions) == vol.ADV_WINDOW_SESSIONS


def test_adv_sessions_reports_the_real_sample_size(con) -> None:
    """A three-day mean must not be presentable as a thirty-day one."""
    for n in range(4):
        add_price(con, "NEW", date(2026, 9, 1) + timedelta(days=n),
                  "10.000000", volume=10_000_000)
    assert max(r["adv_sessions"] for r in vol_moves(con)) == 4


# --- the session floor --------------------------------------------------


def test_a_thin_name_is_excluded_from_the_gated_lists(con) -> None:
    """The decision, made explicit: the gate claims "liquid over 30 sessions",
    and a name without 30 sessions has not earned that claim."""
    add_price(con, "IPO", D3, "10.000000", volume=10_000_000)
    add_price(con, "IPO", D4, "11.000000", volume=10_000_000)

    result = run(con)
    every = only(result, security_type="stock", band="$10+",
                 direction="gainers", liquidity="all")
    liquid = only(result, security_type="stock", band="$10+",
                  direction="gainers", liquidity="liquid")

    assert [m.ticker for m in every.rows] == ["IPO"], "still in the ungated list"
    assert liquid.rows == [], "and out of the gated one"
    assert result.thin_history == 1


def test_the_exclusion_is_counted_and_reported(con) -> None:
    """Nothing is hidden: it is in the ungated list and the count is printed."""
    add_price(con, "IPO", D3, "10.000000", volume=10_000_000)
    add_price(con, "IPO", D4, "11.000000", volume=10_000_000)
    text = "\n".join(vol.render(run(con)))
    assert "fewer than 30 sessions" in text
    assert "ungated" in text


def test_the_session_floor_is_a_parameter(con) -> None:
    add_price(con, "IPO", D3, "10.000000", volume=10_000_000)
    add_price(con, "IPO", D4, "11.000000", volume=10_000_000)
    result = run(con, min_adv_sessions=1)
    liquid = only(result, security_type="stock", band="$10+",
                  direction="gainers", liquidity="liquid")
    assert [m.ticker for m in liquid.rows] == ["IPO"]
    assert result.thin_history == 0


def test_a_thin_name_below_the_dollar_gate_is_not_counted_as_thin(con) -> None:
    """thin_history means "would have qualified but for the sample size"."""
    add_price(con, "TINY", D3, "10.000000", volume=1)
    add_price(con, "TINY", D4, "11.000000", volume=1)
    assert run(con).thin_history == 0
