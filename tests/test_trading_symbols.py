"""Recovering the ticker a company traded under while it existed.

Every fixture is the real shape of the thing being parsed, because both routes key
on market and regulatory convention and a fixture in my own words would test my
words. No network: the HTML is inline and there is no client.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from marketradar.sources import sec_trading_symbols as sym

# --- the two routes ------------------------------------------------------


def test_the_xbrl_tag_is_preferred_and_says_so() -> None:
    """Unambiguous, and post-2019 only. Tried first so a modern filing never
    depends on a regex over prose."""
    assert sym.symbol_from('<span name="dei:TradingSymbol">TWTR</span>') == (
        "TWTR", "tag")


def test_the_prose_route_covers_the_decade_before_the_tag() -> None:
    """**The tag has a hard cliff and the recovery rate shows it exactly**: 1 row
    in 2017 against 40 in 2019 and 62 in 2020, because the cover-page XBRL mandate
    is what created the tag. A decade of takeouts sits on the wrong side, and that
    is the population deal multiples and the filer universe care about.

    What is on the wrong side is the 10-K's own listing sentence, which is legal
    boilerplate. Deterministic, which matters: the proxy reader's extraction half
    was measured at 62% and declined, so a second field needing a model would be
    declined on the same evidence.
    """
    for html, want in (
        ("<p>listed on the New York Stock Exchange under the symbol "
         "“LNKD”.</p>", "LNKD"),
        ("<p>our common stock trades under the ticker symbol WFM on Nasdaq</p>",
         "WFM"),
        ("<p>under the trading symbol ‘MON’</p>", "MON"),
    ):
        assert sym.symbol_from(html) == (want, "prose"), html


def test_the_share_class_dot_survives_and_the_full_stop_does_not() -> None:
    """**The lookahead that took this from 1 of 6 to 7 of 8.**

    The boilerplate ends the sentence right after the quoted symbol -- "under the
    symbol 'WFM.'" -- so a class character that admits a bare dot swallows the full
    stop and every answer comes back one character long. A dot counts only when a
    share-class letter follows it.
    """
    assert sym.symbol_from(
        "<p>under the symbol “WFM.”</p>") == ("WFM", "prose")
    assert sym.symbol_from(
        "<p>under the ticker symbol BRK.A on the NYSE</p>") == ("BRK.A", "prose")


def test_a_sentinel_is_rejected_whole_rather_than_truncated() -> None:
    """**The worst shape available here, and it was real.**

    The tag capture used to stop at the slash, so ``N/A`` came back as ``N`` --
    which passes every validity check, because N is a genuine NYSE ticker. A company
    with no listing would have been mapped to somebody else's stock. The capture now
    takes the slash so the sentinel arrives intact, and ``SYMBOL`` refuses it.
    """
    html = ('<span name="dei:TradingSymbol">N/A</span>'
            "<p>under the symbol “REAL”</p>")
    assert sym.symbol_from(html) == ("REAL", "prose")
    # A real one-letter ticker still works, which is why the truncation could not
    # be caught by length alone.
    assert sym.symbol_from(
        '<span name="dei:TradingSymbol">N</span>') == ("N", "tag")
    assert "N/A" in sym.NOT_A_SYMBOL


def test_a_sentinel_shaped_like_a_symbol_is_rejected_by_name() -> None:
    """**Where the sentinel list is the only thing doing the work.**

    Mutation caught this one: deleting ``NOT_A_SYMBOL`` left the ``N/A`` case above
    still passing, because ``SYMBOL`` rejects it on the slash anyway. The list only
    matters for the sentinels that are *valid symbol shapes* -- NONE, NA, TBD -- and
    those are the ones that would otherwise be written to the map as tickers.
    """
    for sentinel in sorted(sym.NOT_A_SYMBOL):
        if not sym.SYMBOL.match(sentinel):
            continue  # rejected by shape; the list is belt and braces there
        html = f'<span name="dei:TradingSymbol">{sentinel}</span>'
        assert sym.symbol_from(html) is None, sentinel


def test_no_symbol_is_none_rather_than_a_guess() -> None:
    assert sym.symbol_from("<p>nothing relevant here</p>") is None
    assert sym.symbol_from("") is None


def test_the_rejected_routes_are_recorded_in_the_module() -> None:
    """Form 25-NSE and the 8-K Item 3.01 were tried and carry no symbol at all,
    measured on LinkedIn and Whole Foods in raw HTML and in visible text. Written
    down so the next person does not re-test them."""
    # Whitespace-collapsed: the note wraps across comment lines, and a test that
    # breaks on a line break is testing the formatter. Third time today.
    import re as _re

    source = Path(sym.__file__).read_text(encoding="utf-8")
    # Comment markers off first, *then* collapse whitespace -- collapsing both at
    # once leaves the "#:" colon behind in the middle of the sentence.
    text = _re.sub(r"\s+", " ", _re.sub(r"(?m)^\s*#:?", " ", source))
    assert "Form 25-NSE" in text
    assert "do not carry the symbol at all" in text
    assert "7 of 8" in text, "the prose route's measured accuracy is not recorded"


# --- ranges, and what they do not claim ---------------------------------


def obs(cik: str, ticker: str, day: str, route: str = "tag") -> sym.Observation:
    return sym.Observation(cik=cik, ticker=ticker,
                           filed=date.fromisoformat(day), form="10-K",
                           accession=f"{cik}-{day}", route=route)


def test_a_cik_that_changed_symbol_gets_two_rows() -> None:
    """Collapsing to "the" ticker for a CIK would pick one arbitrarily, and which
    one it picked would decide whether a price join found anything."""
    ranges = sym.collapse([
        obs("0000000001", "OLDY", "2016-03-01"),
        obs("0000000001", "OLDY", "2017-03-01"),
        obs("0000000001", "NEWY", "2019-03-01"),
    ])
    assert len(ranges) == 2
    by = {r.ticker: r for r in ranges}
    assert by["OLDY"].first_seen == date(2016, 3, 1)
    assert by["OLDY"].last_seen == date(2017, 3, 1)
    assert by["NEWY"].is_point


def test_a_recycled_symbol_keeps_both_companies() -> None:
    """**The failure this module exists to avoid.** 356 active symbols carry two
    different companies inside a ten-year pull, and SGEN is the case in point: a
    different issuer took it after Seagen was acquired in 2023. A recovered ticker
    that overwrote a recycled one would leave a map that looks complete and
    silently resolves the wrong company."""
    ranges = sym.collapse([
        obs("0001060736", "SGEN", "2019-07-24"),
        obs("0001060736", "SGEN", "2023-12-14"),
        obs("0009999999", "SGEN", "2025-04-01"),
    ])
    assert len(ranges) == 2
    assert {r.cik for r in ranges} == {"0001060736", "0009999999"}


def test_the_range_is_a_bound_and_never_reads_as_a_lifetime() -> None:
    """The bounds are filing dates: the symbol was in use before the first filing
    that mentions it and usually after the last, so the error runs one way. Same
    rule as Form 5500's ``PLAN_EFF_DATE`` -- usable as a join bound, unusable as a
    reported fact."""
    single = sym.collapse([obs("0000000001", "ONE", "2020-01-01")])[0]
    assert single.is_point
    assert "seen on" in single.describe()
    assert "traded" not in single.describe()

    span = sym.collapse([obs("0000000002", "TWO", "2018-01-01"),
                         obs("0000000002", "TWO", "2021-01-01")])[0]
    assert not span.is_point
    assert "seen between" in span.describe()
    assert "traded" not in span.describe()


def test_the_route_rides_on_the_range() -> None:
    """A tagged symbol and a parsed one are different evidence -- the tag is
    unambiguous, the prose is a regex at 7 of 8 -- so the row says which."""
    both = sym.collapse([obs("0000000003", "AAA", "2016-01-01", route="prose"),
                         obs("0000000003", "AAA", "2021-01-01", route="tag")])[0]
    assert both.routes == ("prose", "tag")
    only = sym.collapse([obs("0000000004", "BBB", "2016-01-01",
                             route="prose")])[0]
    assert only.routes == ("prose",)


# --- sampling ------------------------------------------------------------


def test_sampling_takes_the_ends_not_the_newest() -> None:
    """A range needs its ends, and a symbol that changed inside the window is only
    visible if something between them is read. Taking the newest N would give a
    tight range around the wrong part of the history."""
    filings = [{"filed": f"20{y:02d}-01-01"} for y in range(10, 22)]
    picked = sym._sample(filings, 3)
    assert len(picked) == 3
    days = sorted(f["filed"] for f in picked)
    assert days[0] == "2010-01-01"
    assert days[-1] == "2021-01-01"
    assert days[1] not in (days[0], days[-1])


def test_a_short_history_is_taken_whole() -> None:
    filings = [{"filed": "2019-01-01"}, {"filed": "2020-01-01"}]
    assert sym._sample(filings, 3) == filings


# --- the report ----------------------------------------------------------


def test_the_denominator_is_what_was_asked_for() -> None:
    """A denominator that shrank to the successes would make every coverage figure
    read 100%, which is the one number this sweep must not produce."""
    report = sym.SweepReport(
        ciks=100, requests=400, observations=1,
        ranges=sym.collapse([obs("0000000001", "AAA", "2020-01-01")]),
        no_symbol=["0000000002"] * 97, failed=[("0000000003", "timeout")])
    text = "\n".join(report.lines())
    assert "100 CIKs" in text
    assert "1.0%" in text


def test_the_report_surfaces_recycling() -> None:
    report = sym.SweepReport(
        ciks=2, requests=8, observations=2,
        ranges=sym.collapse([obs("0000000001", "DUP", "2018-01-01"),
                             obs("0000000002", "DUP", "2024-01-01")]))
    text = "\n".join(report.lines())
    assert "more than one CIK" in text
    assert "DUP" in text


# --- the route reaching the row -----------------------------------------


def test_the_route_is_written_to_the_row_not_a_module_constant() -> None:
    """**The defect this section exists for, found by measuring rather than by
    reading.**

    ``load`` interpolated a module-level ``SOURCE`` string, so all 138 symbols the
    prose route had recovered were stored as ``dei:TradingSymbol``. The route was
    computed, carried through ``collapse``, attached to the range -- and dropped at
    the write. Every test passed, because nothing could see the statement.

    Same rule as the provider and model on an LLM-produced row and the resolved tag
    on a fundamentals row: provenance that does not reach the row does not exist.
    """
    prose = sym.collapse([obs("0000000001", "LNKD", "2012-01-01", route="prose"),
                          obs("0000000001", "LNKD", "2016-05-01", route="prose")])[0]
    tag = sym.collapse([obs("0000000002", "TWTR", "2019-02-01", route="tag")])[0]

    assert sym.SOURCE["prose"] in sym.row_values(prose)
    assert sym.SOURCE["tag"] not in sym.row_values(prose)
    assert sym.SOURCE["tag"] in sym.row_values(tag)


def test_both_routes_on_one_range_are_stored_as_both() -> None:
    """A symbol confirmed by the tag *and* the prose is stronger evidence than
    either alone, and collapsing it to one route throws that away."""
    both = sym.collapse([obs("0000000003", "DOW", "2016-01-01", route="prose"),
                         obs("0000000003", "DOW", "2021-01-01", route="tag")])[0]
    assert sym.source_of(both) == sym.SOURCE["prose+tag"]


def test_an_unknown_route_raises_rather_than_inheriting_a_label() -> None:
    """A default is precisely what caused the defect: the row inherited a label it
    had not earned and read plausibly. A new route must stop the load."""
    import dataclasses

    rng = dataclasses.replace(
        sym.collapse([obs("0000000004", "AAA", "2020-01-01")])[0],
        routes=("semantic-guess",))
    try:
        sym.source_of(rng)
    except sym.SymbolError as exc:
        assert "semantic-guess" in str(exc)
    else:
        raise AssertionError("an unrecognised route was given a label anyway")


def test_every_stored_label_is_allowed_by_the_table() -> None:
    """The Python labels and the CHECK constraint are two statements of the same
    list, and a label Python can produce but Postgres rejects would fail the whole
    batch at the end of a 45-minute sweep."""
    import re as _re

    sql = Path("sql/015_ticker_history_route.sql").read_text(encoding="utf-8")
    allowed = set(_re.findall(r"'([^']+)'", sql.split("check (source in (")[1]))
    assert set(sym.SOURCE.values()) <= allowed, (
        f"labels Python can write but the table rejects: "
        f"{set(sym.SOURCE.values()) - allowed}")


def test_the_conflict_clause_widens_the_route_like_the_bounds() -> None:
    """A row first recovered by the tag and later confirmed in prose must end up
    saying both. Leaving ``source`` out of the update would leave it claiming the tag
    only -- the same silent narrowing as an upsert that reset the bounds instead of
    extending them.

    Reads the statement the loader builds, not the module's source text. The first
    version of this test read the source, and a deliberately broken mutation passed
    it: the clause was still *present* in the file while no longer reaching the SQL.
    """
    stmt = sym.upsert_statement("('x')")
    update = stmt.split("do update set")[1]
    for column, widening in (("first_seen", "least"), ("last_seen", "greatest"),
                             ("filings", "+ excluded.filings"),
                             ("source", "case when")):
        assert f"{column} " in update, f"{column} is not widened on conflict"
        assert widening in update, f"{column} does not widen, it replaces"
    assert sym.SOURCE["prose+tag"] in update


# --- the overlap check --------------------------------------------------


def _fate_fixture():
    """A map and a price file covering each fate exactly once.

    Real DuckDB, not a stub: the classification is a CASE expression and a stub would
    only test my reading of it. `company_ticker_history` is created locally under the
    alias name so no Postgres is needed.
    """
    import duckdb

    con = duckdb.connect()
    con.execute("create schema pg")
    con.execute("""create table pg.company_ticker_history (
        cik text, ticker text, first_seen date, last_seen date,
        filings integer, source text)""")
    con.execute("""insert into pg.company_ticker_history values
        -- bars overlap the window it was seen in: this company's history
        ('0000000001', 'LIVE', date '2018-01-01', date '2020-01-01', 3, 'tag'),
        -- SGEN: seen 2019-2023, and the bars under that symbol start 2024, because a
        -- different issuer took it. Present in the file and not ours.
        ('0001060736', 'SGEN', date '2019-01-01', date '2023-01-01', 2, 'tag'),
        -- never in the price file, and inside our window
        ('0000000003', 'GONE', date '2017-01-01', date '2019-01-01', 1, 'prose'),
        -- last seen before the price file begins
        ('0000000004', 'OLDY', date '2002-01-01', date '2004-01-01', 1, 'prose'),
        -- Seen 2020-2022; the bars under PRED are from 2016-2017, so they are the
        -- previous owner's. Same fate, opposite direction.
        ('0000000005', 'PRED', date '2020-01-01', date '2022-01-01', 2, 'prose'),
        -- In the price file under its CURRENT owner, while this company's own window
        -- closed before our history opens. Both facts are true and only one is
        -- actionable: buying history recovers *this* company, so the fate must be
        -- `before_our_history` and not the one a purchase cannot fix.
        ('0000000006', 'HELD', date '2002-01-01', date '2004-01-01', 1, 'prose')""")
    con.execute("""create table bars (ticker text, date date)""")
    con.execute("""insert into bars values
        ('LIVE', date '2016-06-01'), ('LIVE', date '2019-06-01'),
        ('SGEN', date '2024-02-01'), ('SGEN', date '2026-01-01'),
        -- The mirror case, and the one the first fixture missed: bars entirely
        -- BEFORE the observed window, i.e. the symbol's previous owner.
        ('PRED', date '2016-02-01'), ('PRED', date '2017-01-01'),
        ('HELD', date '2016-03-01'), ('HELD', date '2026-01-01')""")
    return con


def test_the_overlap_check_separates_four_fates() -> None:
    """**The headline finding of this map, as a function rather than a step.**

    Recovery was 45.9% of stopped filers and *usable* was 4.0%, and a consumer joining
    the map to prices on the symbol alone would get the first number while believing
    it had the second. Four outcomes rather than a boolean, for the same reason
    `proxy_consideration` carries a shape: four different absences must not become one
    NULL, and only one of them is fixed by buying price history.
    """
    con = _fate_fixture()
    got = dict(sym.usable_prices(con, prices="bars").select(
        "ticker, fate").fetchall())
    assert got == {
        "LIVE": sym.USABLE,
        "SGEN": sym.OTHER_OWNER_BARS,
        "GONE": sym.NOT_IN_FILE,
        "OLDY": sym.BEFORE_OUR_HISTORY,
        "PRED": sym.OTHER_OWNER_BARS,
        "HELD": sym.BEFORE_OUR_HISTORY,
    }


def test_a_recycled_symbol_is_not_counted_as_coverage() -> None:
    """**The rejection that matters, named.** SGEN carries bars through 2026 because a
    different issuer took the symbol after Seagen was acquired in 2023. Presence in
    the price file is not coverage -- measured on the stopped-filer population, 56 of
    206 symbols present in the file had bars belonging to the next owner, so a
    presence test is wrong about more than a quarter of what it calls coverage.

    And it fails in the dangerous direction: the bars are real, continuous and
    plausible, so a forward-return study would report a number for a company that had
    already ceased to exist.
    """
    con = _fate_fixture()
    fate = sym.usable_prices(con, prices="bars")
    sgen = fate.filter("ticker = 'SGEN'").fetchall()[0]
    assert sgen[-1] == sym.OTHER_OWNER_BARS
    # Present, which is exactly why a presence test would have passed it.
    assert sgen[fate.columns.index("bars")] == 2
    assert sym.OTHER_OWNER_BARS not in (sym.USABLE,)


def test_an_empty_price_relation_raises_rather_than_classifying_everything() -> None:
    """Every symbol would come back `not_in_file`, which is a *believable* answer
    about a map of delisted companies -- so it is the one that has to raise. Same
    shape as a freshness assertion on an empty load."""
    import duckdb

    con = _fate_fixture()
    con.execute("delete from bars")
    try:
        sym.usable_prices(con, prices="bars")
    except sym.SymbolError as exc:
        assert "empty" in str(exc)
    else:
        raise AssertionError("an empty price file classified the whole map")


def test_every_fate_is_declared() -> None:
    """A fate the classifier can emit but `PRICE_FATE` does not list is a value no
    consumer knows to handle."""
    con = _fate_fixture()
    emitted = {row[0] for row in
               sym.usable_prices(con, prices="bars").select("fate").fetchall()}
    assert emitted <= set(sym.PRICE_FATE)
    assert set(sym.PRICE_FATE) == emitted, (
        f"declared but never emitted by the fixture: "
        f"{set(sym.PRICE_FATE) - emitted}")
