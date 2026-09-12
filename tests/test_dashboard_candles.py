"""The candle aggregation. Where a sampled bar would be caught.

**The label is not the thing.** The jsdom suite checks that the chart says "weekly
candles" at 5Y, and a *sampled* series would say exactly the same -- mutation proved
it: replacing `aggregate` with `ohlcv[::5]` left every DOM test green. A sampled
candle is a bar that never traded, with its open, high, low and close all taken from
one arbitrary session standing in for a week. So the arithmetic is tested here,
against the daily bars it came from, rather than inferred from a caption.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from marketradar.dashboard import tickers


def _day(i: int) -> date:
    """Weekday i sessions after 2024-01-01, skipping weekends."""
    when = date(2024, 1, 1)
    seen = 0
    while True:
        if when.weekday() < 5:
            if seen == i:
                return when
            seen += 1
        when += timedelta(days=1)


def _bars(n: int) -> list[tuple[date, float, float, float, float, int]]:
    """Sessions whose highs and lows are deliberately not monotonic.

    A rising series would let `max(high)` coincide with the last session's high, so a
    sampled bar that took the last session would match the aggregate by luck. The
    extremes sit in the middle of each week instead.
    """
    out = []
    for i in range(n):
        base = 10.0 + i * 0.1
        bump = 1.5 if i % 5 == 2 else 1.0         # mid-week spike
        dip = 0.7 if i % 5 == 3 else 1.0          # mid-week trough
        out.append((_day(i), round(base, 4), round(base * 1.02 * bump, 4),
                    round(base * 0.98 * dip, 4), round(base * 1.01, 4),
                    1_000 + i))
    return out


def test_a_period_bar_is_the_aggregate_of_its_sessions() -> None:
    """**open of the first, close of the last, max high, min low, sum volume.**

    Checked against the sessions themselves, which is what separates an aggregate
    from a sample. The fixture puts each week's high and low mid-week on purpose: a
    sampler taking the first or last session of the period would otherwise match on
    the extremes by coincidence and pass.
    """
    bars = _bars(40)
    weekly = tickers.aggregate(bars, tickers._week)
    assert weekly, "no weekly bars produced"

    by_week: dict[tuple[int, int], list] = {}
    for row in bars:
        by_week.setdefault(tickers._week(row[0]), []).append(row)

    assert len(weekly) == len(by_week)
    for day, o, h, l, c, v in weekly:
        when = tickers.EPOCH + timedelta(days=day)
        sessions = by_week[tickers._week(when)]
        assert o == sessions[0][1], "open is not the first session's open"
        assert c == sessions[-1][4], "close is not the last session's close"
        assert h == max(r[2] for r in sessions), "high is not the period maximum"
        assert l == min(r[3] for r in sessions), "low is not the period minimum"
        assert v == sum(r[5] for r in sessions), "volume is not the period sum"


def test_the_period_is_stamped_with_its_first_session() -> None:
    """Not the calendar start. A week whose Monday was a holiday opens on the
    Tuesday, and labelling it Monday claims a session that did not happen."""
    bars = _bars(10)
    # Drop the first session of the second week, so that week starts on a Tuesday.
    second = tickers._week(bars[5][0])
    trimmed = [r for r in bars if not (tickers._week(r[0]) == second
                                       and r[0] == bars[5][0])]
    weekly = tickers.aggregate(trimmed, tickers._week)
    stamped = {tickers.EPOCH + timedelta(days=b[0]) for b in weekly}
    assert bars[5][0] not in stamped, "stamped a session that was removed"
    assert bars[6][0] in stamped, "did not stamp the week's actual first session"


def test_no_bar_is_invented_and_none_is_dropped() -> None:
    """Every session lands in exactly one period, and every period has a bar.

    The count is the cheap half; the volume total is the half that catches a session
    counted twice, which is what the duplicate `(ticker, date)` rows in the price
    staging would have done to `sum(volume)`.
    """
    bars = _bars(120)
    for key in (tickers._week, tickers._month):
        agg = tickers.aggregate(bars, key)
        assert len(agg) == len({key(r[0]) for r in bars})
        assert sum(b[5] for b in agg) == sum(r[5] for r in bars), (
            "aggregate volume does not equal the sessions' volume")


def test_an_empty_series_aggregates_to_nothing_rather_than_a_zero_bar() -> None:
    assert tickers.aggregate([], tickers._week) == []


def test_one_session_is_a_bar_whose_four_prices_are_its_own() -> None:
    """A single-session period is a real candle, not a placeholder."""
    bars = _bars(1)
    (day, o, h, l, c, v), = tickers.aggregate(bars, tickers._week)
    assert (o, h, l, c, v) == (bars[0][1], bars[0][2], bars[0][3], bars[0][4],
                               bars[0][5])


def test_two_different_bars_for_one_session_raise_rather_than_averaging() -> None:
    """**A restatement must not be drawn as half of each.**

    The price staging holds 2,042 `(ticker, date)` pairs twice, identical in every
    price column -- `select distinct` resolves those. Two *different* bars for one
    session is a different fact, and aggregating them would produce a candle that is
    neither, with nothing to show it had happened.
    """
    import duckdb

    con = duckdb.connect()
    con.execute("""create table px as select * from (values
        ('AAA', date '2024-01-02', 1.0, 2.0, 0.5, 1.5, 100),
        ('AAA', date '2024-01-02', 1.0, 9.9, 0.5, 1.5, 100),
        ('AAA', date '2024-01-03', 1.5, 2.5, 1.0, 2.0, 200))
        t(ticker, date, open, high, low, close, volume)""")
    with pytest.raises(tickers.DetailError) as exc:
        tickers.build(con, con.table("px"), ["AAA"])
    assert "2024-01-02" in str(exc.value)


def test_identical_duplicate_rows_are_tolerated() -> None:
    """The benign case, which is the one that actually occurs: byte-identical OHLCV
    differing only in a column the chart does not read. Those must pass, or the
    dashboard would refuse to draw 2,042 real sessions."""
    import duckdb

    con = duckdb.connect()
    con.execute("""create table px as select * from (values
        ('AAA', date '2024-01-02', 1.0, 2.0, 0.5, 1.5, 100),
        ('AAA', date '2024-01-02', 1.0, 2.0, 0.5, 1.5, 100),
        ('AAA', date '2024-01-03', 1.5, 2.5, 1.0, 2.0, 200))
        t(ticker, date, open, high, low, close, volume)""")
    out = tickers.build(con, con.table("px"), ["AAA"])
    assert len(out["AAA"]["d"]) == 2, "the duplicate was not collapsed"
    # And the volume is not doubled, which is the thing `distinct` is there for.
    assert [b[5] for b in out["AAA"]["d"]] == [100, 200]
