"""Forward-return study. No network.

Every assertion here is a defect the first run of the real study produced.
An event study is easy to write and easy to get quietly wrong: the anchor
lands on the wrong side of the event, a split is missed, a hole in the price
series anchors on a close from four years earlier. All three happened.
"""

from __future__ import annotations

from datetime import date, timedelta

import duckdb
import pytest

from marketradar.screens import outcomes


def sessions(start: date, n: int) -> list[date]:
    """n consecutive weekdays. The engine counts sessions, not calendar days."""
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
    c.execute("create table ca (ticker text, ex_date date, "
              "split_factor double, div_cash double)")
    c.execute("create table ev (event_id text, ticker text, event_date date)")
    return c


def prices(con, ticker: str, start: date, closes: list[float]) -> None:
    days = sessions(start, len(closes))
    con.executemany("insert into px values (?,?,?)",
                    [(ticker, d, c) for d, c in zip(days, closes)])


def run(con, **kw):
    return outcomes.forward_returns(
        con, con.table("ev"), con.table("px"), con.table("ca"), **kw
    )


# --- the anchor ---------------------------------------------------------


def test_anchor_is_the_last_close_before_the_event() -> None:
    """An 8-K accepted at 16:05 moves the next session; we do not hold the
    acceptance time, so the anchor sits before the filing either way."""
    c = duckdb.connect()
    c.execute("create table px (ticker text, date date, close double)")
    c.execute("create table ca (ticker text, ex_date date, "
              "split_factor double, div_cash double)")
    c.execute("create table ev (event_id text, ticker text, event_date date)")
    days = sessions(date(2024, 1, 1), 40)
    prices(c, "AAA", date(2024, 1, 1), [100.0] * 5 + [110.0] * 35)
    # Event on day index 5, whose close is the first 110.
    c.execute("insert into ev values ('e1', 'AAA', ?)", [days[5]])

    rel = run(c)
    c.register("r", rel)
    got = c.execute(
        "select base_date, base_close, fwd_date, fwd_close, ret "
        "from r where h = 1"
    ).fetchone()
    assert got[0] == days[4]          # anchor: the session before
    assert got[1] == pytest.approx(100.0)
    assert got[2] == days[5]          # h=1 is the event session itself
    assert got[3] == pytest.approx(110.0)
    assert float(got[4]) == pytest.approx(0.10)


def test_horizons_are_sessions_not_calendar_days(con) -> None:
    days = sessions(date(2024, 1, 1), 40)
    prices(con, "AAA", date(2024, 1, 1), [float(100 + i) for i in range(40)])
    con.execute("insert into ev values ('e1', 'AAA', ?)", [days[10]])
    con.register("r", run(con))
    got = dict(con.execute("select h, fwd_date from r").fetchall())
    # Five sessions after the anchor at index 9, i.e. index 14 -- which is
    # more than five calendar days later once a weekend intervenes.
    assert got[5] == days[14]
    assert (got[5] - days[9]).days > 5


def test_a_hole_in_the_series_does_not_anchor_years_back(con) -> None:
    """SOUL anchored a 2025 event on a 2020 close of $0.0001 and reported
    10,049,900%. The guard matches the volatility screens' 30 days."""
    con.executemany("insert into px values (?,?,?)", [
        ("SOUL", date(2020, 8, 4), 0.0001),
    ])
    prices(con, "SOUL", date(2025, 4, 8), [10.0] * 40)
    con.execute("insert into ev values ('e1', 'SOUL', ?)", [date(2025, 4, 7)])
    con.register("r", run(con))
    assert con.execute("select count(*) from r").fetchone()[0] == 0


def test_an_anchor_inside_the_guard_is_kept(con) -> None:
    prices(con, "AAA", date(2024, 1, 1), [100.0] * 40)
    days = sessions(date(2024, 1, 1), 40)
    # Day 5, not day 20: the anchor plus thirty sessions has to fit inside
    # the fixture or the +30d row is missing for a reason unrelated to the
    # guard being tested.
    con.execute("insert into ev values ('e1', 'AAA', ?)", [days[5]])
    con.register("r", run(con))
    assert con.execute("select count(*) from r").fetchone()[0] == 3


# --- splits -------------------------------------------------------------


def test_a_split_inside_the_window_is_adjusted_out(con) -> None:
    """A 2-for-1 halves the quote. Uncorrected it reads as -50%."""
    days = sessions(date(2024, 1, 1), 40)
    closes = [100.0] * 12 + [50.0] * 28          # 2-for-1 at index 12
    prices(con, "AAA", date(2024, 1, 1), closes)
    con.execute("insert into ca values ('AAA', ?, 2.0, 0.0)", [days[12]])
    con.execute("insert into ev values ('e1', 'AAA', ?)", [days[10]])
    con.register("r", run(con))
    ret30 = float(con.execute("select ret from r where h = 30").fetchone()[0])
    assert ret30 == pytest.approx(0.0, abs=1e-9)


def test_a_reverse_split_with_no_action_on_record_is_flagged(con) -> None:
    """AYTU's 1-for-20 of 2023-01-06 is absent from corporate_actions and
    reads as +1,751%. Flagged, not deleted: real takeouts live here too."""
    days = sessions(date(2024, 1, 1), 40)
    prices(con, "AAA", date(2024, 1, 1), [1.0] * 12 + [20.0] * 28)
    con.execute("insert into ev values ('e1', 'AAA', ?)", [days[10]])
    con.register("r", run(con))
    row = con.execute(
        "select ret, suspect_unadjusted from r where h = 30").fetchone()
    assert float(row[0]) == pytest.approx(19.0)
    assert row[1] is True


def test_a_large_move_with_an_action_on_record_is_not_flagged(con) -> None:
    days = sessions(date(2024, 1, 1), 40)
    prices(con, "AAA", date(2024, 1, 1), [1.0] * 12 + [20.0] * 28)
    con.execute("insert into ca values ('AAA', ?, 0.05, 0.0)", [days[12]])
    con.execute("insert into ev values ('e1', 'AAA', ?)", [days[10]])
    con.register("r", run(con))
    row = con.execute(
        "select ret, suspect_unadjusted from r where h = 30").fetchone()
    assert float(row[0]) == pytest.approx(0.0, abs=1e-9)
    assert row[1] is False


# --- the benchmark ------------------------------------------------------


def test_excess_is_the_return_less_the_benchmark(con) -> None:
    """A +4% move in a +4% market is zero information."""
    days = sessions(date(2024, 1, 1), 40)
    prices(con, "AAA", date(2024, 1, 1), [100.0] * 11 + [110.0] * 29)
    prices(con, "SPY", date(2024, 1, 1), [400.0] * 11 + [420.0] * 29)
    con.execute("insert into ev values ('e1', 'AAA', ?)", [days[11]])
    con.register("r", run(con))
    row = con.execute(
        "select ret, bench_ret, excess from r where h = 1").fetchone()
    assert float(row[0]) == pytest.approx(0.10)
    assert float(row[1]) == pytest.approx(0.05)
    assert float(row[2]) == pytest.approx(0.05)


def test_a_missing_benchmark_leaves_excess_null_not_zero(con) -> None:
    days = sessions(date(2024, 1, 1), 40)
    prices(con, "AAA", date(2024, 1, 1), [100.0] * 40)
    con.execute("insert into ev values ('e1', 'AAA', ?)", [days[11]])
    con.register("r", run(con))
    assert con.execute("select excess from r where h = 1").fetchone()[0] is None


# --- run-up -------------------------------------------------------------


def test_run_up_measures_the_five_sessions_before_the_anchor(con) -> None:
    """A signal that only appears after the move is a different thing from
    one that precedes it, and without this column they look identical."""
    days = sessions(date(2024, 1, 1), 40)
    prices(con, "AAA", date(2024, 1, 1), [50.0] * 6 + [100.0] * 34)
    con.execute("insert into ev values ('e1', 'AAA', ?)", [days[11]])
    con.register("r", run(con))
    assert float(con.execute(
        "select run_up from r where h = 1").fetchone()[0]) == pytest.approx(1.0)


# --- coverage and grouping ----------------------------------------------


def test_events_with_no_price_history_are_counted_not_hidden(con) -> None:
    """The drops correlate with the outcome -- a delisted target has no
    +30-session close -- so a study that hides them reports the survivors."""
    days = sessions(date(2024, 1, 1), 40)
    prices(con, "AAA", date(2024, 1, 1), [100.0] * 40)
    con.execute("insert into ev values ('e1', 'AAA', ?)", [days[11]])
    con.execute("insert into ev values ('e2', 'GONE', ?)", [days[11]])
    rel = run(con)
    cov = outcomes.coverage(con, con.table("ev"), rel)
    assert cov["events"] == 2
    assert cov["priced"] == 1


def test_event_columns_are_carried_into_the_result(con) -> None:
    con.execute("alter table ev add column role text")
    days = sessions(date(2024, 1, 1), 40)
    prices(con, "AAA", date(2024, 1, 1), [100.0] * 40)
    con.execute("insert into ev values ('e1', 'AAA', ?, 'insider')", [days[11]])
    con.register("r", run(con))
    assert con.execute(
        "select role from r where h = 1").fetchone()[0] == "insider"


def test_summary_excludes_suspects_and_counts_them(con) -> None:
    days = sessions(date(2024, 1, 1), 40)
    prices(con, "AAA", date(2024, 1, 1), [100.0] * 40)
    prices(con, "BBB", date(2024, 1, 1), [1.0] * 12 + [20.0] * 28)
    con.execute("insert into ev values ('e1', 'AAA', ?)", [days[10]])
    con.execute("insert into ev values ('e2', 'BBB', ?)", [days[10]])
    got = outcomes.summarize(con, run(con), horizons=(30,))
    assert len(got) == 1
    assert got[0].n == 1
    assert got[0].n_suspect == 1
    assert got[0].median_ret == pytest.approx(0.0, abs=1e-9)


def test_no_horizons_is_an_error_not_an_empty_table(con) -> None:
    with pytest.raises(outcomes.OutcomeError):
        run(con, horizons=())


# --- the bias runs the same way as the number ---------------------------


def test_the_survivorship_caveat_is_printed_with_the_numbers() -> None:
    """Not only in a docstring. A reader who sees the excess return without
    it reads a measurement of who is still listed as a fact about deals."""
    from marketradar.screens import outcomes

    text = outcomes.render([], "test study")
    assert "biased downward" in text.lower()
    assert "higher than this" in text.lower()


def test_the_caveat_names_the_direction_not_only_its_existence() -> None:
    """'There is survivorship bias' is not actionable. Which way it pushes
    the number is: completing a deal delists the target, so the survivors are
    weighted toward deals that failed and the correction goes up."""
    from marketradar.screens import outcomes

    caveat = outcomes.SURVIVOR_CAVEAT.lower()
    assert "delists" in caveat
    assert "failed" in caveat
    assert "higher" in caveat


def test_the_panel_marks_the_excess_columns_themselves() -> None:
    """The caveat has to reach the cell, not just sit at the top of a page
    somebody scrolled past."""
    from marketradar.dashboard import panels

    page = panels.outcomes_html([{
        "study": "8-K deals", "slice": "all", "horizon": 30, "n": 8000,
        "median_ret": "-0.01", "mean_ret": "-0.01",
        "median_excess": "-0.0236", "mean_excess": "-0.02",
        "win_rate": "0.44", "median_run_up": "0.01", "n_suspect": 3,
        "events": 10689, "priced": 8000, "benchmark": "SPY",
    }])
    assert "biased" in page
    assert "-2.36%" in page
    # and the direction, next to the number rather than in a tooltip alone
    assert "correction goes" in page.lower() or "goes\n            <strong>up" in page
