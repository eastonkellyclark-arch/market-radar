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
