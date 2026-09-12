"""Deal multiples: who the target was, and what it last reported.

The screen's real output on real data is ten rows out of 10,762 deal candidates,
and that is not a bug in the screen -- the deals population is survivor-biased by
a factor of thirty and an acquisition target is a company that stopped filing.
Which is exactly why these fixtures are synthetic: on the real population the
confirming case barely fires, so a test built on it would prove the logic works
by never exercising it.

One filer per decision, named for the decision.
"""

from __future__ import annotations

from datetime import date

import duckdb
import pytest

from marketradar.screens import deal_multiples as dm

TODAY = date(2026, 9, 10)

#: Loaded filing history ends here, which is what bounds `too_recent`.
COVERAGE_TO = date(2026, 3, 31)


def setup(con: duckdb.DuckDBPyConnection, deals: list[tuple],
          facts: list[tuple]) -> None:
    """Build the two relations the screen reads, and nothing else."""
    con.execute("""
        create or replace table deals (
            accession varchar, cik varchar, company varchar, filed_date date,
            value_usd decimal(28,4), consideration varchar, items varchar,
            deal_type varchar, filer_role varchar
        )
    """)
    if deals:
        con.executemany(
            "insert into deals values (?, ?, ?, ?, ?, ?, ?, ?, ?)", deals)
    con.execute("""
        create or replace table xb (
            cik varchar, period_end date, concept varchar,
            value decimal(28,4), tag varchar, status varchar
        )
    """)
    if facts:
        con.executemany("insert into xb values (?, ?, ?, ?, ?, ?)", facts)
    # **The filer universe, built rather than fallen back to.**
    #
    # The screen used to warn and silently use the fundamentals table for identity
    # when this was absent, and two tests here were passing through that fallback
    # without saying so. On real data the fallback overcounts confirmed targets by
    # about 6.5x -- 280 rows against 43 -- so the screen now raises, and a test that
    # wants the weaker path has to pass `filers=None` and mean it.
    #
    # Derived from the same facts, so a fixture cannot drift from what the screen
    # sees: `last_period` is the newest period the filer reported anything for.
    con.execute("""
        create or replace table sec_filers as
        select cik,
               max(period_end) as last_period,
               max(period_end) as last_filed,
               'stopped'       as status
        from xb
        group by cik
    """)


def deal(accession: str, cik: str, company: str, filed: date,
         value: float | None = 1_000.0, *, role: str = "seller",
         deal_type: str = "operating") -> tuple:
    return (accession, cik, company, filed,
            None if value is None else value, "cash", "2.01", deal_type, role)


def annual(cik: str, period_end: date, revenue: float | None = 500.0,
           net_income: float | None = 50.0) -> list[tuple]:
    """One annual report's worth of resolved facts."""
    out = []
    for concept, value, tag in (("revenue", revenue, "Revenues"),
                                ("net_income", net_income, "NetIncomeLoss")):
        out.append((cik, period_end, concept,
                    None if value is None else value, tag,
                    "stated" if value is not None else "absent"))
    return out


# --- the identity question ----------------------------------------------


def test_a_target_that_stopped_filing_gets_a_multiple() -> None:
    """The confirming case: last annual report before the deal, nothing after,
    and long enough ago that the absence means something."""
    con = duckdb.connect()
    setup(
        con,
        [deal("a-1", "0000000100", "ACQUIRED CO", date(2022, 6, 1), 1_000.0)],
        annual("0000000100", date(2021, 12, 31), revenue=500.0)
        + annual("0000000999", COVERAGE_TO, revenue=10.0),  # bounds coverage
    )
    res = dm.screen(con, today=TODAY)
    assert res.identities.get(dm.ACQUIRED_WHOLE) == 1
    assert len(res.rows) == 1
    row = res.rows[0]
    assert row.company == "ACQUIRED CO"
    assert row.period_end == date(2021, 12, 31), "point-in-time, pre-deal"
    assert row.value_to_revenue == pytest.approx(2.0)
    assert row.value_to_net_income == pytest.approx(20.0)
    assert row.tags == {"revenue": "Revenues", "net_income": "NetIncomeLoss"}
    assert row.usable


def test_a_filer_that_kept_filing_is_excluded_and_counted() -> None:
    """The 84% case, and the one that would have poisoned every number.

    A company that sold a division keeps filing. Dividing the division's price
    by the parent's whole revenue gives a small multiple, and a screen for cheap
    deals would rank its own errors first.
    """
    con = duckdb.connect()
    setup(
        con,
        [deal("b-1", "0000000200", "DIVESTOR CO", date(2022, 6, 1), 100.0)],
        annual("0000000200", date(2021, 12, 31))
        + annual("0000000200", date(2022, 12, 31))   # filed after the deal
        + annual("0000000999", COVERAGE_TO),
    )
    res = dm.screen(con, today=TODAY)
    assert res.identities.get(dm.KEPT_FILING) == 1
    assert res.identities.get(dm.ACQUIRED_WHOLE, 0) == 0
    assert res.rows == [], "a division sale produced a multiple"


def test_a_recent_deal_is_too_recent_rather_than_acquired() -> None:
    """Third outing for this distinction, after Form 5500's pending_years and
    the XBRL nil tag: a row that is not there yet and a row that is not there
    are different facts, and the arithmetic that treats them alike never errors.
    """
    con = duckdb.connect()
    setup(
        con,
        [deal("c-1", "0000000300", "JUST SOLD CO", date(2026, 2, 1), 900.0)],
        annual("0000000300", date(2025, 12, 31))
        + annual("0000000999", COVERAGE_TO),
    )
    res = dm.screen(con, today=TODAY)
    assert res.identities.get(dm.TOO_RECENT) == 1
    assert res.identities.get(dm.ACQUIRED_WHOLE, 0) == 0
    assert res.rows == []


def test_the_confirmation_lag_is_measured_against_loaded_coverage() -> None:
    """Not just against today. The answer moves when more quarters are loaded,
    so the edge of the loaded history bounds it as well as the calendar does --
    otherwise a deal would read as confirmed purely because the partitions stop
    shortly after it.
    """
    con = duckdb.connect()
    # The deal is years in the past by the calendar, but loaded history ends
    # only months after it.
    setup(
        con,
        [deal("d-1", "0000000400", "EDGE CO", date(2022, 6, 1), 900.0)],
        annual("0000000400", date(2021, 12, 31))
        + annual("0000000999", date(2022, 9, 30)),
    )
    res = dm.screen(con, today=TODAY)
    assert res.coverage_to == date(2022, 9, 30)
    assert res.identities.get(dm.TOO_RECENT) == 1, (
        "a deal near the edge of loaded history was confirmed on four months "
        "of evidence"
    )


# --- what is excluded, by name ------------------------------------------


def test_an_acquirer_is_excluded_before_anything_else() -> None:
    """The one thing the prose is trusted for. A filing that says it is the
    buyer is not the target, and admitting it would divide the price it paid by
    its own revenue."""
    con = duckdb.connect()
    setup(
        con,
        [deal("e-1", "0000000500", "BUYER CO", date(2022, 6, 1), 900.0,
              role="acquirer")],
        annual("0000000500", date(2021, 12, 31))
        + annual("0000000999", COVERAGE_TO),
    )
    res = dm.screen(con, today=TODAY)
    stages = {s.name: s.remaining for s in res.funnel.stages}
    assert stages["deal candidates"] == 1
    assert stages["filer may be the target"] == 0
    assert res.rows == []


def test_a_deal_with_no_stated_value_is_excluded() -> None:
    con = duckdb.connect()
    setup(
        con,
        [deal("f-1", "0000000600", "NO PRICE CO", date(2022, 6, 1), None)],
        annual("0000000600", date(2021, 12, 31))
        + annual("0000000999", COVERAGE_TO),
    )
    res = dm.screen(con, today=TODAY)
    stages = {s.name: s.remaining for s in res.funnel.stages}
    assert stages["value stated"] == 0


def test_a_spac_is_excluded_by_the_classification_already_made() -> None:
    """A de-SPAC has no operating acquirer and no target financials. Excluded on
    the deals loader's own classification rather than re-derived here."""
    con = duckdb.connect()
    setup(
        con,
        [deal("g-1", "0000000700", "SHELL CO", date(2022, 6, 1), 900.0,
              deal_type="spac")],
        annual("0000000700", date(2021, 12, 31))
        + annual("0000000999", COVERAGE_TO),
    )
    res = dm.screen(con, today=TODAY)
    stages = {s.name: s.remaining for s in res.funnel.stages}
    assert stages["operating deal"] == 0


def test_a_target_outside_the_loaded_range_is_counted_out() -> None:
    """The XBRL range starts at 2019q1 because v1 is post-606. A 2017 deal
    cannot match, and that is a coverage limit rather than a missing row."""
    con = duckdb.connect()
    setup(
        con,
        [deal("h-1", "0000000800", "OLD DEAL CO", date(2017, 6, 1), 900.0)],
        annual("0000000999", COVERAGE_TO),
    )
    res = dm.screen(con, today=TODAY)
    stages = {s.name: s.remaining for s in res.funnel.stages}
    assert stages["target appears in XBRL"] == 0


# --- the ratio itself ---------------------------------------------------


def test_a_non_positive_denominator_is_no_multiple_rather_than_a_small_one() -> None:
    """A loss-making target at a positive price gives a negative ratio that
    sorts below every real one, and a zero-revenue target gives infinity.
    Neither is a multiple, so neither is a number -- the same rule as the nil
    XBRL tag: no value is not a value of zero.
    """
    con = duckdb.connect()
    setup(
        con,
        [deal("i-1", "0000000900", "LOSSMAKER CO", date(2022, 6, 1), 900.0),
         deal("i-2", "0000001000", "NO REVENUE CO", date(2022, 6, 1), 900.0)],
        annual("0000000900", date(2021, 12, 31), revenue=500.0,
               net_income=-200.0)
        + annual("0000001000", date(2021, 12, 31), revenue=0.0)
        + annual("0000000999", COVERAGE_TO),
    )
    res = dm.screen(con, today=TODAY)
    by_company = {r.company: r for r in res.rows}
    loss = by_company["LOSSMAKER CO"]
    assert loss.value_to_revenue == pytest.approx(1.8)
    assert loss.value_to_net_income is None, "a negative multiple was reported"
    # Zero revenue resolves, so the row exists; the ratio does not.
    assert "NO REVENUE CO" in by_company
    assert by_company["NO REVENUE CO"].value_to_revenue is None


def test_the_pre_deal_report_is_the_one_before_the_deal() -> None:
    """Point-in-time, which is the whole reason these figures come from the
    Financial Statement Data Sets rather than companyfacts. Taking the newest
    report instead would value a 2021 deal on 2022 revenue.
    """
    con = duckdb.connect()
    setup(
        con,
        [deal("j-1", "0000001100", "TWO YEARS CO", date(2022, 3, 1), 1_000.0)],
        annual("0000001100", date(2020, 12, 31), revenue=100.0)
        + annual("0000001100", date(2021, 12, 31), revenue=250.0)
        + annual("0000000999", COVERAGE_TO),
    )
    res = dm.screen(con, today=TODAY)
    row = res.rows[0]
    assert row.period_end == date(2021, 12, 31)
    assert row.value_to_revenue == pytest.approx(4.0)


# --- the funnel ---------------------------------------------------------


def test_every_stage_carries_a_reason() -> None:
    """Per the funnel rule: a number with no reason attached is the number
    nobody checks."""
    con = duckdb.connect()
    setup(con, [deal("k-1", "0000001200", "A CO", date(2022, 6, 1))],
          annual("0000001200", date(2021, 12, 31))
          + annual("0000000999", COVERAGE_TO))
    res = dm.screen(con, today=TODAY)
    assert res.funnel.screen == dm.SCREEN
    for stage in res.funnel.stages:
        assert stage.why.strip(), f"{stage.name} has no reason"
    assert res.funnel.stages[0].name == "deal candidates"


def test_the_collapse_is_marked_and_says_it_is_meant_to() -> None:
    """The identity stage removes nearly everything on real data -- 21 of 2,320
    -- and the funnel marks it.

    What the marking says matters. A 99% cut reads as a filter that is too
    strict, and the next person loosens it; it has to say that the cut is the
    point, because most 8-K deal filings are by companies that go on existing,
    which is what an acquirer or a divesting parent is.
    """
    con = duckdb.connect()
    kept = [deal(f"m-{i}", f"000000{2000 + i}", f"CO {i}", date(2022, 6, 1))
            for i in range(20)]
    facts: list[tuple] = []
    for i in range(20):
        cik = f"000000{2000 + i}"
        facts += annual(cik, date(2021, 12, 31))
        facts += annual(cik, date(2022, 12, 31))   # all kept filing
    facts += annual("0000000999", COVERAGE_TO)
    setup(con, kept, facts)
    res = dm.screen(con, today=TODAY)
    collapsed = [s.name for s in res.funnel.collapsed]
    assert "target identity confirmed" in collapsed
    stage = next(s for s in res.funnel.stages
                 if s.name == "target identity confirmed")
    assert "meant to collapse" in stage.why, stage.why
    assert "Loosening it" in stage.why, (
        "the marking does not say what happens if the filter is relaxed"
    )


def test_an_empty_population_does_not_raise() -> None:
    """`mr` runs this before any deal is stored, and a screen that cannot run
    has to say so rather than crash the page that reports it."""
    con = duckdb.connect()
    setup(con, [], [])
    res = dm.screen(con, today=TODAY)
    assert res.rows == []
    assert res.coverage_to is None
    assert res.funnel.stages[0].remaining == 0


# --- identity comes from the wide table, not the narrow one --------------


def test_a_filer_that_left_the_narrow_table_is_not_treated_as_acquired() -> None:
    """CleanSpark appeared as a 1.18x takeout. It is alive.

    It had left the *fundamentals* table because its SIC moved into a financial
    class the operating-company filter excludes, and "disappeared from the narrow
    table" was being read as "stopped filing". Only the filer universe can tell
    those apart, which is why it covers every form type and every SIC -- whether
    a company still exists is a different question from whether its income
    statement is comparable.
    """
    con = duckdb.connect()
    setup(
        con,
        [deal("n-1", "0000001300", "RECLASSIFIED CO", date(2022, 6, 1), 900.0)],
        annual("0000001300", date(2021, 12, 31))
        + annual("0000000999", COVERAGE_TO),
    )
    # The wide table knows it kept reporting after the deal, under a SIC the
    # fundamentals table does not carry.
    con.execute("""
        create or replace table sec_filers as select * from (values
            ('0000001300', DATE '2025-12-31', DATE '2026-03-01', 'filing'),
            ('0000000999', DATE '2026-03-31', DATE '2026-05-01', 'filing')
        ) as t(cik, last_period, last_filed, status)
    """)
    res = dm.screen(con, today=TODAY)
    assert res.identities.get(dm.ACQUIRED_WHOLE, 0) == 0, (
        "a live company was reported as an acquisition target"
    )
    assert res.identities.get(dm.KEPT_FILING) == 1
    assert res.rows == []


def test_the_funnel_says_which_table_identity_came_from() -> None:
    """The fallback is the weaker answer, so a reader has to be able to tell
    which one produced the number in front of them."""
    con = duckdb.connect()
    setup(con, [deal("o-1", "0000001400", "A CO", date(2022, 6, 1))],
          annual("0000001400", date(2021, 12, 31))
          + annual("0000000999", COVERAGE_TO))

    narrow = dm.screen(con, filers=None, today=TODAY)
    stage = next(s for s in narrow.funnel.stages
                 if s.name == "target appears in XBRL")
    assert "narrower" in stage.why

    con.execute("""
        create or replace table sec_filers as select * from (values
            ('0000001400', DATE '2021-12-31', DATE '2022-03-01', 'stopped'),
            ('0000000999', DATE '2026-03-31', DATE '2026-05-01', 'filing')
        ) as t(cik, last_period, last_filed, status)
    """)
    wide = dm.screen(con, today=TODAY)
    stage = next(s for s in wide.funnel.stages
                 if s.name == "target appears in XBRL")
    assert "filer universe" in stage.why


def test_a_later_deal_filing_by_the_same_target_excludes_the_earlier_one() -> None:
    """One CIK in the real sample had twenty deal filings between 2019 and 2025 --
    a stream of $1-31M transactions -- and every priced one read as a takeout
    because its 10-K history had ended. It filed an 8-K in September 2025.

    A company cannot be acquired twice, and a later filing is proof it outlived
    the earlier transaction.
    """
    con = duckdb.connect()
    setup(
        con,
        [deal("p-1", "0000001500", "SERIAL CO", date(2021, 6, 1), 10.0),
         deal("p-2", "0000001500", "SERIAL CO", date(2023, 6, 1), 20.0)],
        annual("0000001500", date(2020, 12, 31))
        + annual("0000000999", COVERAGE_TO),
    )
    con.execute("""
        create or replace table sec_filers as select * from (values
            ('0000001500', DATE '2020-12-31', DATE '2021-03-01', 'stopped'),
            ('0000000999', DATE '2026-03-31', DATE '2026-05-01', 'filing')
        ) as t(cik, last_period, last_filed, status)
    """)
    res = dm.screen(con, today=TODAY)
    # Only the last one can be the transaction that ended it.
    assert [r.accession for r in res.rows] == ["p-2"]
    assert res.identities.get(dm.KEPT_FILING) == 1


# --- the warning that became a failure -----------------------------------


def test_a_missing_filer_universe_raises_rather_than_warning() -> None:
    """**A screen that can warn and still publish is a green run on empty data.**

    This used to log "no filer universe in scope; identity falls back to the
    fundamentals table, which is narrower" and carry on. On the completed re-sweep
    the fallback produced **280 usable rows against the universe's 43** -- a 6.5x
    overcount of confirmed acquisitions -- because "stopped appearing in the loaded
    quarters" stands in for "stopped filing anything", which counts every company
    that still files just not 10-Ks.

    The warning was the only thing that said so, and nothing read it. So it raises,
    and the message says what the number would have been.
    """
    con = duckdb.connect()
    setup(con, [], [])
    con.execute("drop table sec_filers")
    with pytest.raises(ValueError, match="6.5x"):
        dm.screen(con, today=TODAY)


def test_the_weaker_test_has_to_be_asked_for(monkeypatch) -> None:
    """`filers=None` is still allowed -- a caller with no universe available is a
    real case. It is visible in the call rather than buried in a log, which is the
    difference that matters."""
    con = duckdb.connect()
    setup(con, [], [])
    con.execute("drop table sec_filers")
    res = dm.screen(con, filers=None, today=TODAY)
    assert res.rows == []
