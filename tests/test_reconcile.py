"""Offline coverage for the SEC/Tiingo reconciliation.

No network. Every case is a hand-built universe small enough to reason about,
because the whole point of this module is that the big numbers are misleading
and the decomposition has to be right.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from marketradar.entities import reconcile


@dataclass(frozen=True)
class F:
    cik: str
    ticker: str
    name: str = "Widget Co"


@dataclass(frozen=True)
class M:
    ticker: str
    exchange: str = "NASDAQ"
    asset_type: str = "stock"
    end_date: str = "2026-09-04"


def row(ticker: str, exchange: str = "NASDAQ", asset_type: str = "Stock") -> dict:
    return {"ticker": ticker, "exchange": exchange, "assetType": asset_type}


# --- the join rule ------------------------------------------------------


def test_cik_with_one_matching_ticker_counts_as_covered_once() -> None:
    """A filer is one entity however many share classes it lists.

    This is the regression: counting the SEC side in tickers made a CIK with
    three unmatched classes look like three items of manual work.
    """
    filers = [
        F("0000000001", "BRKA"),
        F("0000000001", "BRKB"),
        F("0000000001", "BRKC"),
    ]
    active = [M("BRKA")]
    rep = reconcile.build(filers, active, [row("BRKA")])

    assert rep.sec.filers == 1
    assert rep.sec.covered == 1
    assert rep.sec.uncovered == 0


def test_cik_is_uncovered_only_when_no_ticker_matches() -> None:
    filers = [F("0000000001", "AAAA"), F("0000000001", "BBBB")]
    rep = reconcile.build(filers, [M("ZZZZ")], [row("ZZZZ")])

    assert rep.sec.filers == 1
    assert rep.sec.uncovered == 1


# --- direction 2 decomposition -----------------------------------------


def test_filter_dropped_is_separated_from_genuinely_absent() -> None:
    """The exact split: Tiingo's own exchange field, not a heuristic."""
    filers = [F("0000000001", "PINKY"), F("0000000002", "GHOST")]
    active: list[M] = []
    raw = [row("PINKY", exchange="PINK")]  # GHOST is in no row at all

    rep = reconcile.build(filers, active, raw)

    assert rep.sec.uncovered == 2
    assert rep.sec.filter_dropped == 1
    assert rep.sec.absent == 1
    assert rep.sec.dropped_by_exchange == {"PINK": 1}
    assert rep.real_gap_ciks == 1


def test_foreign_ordinaries_are_counted_within_absent() -> None:
    filers = [F("0000000001", "AKZOF"), F("0000000001", "AKZOY")]
    rep = reconcile.build(filers, [], [])

    assert rep.sec.absent == 1
    assert rep.sec.foreign == 1


# --- direction 1 decomposition -----------------------------------------


def test_etfs_are_split_out_of_the_unmatched_count() -> None:
    active = [M("SPXL", asset_type="etf"), M("REALCO", asset_type="stock")]
    rep = reconcile.build([], active, [])

    assert rep.tiingo.unmatched == 2
    assert rep.tiingo.etfs == 1
    assert rep.tiingo.unidentified == 1


def test_units_and_warrants_are_not_counted_as_unidentified() -> None:
    active = [M("AACBU"), M("AACBR"), M("ABLLW"), M("AAC-U"), M("PLAIN")]
    rep = reconcile.build([], active, [])

    assert rep.tiingo.structured == 4
    assert rep.tiingo.unidentified == 1


def test_structured_ticker_is_attributed_to_a_known_parent() -> None:
    """AACBU has no CIK, but AACB does — that is a known company's unit,
    and therefore zero manual work rather than an unidentified company."""
    filers = [F("0000000001", "AACB")]
    rep = reconcile.build(filers, [M("AACBU"), M("ZZZZU")], [])

    assert rep.tiingo.structured == 2
    assert rep.tiingo.structured_parent_known == 1
    assert rep.tiingo.structured_parent_unknown == 1
    assert rep.tiingo.unidentified == 0


# --- spelling ----------------------------------------------------------


def test_separator_variants_are_reported_not_joined() -> None:
    filers = [F("0000000001", "BRK-B")]
    active = [M("BRK.B")]
    rep = reconcile.build(filers, active, [])

    # Still unmatched: a variant is a diagnosis, never a join.
    assert rep.tiingo.unmatched == 1
    assert rep.spelling.ticker_hits == 1
    assert rep.spelling.sample == [("BRK.B", "BRK-B")]


@pytest.mark.parametrize(
    "ticker, expected",
    [
        ("BRK-B", {"BRK-B", "BRK.B", "BRKB"}),
        ("BRK.B", {"BRK.B", "BRK-B", "BRKB"}),
        ("AAPL", {"AAPL"}),
    ],
)
def test_ticker_variants(ticker: str, expected: set[str]) -> None:
    assert reconcile.ticker_variants(ticker) == expected


def test_base_symbol_strips_instrument_tails() -> None:
    assert reconcile.base_symbol("AACBU") == "AACB"
    assert reconcile.base_symbol("ABLLW") == "ABLL"
    assert reconcile.base_symbol("AAC-U") == "AAC"
    assert reconcile.base_symbol("AAPL") == "AAPL"


# --- rendering ---------------------------------------------------------


def test_render_survives_an_empty_universe() -> None:
    """Divide-by-zero is the obvious way this breaks on a bad fetch."""
    lines = list(reconcile.render(reconcile.build([], [], [])))
    assert any("n/a" in line for line in lines)


def test_render_reports_both_directions_and_the_real_gap() -> None:
    filers = [F("0000000001", "AAAA"), F("0000000002", "PINKY")]
    active = [M("AAAA"), M("SPXL", asset_type="etf"), M("MYSTERY")]
    raw = [row("AAAA"), row("SPXL", asset_type="ETF"), row("PINKY", exchange="PINK")]

    text = "\n".join(reconcile.render(reconcile.build(filers, active, raw)))

    assert "direction 1" in text and "direction 2" in text
    assert "counted in tickers" in text
    assert "counted in CIKs" in text
    assert "real identity gap" in text
    assert "(OTC)" in text
