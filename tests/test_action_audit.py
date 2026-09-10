"""The unexplained-move detector. No network.

Written after the defect it detects: ``corporate_actions`` held 365 splits
where the staged parquet held 3,724, because the upsert issued one round trip
per row for a quarter of a million rows and then reported ``len(rows)``
whatever happened. AYTU's 1-for-20 reverse split of 2023-01-06 sat in the
price history reading as a genuine +1,751% move.

The point of these tests is that the detector needs no knowledge of the
loader: it reads the prices and the action table and reports where they
disagree, so it catches the next cause too.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import duckdb
import pytest

from marketradar.screens import action_audit


def sessions(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


@pytest.fixture()
def con() -> duckdb.DuckDBPyConnection:
    c = duckdb.connect()
    c.execute("create table px (ticker text, date date, close double)")
    c.execute("create table ca (ticker text, ex_date date)")
    return c


def prices(con, ticker: str, start: date, closes: list[float]) -> None:
    con.executemany(
        "insert into px values (?,?,?)",
        [(ticker, d, v) for d, v in zip(sessions(start, len(closes)), closes)],
    )


def run(con, **kw):
    return action_audit.candidates(con, con.table("px"), con.table("ca"), **kw)


def test_a_reverse_split_with_no_action_is_flagged(con) -> None:
    """The AYTU shape: a 20x jump with nothing in corporate_actions."""
    prices(con, "AYTU", date(2023, 1, 2), [0.19] * 3 + [3.80] * 3)
    rows = run(con).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "AYTU"
    assert rows[0][6] == "jump"


def test_the_same_split_with_an_action_is_not_flagged(con) -> None:
    days = sessions(date(2023, 1, 2), 6)
    prices(con, "AYTU", date(2023, 1, 2), [0.19] * 3 + [3.80] * 3)
    con.execute("insert into ca values ('AYTU', ?)", [days[3]])
    assert run(con).fetchall() == []


def test_the_action_only_counts_inside_the_interval(con) -> None:
    """A range join, not an equality one -- but an action a month before the
    jump does not explain the jump either."""
    prices(con, "AYTU", date(2023, 1, 2), [0.19] * 3 + [3.80] * 3)
    con.execute("insert into ca values ('AYTU', ?)", [date(2022, 11, 1)])
    assert len(run(con).fetchall()) == 1


def test_a_forward_split_reads_as_a_fall(con) -> None:
    prices(con, "BBB", date(2024, 3, 4), [300.0] * 3 + [100.0] * 3)
    rows = run(con).fetchall()
    assert len(rows) == 1
    assert rows[0][6] == "fall"


def test_an_ordinary_move_is_left_alone(con) -> None:
    """The thresholds exist so the detector is readable, not exhaustive."""
    prices(con, "CCC", date(2024, 3, 4), [10.0, 10.5, 11.2, 9.8, 12.0, 11.0])
    assert run(con).fetchall() == []


def test_a_real_double_is_flagged_and_that_is_accepted(con) -> None:
    """A genuine one-session double trips the jump threshold. The tolerance
    in the health block is set above the noise for exactly this reason -- the
    detector reports candidates, not verdicts."""
    prices(con, "DDD", date(2024, 3, 4), [5.0] * 3 + [11.0] * 3)
    assert len(run(con).fetchall()) == 1


def test_sub_penny_quotes_are_floored_out(con) -> None:
    """Dividing by a stale sub-penny quote invents percentages from noise.
    Same floor the screens use."""
    prices(con, "EEE", date(2024, 3, 4), [0.0002] * 3 + [0.0009] * 3)
    assert run(con).fetchall() == []


def test_a_move_across_a_long_gap_is_not_a_move(con) -> None:
    """SOUL's five-year hole produced a 10,049,900% "return" in the outcome
    study for the same reason."""
    con.execute("insert into px values ('FFF', ?, 0.10)", [date(2020, 1, 6)])
    prices(con, "FFF", date(2025, 4, 8), [10.0] * 3)
    assert run(con).fetchall() == []


def test_since_restricts_the_window_but_keeps_the_prior_close(con) -> None:
    """A nightly check wants new candidates. The first session in the window
    still needs something to compare against."""
    prices(con, "GGG", date(2024, 3, 4), [1.0] * 10 + [30.0] * 5)
    days = sessions(date(2024, 3, 4), 15)
    jump_day = days[10]

    assert len(run(con, since=jump_day).fetchall()) == 1
    # A window that starts after the jump must not report it.
    assert run(con, since=days[12]).fetchall() == []


def test_summarize_splits_jumps_from_falls(con) -> None:
    prices(con, "UP", date(2024, 3, 4), [1.0] * 3 + [20.0] * 3)
    prices(con, "DOWN", date(2024, 3, 4), [300.0] * 3 + [100.0] * 3)
    stats = action_audit.summarize(con, run(con))
    assert stats == {"total": 2, "jumps": 1, "falls": 1, "tickers": 2,
                     "newest": stats["newest"]}


def test_implied_split_reads_as_a_ratio() -> None:
    up = action_audit.Candidate(
        ticker="AYTU", date=date(2023, 1, 6), prev_date=date(2023, 1, 5),
        prev_close=Decimal("0.19"), close=Decimal("3.80"),
        ratio=Decimal("20"), direction="jump")
    assert up.implied_split == "1-for-20"

    down = action_audit.Candidate(
        ticker="BBB", date=date(2024, 3, 7), prev_date=date(2024, 3, 6),
        prev_close=Decimal("300"), close=Decimal("100"),
        ratio=Decimal("0.3333333333"), direction="fall")
    assert down.implied_split.endswith("-for-1")


def test_health_line_is_ascii_and_says_nothing_when_clean() -> None:
    """The console here is cp1252; a non-ASCII glyph raises on print."""
    clean = action_audit.health_line(
        {"total": 0, "jumps": 0, "falls": 0, "tickers": 0, "newest": None})
    dirty = action_audit.health_line(
        {"total": 9, "jumps": 7, "falls": 2, "tickers": 5,
         "newest": date(2026, 9, 8)})
    for line in (clean, dirty):
        line.encode("ascii")
    assert "no unexplained moves" in clean
    assert "7" in dirty and "candidate missing splits" in dirty


def test_render_is_ascii_and_caps_its_output(con) -> None:
    for i in range(6):
        prices(con, f"T{i}", date(2024, 3, 4), [1.0] * 3 + [20.0 + i] * 3)
    rows = run(con).fetchall()
    out = action_audit.render(rows, limit=2)
    out.encode("ascii")
    assert "and 4 more" in out
