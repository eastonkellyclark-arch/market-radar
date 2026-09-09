"""A ticker is not an entity across time.

356 of the 14,107 active symbols carry two different companies inside a
ten-year pull. Before this, the price store's key was (ticker, date) with no
entity column at all, so a backfill concatenated them: AAAP compared a $25.10
close on 2026-06-23 against an $81.63 close from 2018-02-20 and reported a
clean, plausible -69.26%.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import duckdb
import pytest

from marketradar.screens import volatility as vol
from marketradar.sources import tiingo


def row(ticker, start, end=None, exchange="NASDAQ"):
    return {"ticker": ticker, "exchange": exchange, "assetType": "Stock",
            "startDate": start, "endDate": end}


# --- span construction --------------------------------------------------


def test_a_relisting_becomes_two_spans() -> None:
    spans = tiingo.listing_spans([
        row("ACAT", "2001-01-02", "2017-03-07"),
        row("ACAT", "2026-05-28", "2026-09-04"),
    ])
    assert spans["ACAT"] == [
        (date(2001, 1, 2), date(2017, 3, 7)),
        (date(2026, 5, 28), date(2026, 9, 4)),
    ]


def test_a_venue_change_over_a_weekend_stays_one_span() -> None:
    """AIEQ ends on a Friday and resumes the Monday. Same company.

    At zero tolerance the real file shows 516 multi-listing active symbols;
    at seven days, 418; at thirty, 416. It goes flat after a week because a
    genuine recycle leaves months or years, never a weekend.
    """
    spans = tiingo.listing_spans([
        row("AIEQ", "2017-10-18", "2024-01-26", exchange="NYSE ARCA"),
        row("AIEQ", "2024-01-29", "2026-09-04", exchange="NASDAQ"),
    ])
    assert spans["AIEQ"] == [(date(2017, 10, 18), date(2026, 9, 4))]


def test_adjacent_day_handoff_stays_one_span() -> None:
    spans = tiingo.listing_spans([
        row("ACFN", "1992-02-11", "2025-07-23"),
        row("ACFN", "2025-07-24", "2026-09-04"),
    ])
    assert len(spans["ACFN"]) == 1


def test_overlapping_ranges_are_one_listing_on_two_venues() -> None:
    spans = tiingo.listing_spans([
        row("DUAL", "2020-01-02", "2024-06-30", exchange="NYSE"),
        row("DUAL", "2022-01-03", "2026-09-04", exchange="BATS"),
    ])
    assert spans["DUAL"] == [(date(2020, 1, 2), date(2026, 9, 4))]


def test_a_row_with_no_start_date_is_not_a_span() -> None:
    spans = tiingo.listing_spans([row("NOSTART", None, "2026-09-04")])
    assert "NOSTART" not in spans


# --- assignment ---------------------------------------------------------


def test_a_bar_is_assigned_to_the_listing_that_contains_it() -> None:
    spans = [(date(2001, 1, 2), date(2017, 3, 7)),
             (date(2026, 5, 28), date(2026, 9, 4))]
    assert tiingo.listing_for(spans, date(2010, 5, 1)) == date(2001, 1, 2)
    assert tiingo.listing_for(spans, date(2026, 6, 1)) == date(2026, 5, 28)


def test_a_bar_outside_every_span_is_unattributed_not_defaulted() -> None:
    """The silent default this whole column exists to remove."""
    spans = [(date(2020, 1, 2), date(2021, 12, 31))]
    assert tiingo.listing_for(spans, date(2019, 5, 1)) is None
    assert tiingo.listing_for(spans, date(2025, 5, 1)) is None


def test_a_ticker_with_no_spans_is_unattributed() -> None:
    assert tiingo.listing_for(None, date(2026, 1, 2)) is None
    assert tiingo.listing_for([], date(2026, 1, 2)) is None


def test_the_identifier_is_the_listing_start_not_an_ordinal() -> None:
    """Ordinals renumber when the vendor adds an earlier listing.

    Partitions are immutable, so an ordinal would silently change the meaning
    of years of stored history the first time Tiingo learns about a listing
    that predates the ones we already had.
    """
    spans = [(date(2001, 1, 2), date(2017, 3, 7)),
             (date(2026, 5, 28), date(2026, 9, 4))]
    assert tiingo.listing_for(spans, date(2026, 6, 1)) == date(2026, 5, 28)

    with_earlier = [(date(1995, 1, 3), date(1999, 12, 31))] + spans
    assert tiingo.listing_for(with_earlier, date(2026, 6, 1)) == date(2026, 5, 28)


def test_shape_rows_attaches_the_listing() -> None:
    bar = {"date": "2026-06-23T00:00:00.000Z", "open": 25, "high": 25,
           "low": 25, "close": 25.095, "volume": 100,
           "divCash": 0, "splitFactor": 1}
    spans = [(date(2015, 1, 2), date(2018, 2, 20)),
             (date(2026, 6, 23), date(2026, 9, 4))]
    prices, _ = tiingo.shape_rows("AAAP", None, [bar], spans=spans)
    assert prices[0]["listing_id"] == date(2026, 6, 23)

    prices, _ = tiingo.shape_rows("AAAP", None, [bar], spans=None)
    assert prices[0]["listing_id"] is None


# --- the dedupe key -----------------------------------------------------


def test_listing_id_is_not_part_of_the_dedupe_key() -> None:
    """Deliberate, and the reasoning is easy to get backwards.

    Spans are disjoint, so (ticker, date) already resolves to at most one
    listing -- adding listing_id to the key gains nothing. It actively costs
    something: if the vendor revises a listing range, the same bar gets a new
    listing_id and a key that included it would keep BOTH rows, duplicating a
    trading day. Out of the key, a re-publish overwrites the row and the
    assignment self-heals.
    """
    import inspect

    source = inspect.getsource(tiingo.publish)
    assert "PARTITION BY ticker, date, source" in source
    assert "listing_id" not in source.split("row_number")[1].split(")")[0]


# --- the screen ---------------------------------------------------------


def px_table(rows):
    con = duckdb.connect()
    con.execute(
        "create table px (ticker varchar, date date, close decimal(18,6), "
        "volume bigint, security_type varchar, exchange varchar, "
        "listing_id date)"
    )
    for ticker, day, close, listing in rows:
        con.execute("insert into px values (?, ?, ?, 900000, 'stock', 'NYSE', ?)",
                    [ticker, day, Decimal(str(close)), listing])
    con.execute("create table act (ticker varchar, ex_date date, "
                "split_factor decimal(18,8), div_cash decimal(18,8))")
    return con


L1, L2 = date(2015, 1, 2), date(2026, 6, 23)


def test_lag_does_not_reach_across_a_relisting() -> None:
    """The AAAP case, reproduced exactly.

    Under the old partitioning this produced -69.26% by comparing $25.095
    against an $81.63 close from eight years and one company earlier.
    """
    con = px_table([
        ("AAAP", date(2018, 2, 20), 81.63, L1),
        ("AAAP", date(2026, 6, 23), 25.095, L2),
        ("AAAP", date(2026, 6, 24), 26.000, L2),
    ])
    rel = vol.moves(con, prices=con.table("px"), actions=con.table("act"))
    got = {r[1]: dict(zip(rel.columns, r)) for r in rel.fetchall()}

    # The first bar of the new listing has no prior bar *in that listing*, so
    # there is no move for it at all -- which is the correct answer.
    assert date(2026, 6, 23) not in got
    # (26.000 - 25.095) / 25.095 -- both bars from the new listing.
    assert got[date(2026, 6, 24)]["pct_move"] == pytest.approx(
        Decimal("3.606296"), abs=1e-4)


def test_dollar_volume_is_averaged_within_a_listing() -> None:
    con = px_table([
        ("REC", date(2018, 1, 3), 100, L1),
        ("REC", date(2018, 1, 4), 100, L1),
        ("REC", date(2026, 6, 23), 1, L2),
        ("REC", date(2026, 6, 24), 1, L2),
    ])
    rel = vol.moves(con, prices=con.table("px"), actions=con.table("act"))
    rows = {r[1]: dict(zip(rel.columns, r)) for r in rel.fetchall()}
    # The new listing's ADV is its own, not blended with the old $100 one.
    assert float(rows[date(2026, 6, 24)]["avg_dollar_volume"]) == pytest.approx(900_000)


def test_the_gap_guard_catches_a_long_halt_inside_one_listing() -> None:
    """Kept even with listing_id: the vendor's own ranges can be wrong."""
    con = px_table([
        ("HALT", date(2026, 1, 5), 10, L1),
        ("HALT", date(2026, 8, 3), 4, L1),      # 210 days, same listing
    ])
    rel = vol.moves(con, prices=con.table("px"), actions=con.table("act"))
    row = dict(zip(rel.columns, rel.fetchall()[0]))
    assert row["gap_days"] > vol.MAX_GAP_DAYS
    assert row["within_gap"] is False


def test_a_normal_holiday_weekend_is_not_a_gap() -> None:
    con = px_table([
        ("OK", date(2026, 9, 4), 10, L1),
        ("OK", date(2026, 9, 8), 11, L1),      # Fri -> Tue over Labor Day
    ])
    rel = vol.moves(con, prices=con.table("px"), actions=con.table("act"))
    row = dict(zip(rel.columns, rel.fetchall()[0]))
    assert row["within_gap"] is True


def test_gapped_moves_are_excluded_and_counted(monkeypatch) -> None:
    con = px_table([
        ("HALT", date(2026, 1, 5), 10, L1),
        ("HALT", date(2026, 8, 3), 4, L1),
        ("FINE", date(2026, 7, 31), 10, L1),
        ("FINE", date(2026, 8, 3), 11, L1),
    ])
    result = vol.screen(con, prices=con.table("px"), actions=con.table("act"))
    assert result.gap_excluded == 1
    assert result.moves_screened == 1
    assert "gap of more than" in "\n".join(vol.render(result))


def test_unattributed_bars_are_reported() -> None:
    con = px_table([
        ("UNK", date(2026, 8, 3), 10, None),
        ("UNK", date(2026, 8, 4), 11, None),
    ])
    result = vol.screen(con, prices=con.table("px"), actions=con.table("act"))
    assert result.unattributed == 1
    assert "no listing_id" in "\n".join(vol.render(result))


def test_prices_without_the_column_still_screen(caplog) -> None:
    """Old partitions predate listing_id. Degrade, but say so."""
    con = duckdb.connect()
    con.execute(
        "create table px (ticker varchar, date date, close decimal(18,6), "
        "volume bigint, security_type varchar, exchange varchar)"
    )
    for day, close in ((date(2026, 9, 3), 10), (date(2026, 9, 4), 11)):
        con.execute("insert into px values ('OLD', ?, ?, 900000, 'stock', 'NYSE')",
                    [day, Decimal(str(close))])
    con.execute("create table act (ticker varchar, ex_date date, "
                "split_factor decimal(18,8), div_cash decimal(18,8))")

    import logging
    with caplog.at_level(logging.WARNING):
        rel = vol.moves(con, prices=con.table("px"), actions=con.table("act"))
    assert rel.fetchall()
    assert "no listing_id" in caplog.text
