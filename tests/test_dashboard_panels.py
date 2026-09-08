"""Panel bodies, and the expansion policy.

24 lists is more than anyone scans over coffee. What opens on load is one
function, deliberately, because it is a hypothesis that should be cheap to
revise after a week of actually reading it.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import duckdb
import pytest

from marketradar import digest as digest_mod
from marketradar.dashboard import panels, shell
from marketradar.screens import volatility

D3, D4 = date(2026, 9, 3), date(2026, 9, 4)


def build(rows) -> volatility.ScreenResult:
    con = duckdb.connect()
    con.execute(
        "create table px (ticker varchar, date date, close decimal(18,6), "
        "volume bigint, security_type varchar, exchange varchar, listing_id date)"
    )
    for ticker, prev, close, kind, vol in rows:
        for day, price in ((D3, prev), (D4, close)):
            con.execute(
                "insert into px values (?, ?, ?, ?, ?, 'NYSE', DATE '2015-01-02')",
                [ticker, day, Decimal(str(price)), vol, kind],
            )
    con.execute("create table act (ticker varchar, ex_date date, "
                "split_factor decimal(18,8), div_cash decimal(18,8))")
    return volatility.screen(con, prices=con.table("px"), actions=con.table("act"))


def digest_for(result) -> digest_mod.Digest:
    return digest_mod.Digest(
        day=D4, prior_day=D3,
        generated_at=datetime(2026, 9, 8, 21, 0, tzinfo=timezone.utc),
        health=digest_mod.Health(items=[digest_mod.HealthItem("prices", "ok")]),
        macro=[digest_mod.MacroLine("10y Treasury", Decimal("4.77"), "%",
                                    D3, {"30d": Decimal("0.14"), "1y": None})],
        screen=result, names={}, new_tickers={}, top_n=20, include_ungated=True,
    )


LIQUID, THIN = 900_000, 1


# --- the expansion policy ----------------------------------------------


def test_the_two_anchors_are_always_open() -> None:
    result = build([("BIG", 100, 110, "stock", LIQUID),
                    ("DWN", 100, 90, "stock", LIQUID)])
    keys = panels.default_expanded(result.lists)
    assert ("stock", "$10+", "gainers", "liquid") in keys
    assert ("stock", "$10+", "losers", "liquid") in keys


def test_the_loudest_liquid_band_also_opens() -> None:
    """So an unusual day in a quiet band is not hidden the morning it matters."""
    result = build([("BIG", 100, 101, "stock", LIQUID),
                    ("MID", 5, 9, "stock", LIQUID)])
    keys = panels.default_expanded(result.lists)
    assert ("stock", "$1-10", "gainers", "liquid") in keys


def test_expansion_never_opens_an_ungated_list_when_a_liquid_one_exists() -> None:
    """The default view should not mix tradeable with untradeable.

    Nothing is hidden by this: every collapsed header carries its own top row.
    """
    result = build([("BIG", 100, 101, "stock", LIQUID),
                    ("THIN", 5, 20, "stock", THIN)])
    for key in panels.default_expanded(result.lists):
        assert key[3] == "liquid"


def test_the_policy_is_one_function() -> None:
    """It is expected to be wrong at first; changing it must touch one place."""
    import inspect

    source = inspect.getsource(panels)
    assert source.count("def default_expanded") == 1
    assert source.count("default_expanded(") == 2  # the def and one call site


# --- rendering ----------------------------------------------------------


def test_every_list_is_present_even_when_collapsed() -> None:
    result = build([("BIG", 100, 110, "stock", LIQUID),
                    ("ETF", 20, 26, "etf", LIQUID)])
    page = panels.screens_html(digest_for(result))
    populated = [sl for sl in result.lists if sl.rows]
    assert page.count('<details class="list"') == len(populated)


def test_a_collapsed_header_carries_its_top_row() -> None:
    """Collapsing must not hide the headline, only the depth."""
    result = build([("BIG", 100, 110, "stock", LIQUID),
                    ("ETF", 20, 26, "etf", LIQUID)])
    page = panels.screens_html(digest_for(result))
    assert 'class="peek"' in page
    assert "ETF" in page


def test_collapse_works_without_the_script() -> None:
    """<details> is native, so the page degrades to readable, not broken."""
    page = panels.screens_html(digest_for(build([("BIG", 100, 110, "stock", LIQUID)])))
    assert "<details" in page and "<summary>" in page


def test_filters_cover_band_and_security_type() -> None:
    result = build([("BIG", 100, 110, "stock", LIQUID),
                    ("ETF", 20, 26, "etf", LIQUID)])
    page = panels.screens_html(digest_for(result))
    assert 'data-f="band"' in page
    assert 'data-f="sec"' in page
    assert 'data-f="liq"' in page


def test_the_screens_panel_says_the_gate_is_not_trustworthy_yet() -> None:
    """The liquidity caveat travels with the lists it applies to."""
    page = panels.screens_html(digest_for(build([("BIG", 100, 110, "stock", LIQUID)])))
    assert "Liquidity gating is not yet trustworthy" in page


def test_macro_renders_basis_points_not_percent() -> None:
    page = panels.macro_html(digest_for(build([("BIG", 100, 110, "stock", LIQUID)])))
    assert "+14bp" in page
    assert "n/a" in page


def test_health_shows_its_status_with_a_glyph_not_only_colour() -> None:
    d = digest_for(build([("BIG", 100, 110, "stock", LIQUID)]))
    page = panels.health_html(d)
    assert "OK" in page and 'class="status"' in page


def test_a_degraded_health_block_is_marked_per_row() -> None:
    result = build([("BIG", 100, 110, "stock", LIQUID)])
    d = digest_for(result)
    bad = digest_mod.Health(items=[
        digest_mod.HealthItem("prices_eod_raw", "9,102 / 14,133 (64%)",
                              ok=False, note="sweep looks truncated")])
    from dataclasses import replace

    page = panels.health_html(replace(d, health=bad))
    assert "DEGRADED" in page
    assert 'class="bad"' in page
    assert "sweep looks truncated" in page


def test_ticker_cells_are_addressable_for_the_detail_panel_later() -> None:
    """U3 hangs off this; the hook costs nothing now."""
    page = panels.screens_html(digest_for(build([("BIG", 100, 110, "stock", LIQUID)])))
    assert 'data-ticker="BIG"' in page


def test_the_full_page_stays_self_contained_with_a_digest() -> None:
    ctx = shell.Context(generated_at=datetime.now(timezone.utc), postgres=True)
    page = shell.render(ctx, digest=digest_for(build([("B", 100, 110, "stock", LIQUID)])))
    assert "<script>" in page
    for forbidden in ("http://", "https://", "src=", "<link", "fetch("):
        assert forbidden not in page
