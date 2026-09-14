"""Promotion: the three legs, what each one is keyed on, and the gate.

Fixtures are built by hand in the shapes the 2026-09-11 measurement found,
because those shapes are what the module is arranged around: a screen list full
of ETFs, a ticker that does not resolve, a deal type that must not promote, and a
cluster whose only identifier is inside a composite key. A fixture of three tidy
filers would test the dict-merging and none of the decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import duckdb
import pytest

from marketradar.screens import promote

DAY = date(2026, 9, 11)


# --- the fake screen ----------------------------------------------------
#
# A stand-in rather than a real `volatility.screen`, which would need eleven
# price partitions and a corporate-actions table to produce four rows. What
# `promote` reads off a ScreenResult is three attributes deep, so the stand-in
# carries exactly those and nothing else -- and a real ScreenResult growing a
# field this module then depended on would fail here rather than silently.


@dataclass(frozen=True)
class FakeMove:
    ticker: str
    security_type: str
    band: str
    pct_move: Decimal
    tick_move: Decimal = Decimal("1")


@dataclass(frozen=True)
class FakeList:
    security_type: str
    band: str
    rows: list[FakeMove]


@dataclass(frozen=True)
class FakeScreen:
    day: date
    lists: list[FakeList]


def screen_with(*moves: FakeMove) -> FakeScreen:
    """One list per (security_type, band), the way the real screen splits."""
    buckets: dict[tuple[str, str], list[FakeMove]] = {}
    for m in moves:
        buckets.setdefault((m.security_type, m.band), []).append(m)
    return FakeScreen(day=DAY, lists=[
        FakeList(security_type=k[0], band=k[1], rows=v)
        for k, v in sorted(buckets.items())
    ])


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    """A `pg` schema shaped like the attached Postgres, with no Postgres.

    `promote` reads three tables through the alias, so the alias is a DuckDB
    schema here. That keeps the SQL under test -- including the `cik_sql`
    wrapping and the `at time zone 'UTC'` cast -- rather than mocked away.
    """
    con = duckdb.connect()
    con.execute("create schema pg")
    con.execute("create table pg.companies (id bigint, cik varchar, name varchar)")
    con.execute("create table pg.company_tickers "
                "(company_id bigint, ticker varchar, last_seen date)")
    con.execute("""create table pg.deals (
        accession varchar, cik varchar, company varchar, filed_date date,
        items varchar, deal_type varchar, filer_role varchar)""")
    con.execute("""create table pg.signals (
        kind varchar, accession varchar, occurred_at timestamptz,
        payload json)""")
    # `companies.cik` padded and `deals.cik` not, which is how the two real
    # stores actually differ. A fixture that spelled them alike would let a
    # half-wrapped join pass.
    con.executemany("insert into pg.companies values (?,?,?)", [
        (1, "0000066740", "3M CO"),
        (2, "0000320193", "Apple Inc."),
        (3, "0001853070", "VOLATO GROUP, INC."),
        (4, "0000001750", "AAR CORP"),
    ])
    con.executemany("insert into pg.company_tickers values (?,?,?)", [
        (1, "MMM", date(2026, 9, 11)),
        (2, "AAPL", date(2026, 9, 11)),
        (3, "SOAR", date(2026, 9, 11)),
        (4, "AIR", date(2026, 9, 11)),
    ])
    return con


def add_deal(con, cik: str, deal_type: str, *, day: date = DAY,
             role: str = "acquirer", items: str = "1.01,9.01") -> None:
    con.execute("insert into pg.deals values (?,?,?,?,?,?,?)",
                [f"acc-{cik}-{deal_type}", cik, f"FILER {cik}", day, items,
                 deal_type, role])


def add_cluster(con, key: str, *, at: str = "2026-09-08 00:00:00+00",
                n_buyers: int = 3, role: str = "insider",
                fund_like: bool = False) -> None:
    con.execute(
        "insert into pg.signals values ('form4_cluster', ?, "
        "cast(? as timestamptz), ?)",
        [key, at,
         '{"n_buyers": %d, "role": "%s", "fund_like": %s}'
         % (n_buyers, role, "true" if fund_like else "false")])


# --- the cluster key ---------------------------------------------------


def test_cluster_cik_reads_the_only_identifier_a_stored_cluster_has() -> None:
    """`form4.load` writes no issuer CIK anywhere but the composite key.

    The payload carries a symbol and an issuer name, and `company_id` is null
    because a cluster can arrive before the entity resolves. So the alternative
    to parsing the key is matching `issuer_name` against `companies.name`, which
    the Form 5500 measurement put at 44.2% precision against EIN ground truth --
    wrong more often than right.
    """
    assert promote.cluster_cik(
        "cluster:0002007919:insider:2026-09-08") == "0002007919"
    # Unpadded in the key is padded on the way out: one representation.
    assert promote.cluster_cik("cluster:66740:ten_percent:2026-01-02") == \
        "0000066740"


@pytest.mark.parametrize("key", [
    "", "nonsense", "cluster:abc:insider:2026-09-08",
    "cluster:123:insider:not-a-date", "cluster:123:insider",
])
def test_cluster_cik_refuses_anything_that_is_not_a_cluster_key(key) -> None:
    """Empty, never a guess.

    A partial parse would put a wrong CIK in the promoted set, and a promoted set
    is a list of things about to have money spent on them. The caller counts the
    empty ones in `unresolved`, so a malformed row is visible rather than gone.
    """
    assert promote.cluster_cik(key) == ""


# --- the legs ----------------------------------------------------------


def test_an_etf_is_a_named_stage_and_not_an_unresolved_ticker(con) -> None:
    """137 of 345 screen tickers on the measured day were ETFs.

    They cannot resolve and never will: an ETF files no 10-K and has no row in
    `company_tickers`. Counting them as `unresolved` would put 137 permanent
    absences in the column a reader is meant to read as a gap worth closing, so
    they leave at their own funnel stage with their own reason.
    """
    result = promote.promote(con, screen_result=screen_with(
        FakeMove("MMM", "stock", "$10+", Decimal("8.5")),
        FakeMove("SPY", "etf", "$10+", Decimal("1.2")),
        FakeMove("QQQ", "etf", "$10+", Decimal("1.4")),
    ))
    assert result.unresolved[promote.VOLATILITY] == 0
    names = [s.name for s in result.funnel.stages]
    assert names[:2] == ["sentinel hits", "not an ETF"]
    assert result.funnel.stages[0].remaining == 3
    assert result.funnel.stages[1].remaining == 1
    assert result.ciks == ["0000066740"]


def test_a_stock_that_does_not_resolve_is_counted_not_dropped(con) -> None:
    """A missing listing is a gap in `company_tickers`, and gaps get counted.

    On the measured day 25 of 208 stock tickers did not resolve. A screen that
    reported 183 promoted names and no remainder would make that 12% invisible.
    """
    result = promote.promote(con, screen_result=screen_with(
        FakeMove("MMM", "stock", "$10+", Decimal("8.5")),
        FakeMove("NOSUCH", "stock", "sub$1", Decimal("41.0")),
    ))
    assert result.unresolved[promote.VOLATILITY] == 1
    assert result.legs[promote.VOLATILITY] == 1
    remaining = {s.name: s.remaining for s in result.funnel.stages}
    assert remaining["not an ETF"] == 2
    assert remaining["keyed on a CIK"] == 1


def test_the_volatility_leg_refuses_rather_than_returning_an_empty_set(con):
    """An empty join is the one result that looks like a correct answer.

    This leg crosses Parquet into Postgres, which is where the CIK
    representation mismatch has failed six times, every time as a believable
    number. So zero resolutions out of a non-empty ticker list raises instead of
    reporting a quiet day.

    Mutated to confirm it fires: it *is* the mutation -- the fixture below is a
    `company_tickers` table with no matching row, which is what a wrong CIK
    representation looks like from here.
    """
    con.execute("delete from pg.company_tickers")
    with pytest.raises(promote.PromoteError, match="0 of 1 stock tickers"):
        promote.promote(con, screen_result=screen_with(
            FakeMove("MMM", "stock", "$10+", Decimal("8.5"))))


def test_a_spac_or_a_securitization_does_not_promote(con) -> None:
    """Neither has an operating business to draw ten pages of.

    A de-SPAC has no target financials at all, and `unclassified` means neither
    classifier fired -- an absence of evidence rather than a deal. `operating`
    and `division_sale` promote, because both are a real filer that just did
    something. What a division sale must never contribute is a *multiple*, and
    that exclusion lives in `deal_multiples` where the arithmetic is.
    """
    add_deal(con, "66740", "operating")
    add_deal(con, "320193", "division_sale")
    add_deal(con, "1853070", "spac")
    add_deal(con, "1750", "unclassified")
    result = promote.promote(con, day=DAY)
    assert result.legs[promote.DEAL_FILING] == 2
    # Sorted, not set-compared: the row order is part of the contract -- the
    # strongest signal first, then the company name. These two are one reason
    # each, so they come back by name, which is not CIK order.
    assert sorted(result.ciks) == ["0000066740", "0000320193"]


def test_the_deal_leg_reads_a_filed_date_and_not_a_window(con) -> None:
    """An 8-K is visible the day it is filed, so its leg needs no lookback."""
    add_deal(con, "66740", "operating", day=DAY)
    add_deal(con, "320193", "operating", day=date(2026, 9, 10))
    result = promote.promote(con, day=DAY)
    assert result.ciks == ["0000066740"]


def test_the_form4_window_is_measured_and_a_one_day_window_finds_nothing(con):
    """`occurred_at` is the first *purchase*, not the day we could see it.

    Measured over 19,127 historical clusters: the gap from a cluster's first buy
    to the filing that made it visible is a median of 3 days and a p90 of 7. A
    job asking for clusters whose `occurred_at` is today therefore finds almost
    nothing on almost every day -- which is exactly what the first run did, 0
    clusters with 28 in the table.

    Mutated to confirm the window is load-bearing: with `form4_window=1` this
    same fixture promotes nothing at all.
    """
    add_cluster(con, "cluster:0000066740:insider:2026-09-08",
                at="2026-09-08 00:00:00+00")
    assert promote.promote(con, day=DAY).ciks == ["0000066740"]
    assert promote.promote(con, day=DAY, form4_window=1).ciks == []
    assert promote.FORM4_WINDOW_DAYS == 7


def test_a_cluster_stored_at_midnight_utc_is_not_a_day_early(con) -> None:
    """`occurred_at::date` through the attached alias is cast by *DuckDB*.

    DuckDB renders a timestamptz in the machine's zone, so a cluster stored at
    `2026-09-08 00:00:00+00` came back as 2026-09-07 on a US-Central box -- a
    day early on every row, which a wide window hides and a narrow one does not.
    `edgar_rss` casts the same column inside a `postgres_query` string, where
    Postgres does the cast and the zone is its own; the two paths are not
    interchangeable.

    Mutated to confirm it fires: dropping `at time zone 'UTC'` from the query
    makes a one-day window on the 8th miss a cluster stamped the 8th.
    """
    add_cluster(con, "cluster:0000066740:insider:2026-09-08",
                at="2026-09-08 00:00:00+00")
    tight = promote.promote(con, day=date(2026, 9, 8), form4_window=1)
    assert tight.ciks == ["0000066740"], (
        "a cluster stamped the 8th fell outside a window ending the 8th, which "
        "is the timezone cast and not the window")


def test_a_malformed_cluster_key_is_unresolved_rather_than_promoted(con):
    add_cluster(con, "cluster:notacik:insider:2026-09-08")
    result = promote.promote(con, day=DAY)
    assert result.ciks == []
    assert result.unresolved[promote.FORM4_CLUSTER] == 1


# --- the union ---------------------------------------------------------


def test_one_filer_on_three_legs_is_one_row_carrying_three_reasons(con):
    """The strongest signal in the system, so it sorts first and says all three.

    A name promoted by a Form 4 cluster whose deck then said "no Form 4 clusters
    on record" would be a contradiction, and `why` is what makes it visible.
    """
    add_deal(con, "66740", "operating")
    add_cluster(con, "cluster:0000066740:ten_percent:2026-09-08", n_buyers=4)
    result = promote.promote(con, screen_result=screen_with(
        FakeMove("MMM", "stock", "$10+", Decimal("8.5")),
        FakeMove("AAPL", "stock", "$10+", Decimal("4.1")),
    ))
    assert result.rows[0].cik == "0000066740"
    assert result.rows[0].reasons == promote.REASONS
    assert result.rows[0].multi
    assert set(result.rows[0].why) == set(promote.REASONS)
    assert not result.rows[1].multi
    assert "1 promoted by more than one sentinel" in \
        result.funnel.stages[-1].why


def test_the_display_name_comes_from_the_entity_table_not_the_longest_leg(con):
    """Each leg reads a different name column, so the pick is leg precedence.

    `companies.name` for the screen, `deals.company` for a filing, nothing at all
    for a cluster. "The longest name wins" was the first rule here and it is
    worse than the coin flip it replaced: it is deterministic *and* prefers
    whichever source happens to be more verbose, so a filing's long legal name
    would beat the entity table's own spelling.

    Mutated to confirm it fires: `max(..., key=len)` over the two names makes
    this return the deal leg's name.
    """
    add_deal(con, "66740", "operating")   # company is "FILER 66740", longer
    result = promote.promote(con, screen_result=screen_with(
        FakeMove("MMM", "stock", "$10+", Decimal("8.5"))))
    assert result.rows[0].company == "3M CO"


def test_a_filer_with_no_name_on_any_leg_falls_back_to_its_cik(con) -> None:
    """A cluster carries no issuer name in its payload's useful form.

    `cik` rather than an empty string, so the row is still identifiable in a log
    and in a filename -- and never a name guessed from anywhere.
    """
    add_cluster(con, "cluster:0000999999:insider:2026-09-08")
    assert promote.promote(con, day=DAY).rows[0].company == "0000999999"


def test_the_move_line_reads_a_percentage_that_is_already_a_percentage(con):
    """`Move.pct_move` is a percentage, not a fraction.

    The first version of this line multiplied by 100 and printed "+854.0%" for
    an 8.5% day. Nothing raised, every figure was plausible in shape, and it was
    caught by reading the command's own output -- which is the whole reason a
    command does not ship here until it has been run and its output read.
    """
    result = promote.promote(con, screen_result=screen_with(
        FakeMove("MMM", "stock", "$10+", Decimal("8.54"), Decimal("13"))))
    why = result.rows[0].why[promote.VOLATILITY]
    assert "+8.5%" in why
    assert "854" not in why
    assert "$10+ band" in why
    assert "+13 ticks" in why


def test_the_largest_move_wins_on_an_explicit_key_not_on_list_order(con):
    """A ticker is in up to four lists, and which one is read must be a rule.

    `max` on the absolute move with the band as a tiebreak, so two runs of the
    same command describe the same filer the same way -- the distinction
    `build_sponsors` cost this codebase three different review counts to learn.
    """
    result = promote.promote(con, screen_result=screen_with(
        FakeMove("MMM", "stock", "$10+", Decimal("8.5")),
        FakeMove("MMM", "stock", "$1-10", Decimal("-19.2")),
    ))
    assert "-19.2%" in result.rows[0].why[promote.VOLATILITY]


def alliance(con) -> None:
    """Alliance Entertainment: one CIK, a common share and a warrant.

    The real pair, with the real numbers from 2026-09-11 -- and the direction
    matters, so it is recorded rather than assumed. **AENTW, the warrant, is the
    sub-$1 name that moved +24.6%; AENT, the common share, is the $1-10 name at
    +16.5%.** A fixture with those the other way round would still exercise the
    ranking and would teach the next reader a false fact about the data.
    """
    con.execute("insert into pg.companies values (9, '0001823584', 'ALLIANCE ENT')")
    con.executemany("insert into pg.company_tickers values (?,?,?)", [
        (9, "AENT", date(2026, 9, 11)),
        (9, "AENTW", date(2026, 9, 11)),
    ])


def test_a_warrant_and_its_common_share_are_one_filer_and_two_facts(con):
    """**This is the defect that three-runs-identical actually caught.**

    AENT and AENTW are the same CIK and not the same security, and the promoted
    row reported whichever the database returned first -- so three runs of the
    same command against the same session gave three different answers for five
    filers. `company_tickers` had no `order by` to give it, DuckDB's Postgres
    scanner is parallel, and nothing raised: every run produced a plausible row
    about a real company.

    The fix is not only an ordering. The symbol that earned the promotion is the
    one with the largest move, which is a fact about the day; the filer's
    *primary listing* is not knowable from what is stored, so no rule pretends to
    pick it. Both symbols stay on the row and the reason line says a warrant does
    not move with its common share -- which here it did not, by 8 points, a band
    and 526 ticks.

    Mutated to confirm it fires: inverting the ranking key to
    `-_largest_move(...)` makes it report AENT, and dropping the `len(symbols) >
    1` clause drops the caveat while the number stays right.
    """
    alliance(con)
    result = promote.promote(con, screen_result=screen_with(
        FakeMove("AENTW", "stock", "sub$1", Decimal("24.640575"), Decimal("617")),
        FakeMove("AENT", "stock", "$1-10", Decimal("16.515426"), Decimal("91")),
    ))
    assert len(result.rows) == 1, "two symbols of one filer became two rows"
    row = result.rows[0]
    assert row.tickers == ("AENT", "AENTW")
    assert row.ticker == "AENTW"
    why = row.why[promote.VOLATILITY]
    assert why.startswith("AENTW +24.6%")
    assert "sub$1 band, +617 ticks" in why
    assert "2 of this filer's symbols were in the screen (AENT, AENTW)" in why
    assert "a warrant does not move with its common share" in why


def test_the_promoted_symbol_can_be_the_common_share_or_the_warrant(con):
    """The ranking is the move, so it is not a fixed preference either way.

    **There is no rule here that finds the common share, and that is deliberate.**
    Shortest-then-alphabetical looks like one and is not: measured 2026-09-13,
    527 of 1,452 multi-ticker filers have a shortest symbol that is not a prefix
    of the others -- preferred series (AILIH, AILIM, AILIN), ADR classes (AKZOF
    and AKZOY), share classes (BF-A, BF-B) -- and it resolves JPMorgan to AMJB, a
    structured note. `company_tickers` carries no exchange and no primary flag.

    So the promoted symbol is whichever moved most, which on this pair is the
    warrant and on the same pair a day later might not be. The caveat travels
    with it instead.
    """
    alliance(con)
    flipped = promote.promote(con, screen_result=screen_with(
        FakeMove("AENT", "stock", "$1-10", Decimal("-41.0")),
        FakeMove("AENTW", "stock", "sub$1", Decimal("3.1")),
    ))
    assert flipped.rows[0].ticker == "AENT"
    assert flipped.rows[0].tickers == ("AENT", "AENTW")


def test_three_runs_on_identical_input_promote_an_identical_set(con) -> None:
    """Idempotent is not enough; deterministic is the claim.

    `build_sponsors` wrote a correct upsert every time and produced 22,680,
    22,685 and 22,686 review rows from byte-identical input, because a function
    picked a row from a group without saying which. Every ordering here is on an
    explicit key, and this is the check that says so at runtime rather than by
    reading the SQL.
    """
    add_deal(con, "66740", "operating")
    add_deal(con, "320193", "division_sale")
    add_cluster(con, "cluster:0001853070:insider:2026-09-08")
    screen = screen_with(
        FakeMove("MMM", "stock", "$10+", Decimal("8.5")),
        FakeMove("AIR", "stock", "$10+", Decimal("-6.1")),
        FakeMove("SPY", "etf", "$10+", Decimal("1.0")),
    )
    shots = []
    for _ in range(3):
        r = promote.promote(con, screen_result=screen)
        shots.append((
            [(p.cik, p.company, p.ticker, p.tickers, p.reasons,
              tuple(sorted(p.why.items()))) for p in r.rows],
            r.legs, r.unresolved,
            [(s.name, s.remaining, s.why) for s in r.funnel.stages],
        ))
    assert shots[0] == shots[1] == shots[2]


def test_promoting_with_no_day_and_no_screen_refuses(con) -> None:
    """Defaulting to the calendar would promote against data that has not landed.

    The same reason `volatility.screen` defaults `as_of` to the newest date
    present rather than to today: the deck job runs right after the sweep, in
    the window where the trading date, the local date and the UTC date disagree.
    """
    with pytest.raises(promote.PromoteError, match="no day to promote for"):
        promote.promote(con)


# --- the gate ----------------------------------------------------------


def test_the_gate_keeps_only_what_has_a_valuation(con) -> None:
    """185 promoted, 27 valued, and the other 158 are reported not rendered.

    A deck of a filer with no valuation is ten pages of "no valuation", "no peer
    set", "-- --" and an empty candle chart. A deck is the easiest artifact here
    to mistake for an authoritative one, and a directory of 158 hollow ones is
    worse than no directory.
    """
    add_deal(con, "66740", "operating")
    add_deal(con, "320193", "operating")
    add_deal(con, "1853070", "operating")
    result = promote.promote(con, day=DAY)
    gated = promote.gate(result, valued={"0000066740"})
    assert [p.cik for p in gated.kept] == ["0000066740"]
    assert len(gated.skipped) == 2
    assert all("no DCF valuation" in why for _p, why in gated.skipped)


def test_the_gate_keys_both_sides_because_the_dcf_rows_are_unpadded(con):
    """**The first measurement of this gate reported 0 of 185, and was wrong.**

    `_dcf_rows` returns `r.inputs.cik` unpadded -- `1000228` -- and everything in
    Postgres is padded to ten. A plain set intersection therefore matched nothing
    for all 185 promoted names, which is a perfectly believable answer about a
    mover list full of penny stocks: 14% would have been plausible and 0% only
    slightly less so.

    Mutated to confirm it fires: replacing the `cik_key` calls in `gate` with the
    raw strings makes this test's `kept` list empty.
    """
    add_deal(con, "66740", "operating")
    result = promote.promote(con, day=DAY)
    for spelling in ("66740", "0000066740", 66740):
        gated = promote.gate(result, valued={spelling})
        assert [p.cik for p in gated.kept] == ["0000066740"], (
            f"the gate did not match a valuation spelled {spelling!r}")


def test_the_gate_continues_the_promotion_funnel(con) -> None:
    """One table from every sentinel hit to every file written.

    A second funnel starting at the gate would make the reader do the join, and
    the join is the interesting part: 85% of the promoted set leaves at this one
    stage and that is the number worth seeing next to the other three.
    """
    add_deal(con, "66740", "operating")
    add_deal(con, "320193", "operating")
    result = promote.promote(con, day=DAY)
    gated = promote.gate(result, valued={"0000066740"})
    before = [s.name for s in result.funnel.stages]
    after = [s.name for s in gated.funnel.stages]
    assert after[:len(before)] == before
    assert after[-1] == promote.REQUIRED_STAGE
    assert gated.funnel.stages[-1].remaining == 1


def test_an_optional_input_is_counted_and_never_gated_on(con) -> None:
    """A peer set is a page that says "no peer set", not a reason to skip.

    Only the valuation is required, because only the valuation leaves nine of ten
    pages with no number on them. The rest is reported so a run can say "27
    decks, 24 with a peer set" rather than implying all ten pages are populated
    on all of them.
    """
    add_deal(con, "66740", "operating")
    add_deal(con, "320193", "operating")
    result = promote.promote(con, day=DAY)
    gated = promote.gate(result, valued={"66740", "320193"},
                         with_comps={"0000066740"})
    assert len(gated.kept) == 2
    assert gated.optional == {"peer set": 1}


def test_a_screen_with_no_survivors_gates_to_nothing_without_raising(con):
    """A session where nothing promoted has a valuation is a real answer.

    The funnel says which stage it died at, which is the whole difference
    between a quiet night and a generator that broke last Tuesday.
    """
    add_deal(con, "66740", "operating")
    result = promote.promote(con, day=DAY)
    gated = promote.gate(result, valued=set())
    assert gated.kept == []
    assert len(gated.skipped) == 1
    assert gated.funnel.emptied is not None
    assert gated.funnel.emptied.name == promote.REQUIRED_STAGE


def test_render_states_the_window_and_the_share_it_misses(con) -> None:
    """A number with no reason beside it is the number nobody checks.

    The unattended run's log is the only place this job is visible, so it carries
    the funnel, the per-leg counts and the decile the Form 4 window drops --
    rather than just how many files it wrote.
    """
    add_deal(con, "66740", "operating")
    result = promote.promote(con, day=DAY)
    gated = promote.gate(result, valued={"66740"})
    text = "\n".join(promote.render(result, gated))
    assert "promoted set for 2026-09-11" in text
    assert "occurred_at within 7d" in text
    assert "~10% of clusters land outside it" in text
    assert "1 of 1 promoted filers can produce a deck" in text
    assert "population by stage" in text
