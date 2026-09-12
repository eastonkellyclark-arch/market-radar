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
from conftest import COMPS_SETS, DCF_ROWS, PROXY_ROWS

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
    # min_adv_sessions=1: these fixtures carry two sessions and are
    # testing rendering, not the liquidity gate. The 30-session floor
    # has its own tests in test_volatility.py.
    return volatility.screen(
        con, prices=con.table("px"), actions=con.table("act"),
        min_adv_sessions=1,
    )


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


# --- which tab opens ----------------------------------------------------


def test_the_default_tab_is_liquid_ten_dollars_up() -> None:
    result = build([("BIG", 100, 110, "stock", LIQUID),
                    ("DWN", 100, 90, "stock", LIQUID)])
    assert panels.default_tab(result.lists) == ("stock", "$10+")


def test_the_loudest_band_takes_the_tab_when_it_beats_the_default() -> None:
    """So an unusual day in a quiet band is not hidden the morning it matters
    -- the same reason it was not hidden behind a collapsed header before."""
    result = build([("BIG", 100, 101, "stock", LIQUID),
                    ("MID", 5, 9, "stock", LIQUID)])
    assert panels.default_tab(result.lists) == ("stock", "$1-10")


def test_a_quiet_day_keeps_the_default_tab() -> None:
    """Otherwise the widest-moving band wins every day by construction, and
    sub-$1 moves more than $10+ almost every session."""
    result = build([("BIG", 100, 130, "stock", LIQUID),
                    ("MID", 5, 5.1, "stock", LIQUID)])
    assert panels.default_tab(result.lists) == ("stock", "$10+")


def test_the_tab_never_opens_on_an_ungated_list_when_a_liquid_one_exists(
) -> None:
    """The default view should not open on something untradeable. Nothing is
    hidden by this: every other tab is one click away."""
    result = build([("BIG", 100, 101, "stock", LIQUID),
                    ("THIN", 5, 20, "stock", THIN)])
    sec, band = panels.default_tab(result.lists)
    liquid_bands = {sl.band for sl in result.lists
                    if sl.rows and sl.liquidity == "liquid"}
    assert band in liquid_bands


def test_the_policy_is_one_function() -> None:
    """It is expected to be wrong at first; changing it must touch one place."""
    import inspect

    source = inspect.getsource(panels)
    assert source.count("def default_tab") == 1
    assert source.count("default_tab(") == 2  # the def and one call site


# --- rendering ----------------------------------------------------------


def test_every_list_is_present_even_when_its_tab_is_not_open() -> None:
    """All 24 are in the page; the script shows the two that match. Rendering
    only the open tab would mean re-rendering in JS to change tabs."""
    result = build([("BIG", 100, 110, "stock", LIQUID),
                    ("ETF", 20, 26, "etf", LIQUID)])
    page = panels.screens_html(digest_for(result))
    populated = [sl for sl in result.lists if sl.rows]
    assert page.count('<div class="list"') == len(populated)


def test_each_list_header_carries_its_top_row() -> None:
    """The headline travels with the list, so a glance at a tab is enough."""
    result = build([("BIG", 100, 110, "stock", LIQUID),
                    ("ETF", 20, 26, "etf", LIQUID)])
    page = panels.screens_html(digest_for(result))
    assert 'class="peek"' in page
    assert "ETF" in page


def test_the_screens_panel_is_readable_without_the_script() -> None:
    """The property the old <details> gave for free, now an explicit one.

    Tabs are driven by script, so with JS off nothing must be hidden by CSS:
    the page has to degrade to *every list visible*, not to a blank panel.
    No list may carry `hidden` or inline display:none in the markup.
    """
    result = build([("BIG", 100, 110, "stock", LIQUID),
                    ("ETF", 20, 26, "etf", LIQUID)])
    page = panels.screens_html(digest_for(result))
    for block in page.split('<div class="list"')[1:]:
        head = block[:200]
        assert "hidden" not in head, "a list is hidden in the markup"
        assert "display:none" not in head.replace(" ", "")
    assert "BIG" in page and "ETF" in page


def test_the_tabs_cover_both_axes_and_the_gate_is_a_toggle() -> None:
    result = build([("BIG", 100, 110, "stock", LIQUID),
                    ("ETF", 20, 26, "etf", LIQUID)])
    page = panels.screens_html(digest_for(result))
    assert 'data-axis="sec"' in page
    assert 'data-axis="band"' in page
    # The gate changes which names qualify, not which question is asked, so
    # it is one in-place toggle rather than a third tab axis.
    assert 'id="gate"' in page
    assert 'data-axis="liq"' not in page
    assert 'data-axis="dir"' not in page, "gainers and losers are read together"


def test_exactly_one_tab_per_axis_is_current() -> None:
    result = build([("BIG", 100, 110, "stock", LIQUID),
                    ("ETF", 20, 26, "etf", LIQUID)])
    page = panels.screens_html(digest_for(result))
    for axis in ("sec", "band"):
        row = page.split(f'data-axis-row="{axis}"', 1)[1].split("</div>", 1)[0]
        assert row.count("aria-current") == 1, f"{axis}: {row.count('aria-current')}"


def test_the_caveats_reach_the_panel(monkeypatch) -> None:
    """Whatever the screen dropped travels with the lists it applies to.

    These are shared with the digest and `mr screens` rather than restated
    here: three renderers each writing their own is exactly how thin_history
    ended up printed by one of them and neither of the other two.
    """
    from dataclasses import replace

    result = build([("BIG", 100, 110, "stock", LIQUID)])
    loud = replace(result, floor_excluded=7, gap_excluded=3, thin_history=4_249)
    page = panels.screens_html(digest_for(loud))

    assert "sanity floor" in page
    assert "gap of more than" in page
    assert "4,249 names cleared" in page
    assert page.count("<li>") >= 3


def test_the_three_renderers_cannot_drift_apart() -> None:
    """The fix for the defect, not just the defect.

    Every surface reads volatility.caveats; none of them writes its own.
    """
    import inspect

    from marketradar import digest as digest_mod

    for module in (panels, digest_mod):
        assert "caveats(" in inspect.getsource(module), module.__name__


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


#: The one URL allowed in the page, and it is not a request: the SVG DOM API
#: requires its namespace as a literal string. Allowlisted by exact value
#: rather than by loosening the scan, so a real CDN link still fails.
SVG_NS = "http://www.w3.org/2000/svg"


def _assert_self_contained(page: str) -> None:
    stripped = page.replace(SVG_NS, "")
    for forbidden in ("http://", "https://", "src=", "<link", "fetch(",
                      "XMLHttpRequest", "WebSocket", "import(", "@import"):
        assert forbidden not in stripped, (
            f"{forbidden!r} in the page. It is opened from file:// with no "
            "server and no network -- anything that leaves the disk is a "
            "broken panel for the one person who reads it."
        )


def test_the_full_page_stays_self_contained_with_a_digest() -> None:
    ctx = shell.Context(generated_at=datetime.now(timezone.utc), postgres=True)
    page = shell.render(ctx, digest=digest_for(build([("B", 100, 110, "stock", LIQUID)])))
    assert "<script>" in page
    _assert_self_contained(page)


def test_the_ticker_detail_script_is_scanned_too() -> None:
    """The self-containment scan ran on a page rendered *without* ticker
    detail, so ``tickers.SCRIPT`` -- the largest script in the page and the
    only one that draws -- was never examined. A CDN link in it would have
    shipped.
    """
    ctx = shell.Context(generated_at=datetime.now(timezone.utc), postgres=True)
    details = {"B": {"bars": [{"d": 20000, "c": "1.0", "v": 1000}],
                     "actions": [], "name": "B Corp"}}
    page = shell.render(
        ctx, digest=digest_for(build([("B", 100, 110, "stock", LIQUID)])),
        details=details)
    assert "createElementNS" in page, "the drawing script is in the page"
    _assert_self_contained(page)


# --- U15: peer sets -----------------------------------------------------


def test_a_peer_set_degraded_on_both_axes_shows_both_on_the_page() -> None:
    """The row the panel exists for, rendered rather than merely counted.

    A widened industry and a thinned set are different compromises. The page has
    to carry each, because a reader scanning a median column would see a
    perfectly ordinary 3.40 next to a set that is two steps removed from the
    industry and size that were asked for.
    """
    html = panels.comps_html(
        COMPS_SETS["rows"], COMPS_SETS["stats"], COMPS_SETS["funnel"])
    assert "WIDENED AND THINNED INC" in html
    # The depth is marked, not just printed: 2-digit is a fallback and reads as
    # one.
    assert "2-digit" in html
    assert 'class="collapsed">2-digit' in html
    # The peer count is a fraction, so the survivors cannot read as the set.
    assert "11 " in html and "of 75 banded" in html
    # And the caveat names both axes in words.
    assert "industry widened to 2-digit SIC" in html
    assert "$1M revenue floor" in html


def test_the_clean_set_carries_no_caveat_on_the_page() -> None:
    """The control. If a clean 4-digit set also rendered a caveat, the marking
    would mean nothing."""
    html = panels.comps_html(
        COMPS_SETS["rows"], COMPS_SETS["stats"], COMPS_SETS["funnel"])
    clean = html.split("CLEAN SET CORP", 1)[1].split("</tr>", 1)[0]
    assert "4-digit" in clean
    assert "collapsed" not in clean, "a clean set was marked as degraded"
    assert "banded" not in clean, "an intact set was shown as a fraction"


def test_the_panel_says_which_axes_and_does_not_lump_them() -> None:
    html = panels.comps_html(
        COMPS_SETS["rows"], COMPS_SETS["stats"], COMPS_SETS["funnel"])
    for axis in ("industry widened", "thinned", "both", "clean"):
        assert axis in html, f"the {axis!r} axis is not on the page"
    # Every outcome is named with what it means, so a blank is never guessed at.
    for outcome in ("served", "unplaceable", "immaterial", "too_few_peers"):
        assert outcome in html
    # And the unchosen floors are shown beside the chosen one.
    assert "coverage at floors that were not chosen" in html
    assert "$50M" in html


def test_the_panel_explains_turnover_rather_than_margin() -> None:
    """Net margin is unusable where revenue is near zero, and the page says so
    where a reader would otherwise ask why the obvious ratio is missing."""
    html = panels.comps_html(
        COMPS_SETS["rows"], COMPS_SETS["stats"], COMPS_SETS["funnel"])
    assert "Asset turnover, not net margin" in html
    assert "2834" in html
    assert "spread" in html, "a median with no spread cannot be distrusted"


def test_an_empty_comps_panel_says_what_to_run() -> None:
    html = panels.comps_html([], {}, None)
    assert "mr comps" in html
    assert "waiting" in html


# --- U13: DCF -----------------------------------------------------------


def test_the_dcf_panel_opens_at_the_readable_cohort() -> None:
    """**The thing a reader should not have to construct.** No row has zero
    substitutions, so "the good rows" is a cohort, and the panel opens filtered to
    it rather than leaving someone to compose a column filter every time.

    Note what the cohort already excludes: ``clean_but_constants`` means the
    substitutions are a subset of the two unavoidable constants, and
    ``growth_mismatch`` is deliberately not one of them -- so a cohort row cannot
    carry the mismatch flag. One filter, not two.
    """
    html = panels.dcf_html(DCF_ROWS["rows"], DCF_ROWS["stats"],
                           DCF_ROWS["funnel"])
    assert 'data-cohort="1"' in html
    assert 'data-cohort="0"' in html
    # The toggle is present and starts selected, so the page opens at the cohort.
    assert 'class="f sel" data-f="dcohort"' in html
    assert "best evidence only" in html
    assert "1,192" in html


def test_the_dcf_panel_shows_substitutions_in_the_row_not_a_footnote() -> None:
    """A deck or a screen built on this is the easiest place for the discipline to
    get laundered into something authoritative."""
    html = panels.dcf_html(DCF_ROWS["rows"], DCF_ROWS["stats"],
                           DCF_ROWS["funnel"])
    for name in ("peer_beta", "comp_depth_fallback", "absent_capex",
                 "growth_mismatch"):
        assert name in html, f"{name} is not on the page"
    # And each one says which direction it pushes the answer.
    assert "Understates" in html
    assert "Overstates" in html


def test_the_dcf_panel_says_no_row_is_clean_and_why() -> None:
    html = panels.dcf_html(DCF_ROWS["rows"], DCF_ROWS["stats"],
                           DCF_ROWS["funnel"])
    assert "No row has zero substitutions" in html
    assert "structural" in html
    # The rejected fitted rate is on the page, not just in the module.
    assert "loses" in html and "out of sample" in html


def test_the_dcf_panel_states_the_growth_bound_on_the_answer() -> None:
    """A lower bound for a growth company is not a valuation of one, and the page
    has to say that where the number is, not in an appendix."""
    html = panels.dcf_html(DCF_ROWS["rows"], DCF_ROWS["stats"],
                           DCF_ROWS["funnel"])
    assert "lower bound" in html
    assert "0.03" in html          # Amazon
    assert "0.96" in html          # Johnson & Johnson


def test_the_dcf_panel_reports_the_terminal_share() -> None:
    """A row whose answer is 90% terminal value is resting on one growth number
    however clean its other inputs are."""
    html = panels.dcf_html(DCF_ROWS["rows"], DCF_ROWS["stats"],
                           DCF_ROWS["funnel"])
    assert "terminal" in html
    assert "78%" in html


def test_an_empty_dcf_panel_says_what_to_run() -> None:
    html = panels.dcf_html([], {}, None)
    assert "mr dcf" in html
    assert "waiting" in html


# --- U16: proxy sections ------------------------------------------------


def flat(html: str) -> str:
    """Whitespace-collapsed, because the source wraps and a reader does not.

    Asserting on raw HTML made two of these fail on an f-string line break rather
    than on missing content -- a test that is sensitive to source formatting is
    testing the formatter.
    """
    import re as _re

    return _re.sub(r"\s+", " ", html)


def test_the_proxy_panel_reports_sections_and_not_figures() -> None:
    """**The distinction the panel exists for.** The locator shipped and the
    extraction was measured at 62% per figure and declined, so a panel that led
    with extracted numbers would advertise the half that was rejected.
    """
    html = panels.proxy_html(PROXY_ROWS["rows"], PROXY_ROWS["stats"])
    # Offsets and headings, which is what a reader needs to open the file.
    assert "362,171" in html
    assert "1,094,646" in html
    # And the decline is stated where the sections are, not in an appendix.
    text = flat(html)
    assert "62% across 45 cells" in text
    assert "The locator shipped and the extraction did not" in text
    assert "not on better prompting" in text


def test_the_proxy_panel_states_the_population_argument() -> None:
    """It is the stronger half of the decline and prompting cannot touch it:
    proxies exist only for companies being acquired."""
    html = panels.proxy_html(PROXY_ROWS["rows"], PROXY_ROWS["stats"])
    text = flat(html)
    assert "proxies exist only for companies being acquired" in text
    assert "2,564 valued filers" in text
    assert "under 20% of valuations" in text


def test_a_missing_section_is_shown_as_missing() -> None:
    """Varex has no fairness-opinion window. A blank cell is the honest rendering;
    a panel that only listed what it found would read as full coverage."""
    html = panels.proxy_html(PROXY_ROWS["rows"], PROXY_ROWS["stats"])
    varex = html.split("Varex Imaging Corp", 1)[1].split("</tr>", 1)[0]
    assert "--" in varex, "the absent section is not marked"


def test_the_panel_explains_why_density_chose_the_window() -> None:
    """In a real proxy the same heading matches in the table of contents, the
    body, the tax discussion and the appended merger agreement."""
    html = panels.proxy_html(PROXY_ROWS["rows"], PROXY_ROWS["stats"])
    text = flat(html)
    assert "table of contents" in text
    assert "annex" in text


def test_the_panel_says_projections_are_gated_by_arithmetic() -> None:
    """The one extracted field that survived, and only because a table can be
    checked against itself without knowing the right answer."""
    html = panels.proxy_html(PROXY_ROWS["rows"], PROXY_ROWS["stats"])
    text = flat(html)
    assert "without knowing the right answer" in text
    assert "incoherent" in text
    assert "76 of 76 values" in text
    assert "refused" in text
    # And the forecast/fact distinction is on the page.
    assert "source = 'extracted'" in html
    assert "forecast" in html and "fact" in html


def test_an_empty_proxy_panel_says_it_costs_nothing() -> None:
    html = panels.proxy_html([], {})
    assert "mr proxy" in html
    assert "costs nothing" in html
