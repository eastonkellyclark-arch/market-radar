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
    for expected in ("health", "macro", "screens", "news",
                     "clusters_insider", "clusters_tenpct",
                     "private", "review", "xbrl", "deals", "multiples",
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


DEEP = {str(y): {"rows": 1_500_000, "max_date": "2026-09-08"}
        for y in range(2016, 2027)}


def test_the_liquidity_gate_goes_live_once_there_is_history() -> None:
    state, detail = shell._probe_liquidity(ctx(prices=DEEP))
    assert state == shell.LIVE
    assert "30-session" in detail
    assert "11 years" in detail


def test_the_liquidity_gate_waits_when_there_is_no_history() -> None:
    """A sweep-only partition is tens of thousands of rows, not millions."""
    thin = {"2026": {"rows": 42_484, "max_date": "2026-09-08"}}
    state, _ = shell._probe_liquidity(ctx(prices=thin))
    assert state == shell.WAITING


def test_ticker_detail_moves_from_waiting_to_not_built() -> None:
    """Blocked on the backfill, then blocked on U3. Different answers.

    A panel that still said "waiting on the backfill" after the backfill
    landed would be the shell lying about its own roadmap.
    """
    assert shell._probe_ticker_detail(ctx(prices={}))[0] == shell.WAITING
    state, detail = shell._probe_ticker_detail(ctx(prices=DEEP))
    assert state == shell.NOT_BUILT
    assert "U3" in detail


def test_day_over_day_goes_live_with_history() -> None:
    assert shell._probe_day_over_day(ctx(prices={}))[0] == shell.WAITING
    assert shell._probe_day_over_day(ctx(prices=DEEP))[0] == shell.LIVE


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


def test_the_page_reaches_for_nothing_external() -> None:
    """Self-contained is the constraint, not script-free.

    Sorting and filtering need a little script, and inline script keeps the
    file one file. What must never appear is anything that leaves the disk:
    a `file://` page has no server behind it, and a CDN reference would make
    the dashboard depend on being online to render yesterday's numbers.
    """
    page = shell.render(ctx())
    _assert_no_external(page)


#: The one URL allowed to appear. createElementNS needs it to make an SVG
#: element; it is a namespace identifier and is never fetched. Named here so
#: the exception is explicit rather than the rule quietly weakening.
SVG_NS = "http://www.w3.org/2000/svg"


def _assert_no_external(page: str) -> None:
    stripped = page.replace(SVG_NS, "")
    for forbidden in ("http://", "https://", "fetch(", "<link", "src=",
                      "XMLHttpRequest", "import("):
        assert forbidden not in stripped, f"page reaches for {forbidden}"


def test_the_chart_carries_no_external_reference_either() -> None:
    """The ticker payload and chart script ship in the same file.

    Checked separately because the map alone has no script, so the rule above
    was passing without ever seeing the code that draws.
    """
    from marketradar.dashboard import detail

    page = shell.render(ctx(), digest=None, details={"AAA": {"s": [[0, 1.0]]}})
    assert detail.SCRIPT.strip()[:20] in page or "window.__TK__" in page
    _assert_no_external(page)


def test_the_map_alone_carries_no_script() -> None:
    """With no digest there is nothing to sort, so nothing is emitted."""
    assert "<script" not in shell.render(ctx())


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


# --- U6: two cluster panels ---------------------------------------------


def test_clusters_are_two_panels_not_one() -> None:
    """Same reasoning as the screen lists: the medians are 78x apart, so one
    floor cannot serve both and one panel would imply it could."""
    ids = {p.id for p in shell.PANELS}
    assert {"clusters_insider", "clusters_tenpct"} <= ids
    assert "clusters" not in ids


def test_cluster_panels_wait_until_something_is_stored() -> None:
    for probe in (shell._probe_clusters_insider, shell._probe_clusters_tenpct):
        state, detail = probe(ctx(clusters=[]))
        assert state == shell.WAITING
        assert "mr form4" in detail


def test_each_cluster_panel_counts_only_its_own_role() -> None:
    rows = [{"role": "insider"}, {"role": "insider"}, {"role": "ten_percent"}]
    assert "2 officer/director" in shell._probe_clusters_insider(ctx(clusters=rows))[1]
    assert "1 10%-holder" in shell._probe_clusters_tenpct(ctx(clusters=rows))[1]


# --- a live panel with no body is a bug ---------------------------------


def test_a_live_panel_always_has_a_body() -> None:
    """The probe and the body have independent sources, and nothing used to
    check they agreed.

    ``_probe_screens`` reads ``dataset_stats`` from Postgres while the body
    comes from the digest, so ``mr dashboard --fast`` -- which skips the
    digest -- produced a panel chipped **live**, carrying a confident
    freshness line ("20,175,249 bars across 11 partitions"), with an empty
    slot. Indistinguishable from a panel nobody wired up, which is the one
    distinction this shell exists to draw.
    """
    from datetime import datetime, timezone

    from marketradar.dashboard import shell

    ctx = shell.Context(generated_at=datetime.now(timezone.utc), postgres=True)
    page = shell.render(ctx)
    offenders = []
    for panel in shell.PANELS:
        state, _ = panel.resolve(ctx)
        if state != shell.LIVE:
            continue
        block = page.split(f'data-panel="{panel.id}"', 1)
        if len(block) < 2:
            offenders.append(f"{panel.id}: panel missing from the page")
            continue
        article = block[1].split("</article>", 1)[0]
        if '<div class="slot"></div>' in article:
            offenders.append(f"{panel.id}: chipped live with an empty slot")
    assert not offenders, (
        "Panels claiming live while rendering nothing:\n  "
        + "\n  ".join(offenders)
        + "\n\nEither the probe is wrong or the body was not built. A panel "
        "with no data must say so -- every renderer has an empty-state "
        "message naming the command that fills it."
    )


def test_every_panel_body_renderer_handles_no_data() -> None:
    """The nine empty-state messages were unreachable for months: every one
    sat behind an ``if <data>:`` in render(), so a panel with zero rows
    rendered byte-identical to one that did not exist."""
    from marketradar.dashboard import panels

    for fn, args in (
        (panels.filings_html, ([],)),
        (panels.deals_html, ([],)),
        (panels.outcomes_html, ([],)),
        (panels.private_html, ([], {})),
        (panels.mature_html, ([], {})),
        (panels.review_html, ([], {})),
    ):
        out = fn(*args)
        assert 'class="empty"' in out, f"{fn.__name__} renders nothing for []"
        assert "mr " in out, f"{fn.__name__} does not name the command to run"
