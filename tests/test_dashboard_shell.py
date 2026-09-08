"""The dashboard shell. No network, no database.

The property that matters: a panel that cannot work says *why*. An absent
panel is indistinguishable from a broken one, which is the whole reason the
shell declares things it cannot yet draw.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from marketradar.dashboard import shell


def ctx(**kw) -> shell.Context:
    base = dict(
        generated_at=datetime(2026, 9, 8, 21, 0, tzinfo=timezone.utc),
        postgres=True,
        macro={"DGS10": "2026-09-03"},
        prices={"2026": {"rows": 14_574, "max_date": "2026-09-04"}},
        entities={"companies": 8_005, "tickers": 10_412},
        filings={"edgar_filing": {"count": 172, "latest": "2026-09-08 20:02:00"}},
    )
    base.update(kw)
    return shell.Context(**base)


# --- the registry -------------------------------------------------------


def test_every_panel_in_the_build_order_is_declared() -> None:
    """Including the six that do not exist. The shell is the map."""
    ids = {p.id for p in shell.PANELS}
    for expected in ("health", "macro", "screens", "clusters", "news",
                     "private", "review", "xbrl", "teardowns", "multiples",
                     "outcomes", "dcf", "decks", "ticker", "dod", "liquidity"):
        assert expected in ids, f"{expected} is missing from the panel map"


def test_panels_are_grouped_into_declared_sections() -> None:
    assert {p.section for p in shell.PANELS} == set(shell.SECTIONS)


def test_panel_ids_are_unique() -> None:
    ids = [p.id for p in shell.PANELS]
    assert len(ids) == len(set(ids))


# --- states -------------------------------------------------------------


def test_a_panel_with_no_probe_is_on_the_roadmap() -> None:
    state, detail = shell.Panel("x", "X", "Analysis", "…",
                                weekend="Weekend 9").resolve(ctx())
    assert state == shell.NOT_BUILT
    assert detail == "Weekend 9"


def test_a_waiting_panel_says_what_it_is_waiting_on() -> None:
    """The distinction the whole shell exists to draw."""
    for panel in shell.PANELS:
        state, detail = panel.resolve(ctx())
        if state == shell.WAITING:
            assert detail.strip(), f"{panel.id} is waiting but does not say why"


def test_a_not_built_panel_names_its_weekend() -> None:
    for panel in shell.PANELS:
        state, detail = panel.resolve(ctx())
        if state == shell.NOT_BUILT:
            assert detail.strip(), f"{panel.id} gives no weekend"


def test_missing_macro_names_the_command_that_fixes_it() -> None:
    state, detail = shell.Panel(
        "macro", "Macro", "Markets", "…", probe=shell._probe_macro
    ).resolve(ctx(macro={}))
    assert state == shell.WAITING
    assert "mr fred" in detail


def test_missing_prices_names_the_command_that_fixes_it() -> None:
    state, detail = shell._probe_screens(ctx(prices={}))
    assert state == shell.WAITING
    assert "mr prices" in detail


def test_no_database_leaves_health_waiting_rather_than_crashing() -> None:
    state, _ = shell._probe_health(ctx(postgres=False))
    assert state == shell.WAITING


def test_the_liquidity_gate_is_waiting_on_a_fix_not_on_data() -> None:
    """It is a correctness gap the backfill activates, not a data gap.

    Twelve of the twenty-four lists are gated on a number that silently
    becomes a multi-year average once history lands.
    """
    state, detail = shell._probe_liquidity(ctx())
    assert state == shell.WAITING
    assert "trailing-window" in detail
    assert "12 of the 24" in detail


# --- rendering ----------------------------------------------------------


def test_state_is_never_carried_by_colour_alone() -> None:
    """Status rule from the dataviz skill: colour pairs with icon and label."""
    page = shell.render(ctx())
    for state in (shell.LIVE, shell.WAITING, shell.NOT_BUILT):
        colour, glyph = shell.STATE_STYLE[state]
        assert colour in page
        assert glyph in page
        assert state in page


def test_not_built_uses_muted_ink_rather_than_a_status_colour() -> None:
    """A roadmap state is not an alarm and must not compete with `waiting`."""
    assert shell.STATE_STYLE[shell.NOT_BUILT][0] == "#898781"
    statuses = {shell.STATE_STYLE[s][0] for s in (shell.LIVE, shell.WAITING)}
    assert shell.STATE_STYLE[shell.NOT_BUILT][0] not in statuses


def test_every_panel_reaches_the_page() -> None:
    page = shell.render(ctx())
    for panel in shell.PANELS:
        assert f'data-panel="{panel.id}"' in page


def test_dark_mode_is_declared_under_both_scopes() -> None:
    """The media query covers the OS setting; the attribute covers a toggle."""
    page = shell.render(ctx())
    assert "prefers-color-scheme: dark" in page
    assert '[data-theme="dark"]' in page
    assert ':not([data-theme="light"])' in page


def test_the_page_is_self_contained() -> None:
    """No server, no build step, no network at view time."""
    page = shell.render(ctx())
    for forbidden in ("<script", "http://", "https://", "fetch(", "<link"):
        assert forbidden not in page, f"page reaches for {forbidden}"


def test_panel_text_is_escaped() -> None:
    page = shell.render(
        ctx(), panels=(shell.Panel("x", "<script>alert(1)</script>", "Markets",
                                   "…", weekend="W9"),)
    )
    assert "<script>" not in page
    assert "&lt;script&gt;" in page


def test_the_footer_states_why_it_is_never_published() -> None:
    page = shell.render(ctx())
    assert "redistribution" in page
    assert "Tiingo" in page


# --- the publishing boundary -------------------------------------------


def test_the_module_has_no_publish_path() -> None:
    """Same enforcement as sources/fred.py: absence, not a flag.

    The screens are computed from Tiingo prices, so a public host would be
    redistribution -- the boundary that put prices in R2.
    """
    import inspect

    source = inspect.getsource(shell)
    for forbidden in ("github_release", "gh release", "gh_pages", "publish("):
        assert forbidden not in source
    assert not hasattr(shell, "publish")


def test_the_default_output_is_the_gitignored_path() -> None:
    from pathlib import Path

    assert shell.DEFAULT_OUTPUT == Path(".dashboard") / "index.html"
    ignored = Path(__file__).resolve().parents[1] / ".gitignore"
    assert ".dashboard/" in ignored.read_text(encoding="utf-8")


# --- summary ------------------------------------------------------------


def test_summary_counts_every_panel_once() -> None:
    counts = shell.summary(ctx())
    assert sum(counts.values()) == len(shell.PANELS)


def test_console_output_stays_ascii() -> None:
    """These strings reach a Windows console that is not UTF-8.

    The glyphs are HTML-only; everything printed by `mr dashboard` -- panel
    titles, states, and the waiting detail -- has to survive cp1252.
    """
    for panel in shell.PANELS:
        state, detail = panel.resolve(ctx())
        for text in (panel.title, state, detail):
            text.encode("ascii")
