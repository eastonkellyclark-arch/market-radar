"""The dashboard's script, run in a real DOM.

**Why this file exists.** 659 tests passed on a page where no tab worked and
the ticker script died on its first statement with a ReferenceError. Every one
of them asserted on the *text* of the generated HTML, so every one was green:
the markup said `data-axis="band"`, the script said `addEventListener`, and
nothing anywhere ran the two together. Three separate breakages hid in that
gap at once -- a free `svg`, four binder registrations with no consumer, and a
`default_tab` whose answer was computed and then dropped on the floor.

So this loads the page the user actually opens, in jsdom, with the scripts
running, and drives it with real click events. The assertions are about what
*changed* -- which panel is in the DOM, which lists are on screen, whether the
chart has nodes in it.

The rule this file keeps: **no assertion here may pass without the page having
run.** `test_the_probe_ran_at_all` is the guard on that, and a missing node or
jsdom is a failure with the command to fix it, never a skip. A skipped test is
the same green-without-looking that made this file necessary.

Install with `npm ci`. Node is test-only: nothing in `src/` touches it and
neither scheduled data job does -- the dashboard has no build step and still
ships as one static file. `.github/workflows/tests.yml` installs it, because
the failure-rather-than-skip rule above is only honest on a runner that was
actually given the toolchain.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from marketradar import digest as digest_mod
from marketradar.dashboard import shell
from marketradar.screens import volatility

REPO = Path(__file__).resolve().parent.parent
PROBE = REPO / "tests" / "js" / "page_probe.mjs"

D3, D4 = date(2026, 9, 3), date(2026, 9, 4)
LIQUID, THIN = 900_000, 1

#: Two origins, because they differ in one way that matters. A `file://` page
#: is an opaque origin and `localStorage` throws outright there in some
#: browsers, so the runtime guards every touch of it -- and a guard nothing
#: exercises is a guess. The http case proves the panel is remembered; the
#: file case proves the page still works when it cannot be.
ORIGINS: dict[str, str] = {
    "http": "https://mr.test/dashboard.html",
    "file": "file:///C:/Users/x/.dashboard/index.html",
}


# --- the page under test ------------------------------------------------


def _screens() -> volatility.ScreenResult:
    """Enough of a market to fill both tab axes and both sides of the gate.

    BIG carries the loudest liquid move on purpose, which makes stock/$10+
    `default_tab`'s answer for this fixture -- and BIG is also the one symbol
    the chart payload has history for, so the row the ticker test needs is on
    screen when the panel opens. The first version of this fixture put a +80%
    move in $1-10, the panel opened there exactly as the policy says it
    should, and the ticker row was three clicks away.
    """
    rows = [
        ("BIG", 100, 140, "stock", LIQUID),    # stock  $10+   gainer  liquid
        ("DWN", 100, 88, "stock", LIQUID),     # stock  $10+   loser   liquid
        ("THA", 40, 46, "stock", THIN),        # stock  $10+   gainer  ungated
        ("THB", 40, 35, "stock", THIN),        # stock  $10+   loser   ungated
        ("MID", 5, 6, "stock", LIQUID),        # stock  $1-10  gainer  liquid
        ("MDW", 5, 4.4, "stock", LIQUID),      # stock  $1-10  loser   liquid
        ("PNY", 0.40, 0.46, "stock", LIQUID),  # stock  sub-$1 gainer  liquid
        ("ETF", 20, 24, "etf", LIQUID),        # etf    $10+   gainer  liquid
    ]
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
    # min_adv_sessions=1 as elsewhere: these fixtures carry two sessions and
    # are testing the page, not the 30-session gate.
    return volatility.screen(
        con, prices=con.table("px"), actions=con.table("act"),
        min_adv_sessions=1,
    )


def _digest() -> digest_mod.Digest:
    return digest_mod.Digest(
        day=D4, prior_day=D3,
        generated_at=datetime(2026, 9, 10, 21, 0, tzinfo=timezone.utc),
        health=digest_mod.Health(items=[digest_mod.HealthItem("prices", "ok")]),
        macro=[digest_mod.MacroLine("10y Treasury", Decimal("4.77"), "%", D3,
                                    {"30d": Decimal("0.14"), "1y": None})],
        screen=_screens(), names={}, new_tickers={}, top_n=20,
        include_ungated=True,
    )


#: Day numbers are days since 2016-01-01, which is what the chart's EPOCH is.
_T0 = (date(2026, 8, 3) - date(2016, 1, 1)).days


def _details() -> dict:
    """Chart payload for one symbol only.

    One, deliberately: the panel has rows for eight tickers, so a payload
    covering every row could not tell a working click apart from a click that
    opened the panel regardless of whether there was anything to draw.
    """
    series = [[_T0 + i, 100.0 + i] for i in range(24)]
    return {
        "BIG": {
            "s": series,
            "r": [[_T0 + 23, 122.0, 124.0, 121.0, 123.0, 500_000]],
            "a": [[_T0 + 10, 1.0, 0.25]],
            "g": [[_T0 + 4, _T0 + 9, 5]],
        }
    }


def _ctx() -> shell.Context:
    """Every panel fed, so every panel script is emitted and every binder runs.

    A thinner context was the first version and it hid something: with no
    deals and no filings, `render` emitted one binder instead of four, and the
    probe reported a page where the runtime worked because there was almost
    nothing for it to do.
    """
    return shell.Context(
        generated_at=datetime(2026, 9, 10, 21, 0, tzinfo=timezone.utc),
        postgres=True,
        macro={"DGS10": "2026-09-03"},
        prices={"2025": {"rows": 1_400_000, "max_date": "2025-12-31"},
                "2026": {"rows": 1_200_000, "max_date": "2026-09-04"}},
        entities={"companies": 8_005, "tickers": 10_412},
        filings={"edgar_filing": {"count": 172, "latest": "2026-09-08 20:02:00"}},
        recent_filings=[
            {"form_type": "8-K", "company": "ACME CORP", "cik": "0000001",
             "filed_at": "2026-09-08 14:00:00", "accession": "0000001-26-000001"},
            {"form_type": "4", "company": "BETA INC", "cik": "0000002",
             "filed_at": "2026-09-08 13:00:00", "accession": "0000002-26-000002"},
        ],
        clusters=[
            {"role": "insider", "symbol": "BIG", "issuer_cik": "0000001",
             "issuer_name": "ACME CORP", "n_buyers": 3, "value": 250_000,
             "first": "2026-09-02", "last": "2026-09-04", "buyers": [],
             "fund_like": False, "fund_why": "", "planned_buys": 0},
            {"role": "ten_percent", "symbol": "DWN", "issuer_cik": "0000002",
             "issuer_name": "BETA INC", "n_buyers": 2, "value": 4_000_000,
             "first": "2026-09-03", "last": "2026-09-03", "buyers": [],
             "fund_like": True, "fund_why": "name matches a fund pattern",
             "planned_buys": 0},
        ],
        deals=[
            {"deal_type": "merger", "agree": True, "value_usd": 1_200_000_000,
             "value_basis": "stated", "value_text": "$1.2 billion",
             "target_financials": "included", "exhibit_signal": True,
             "text_signal": "merger", "filer_role": "acquirer",
             "counterparty": "TARGET CO", "company": "ACME CORP",
             "consideration": "cash", "items": "1.01", "filed": "2026-09-08"},
            {"deal_type": "asset_purchase", "agree": False, "value_usd": None,
             "value_basis": "none", "value_text": None,
             "target_financials": "rule_305_promised", "exhibit_signal": False,
             "text_signal": None, "filer_role": "target",
             "counterparty": None, "company": "BETA INC",
             "consideration": "stock", "items": "2.01", "filed": "2026-09-07"},
        ],
        outcomes=[
            {"study": "8-K deals", "slice": "all", "horizon": 5, "n": 1200,
             "median_ret": 0.011, "median_excess": -0.0236, "mean_excess": -0.02,
             "win_rate": 0.48, "median_run_up": 0.004, "n_suspect": 3,
             "events": 1200, "priced": 800},
        ],
        review=[
            {"sponsor_name": "OLD MACHINE SHOP INC", "ein": "111111111",
             "matched_name": "OLD MACHINE SHOP CORP", "matched_cik": "0000003",
             "naics": "332710", "state": "TX", "candidates": 2,
             "match_basis": "normalized name", "status": "pending"},
        ],
        review_counts={"pending": 1, "confirmed": 0, "rejected": 0},
    )


def _private() -> list[dict]:
    return [{
        "ein": "111111111", "sponsor_name": "OLD MACHINE SHOP INC",
        "state": "TX", "naics": "332710", "plans": 1,
        "participants_max": 115, "participants_sum": 115, "is_dfe": False,
        "trend": "flat", "status": "filing", "pct_change": -0.04,
        "first_year": 2022, "last_year": 2024, "years_filed": 3,
        "pending_years": 0, "gap_years": 0,
        "series": [{"year": 2022, "participants": 120},
                   {"year": 2023, "participants": 118},
                   {"year": 2024, "participants": 115}],
    }]


def _mature() -> list[dict]:
    return [{
        "ein": "111111111", "sponsor_name": "OLD MACHINE SHOP INC",
        "naics": "332710", "city": "AUSTIN", "state": "TX",
        "oldest_plan_eff": date(1979, 1, 1), "age_years": 47.7,
        "participants_last": 148, "participants_sum": 148,
        "active_last": 115, "active_sum": 115,
        "trend": "flat", "status": "filing", "pct_change": -0.04,
        "first_year": 2022, "last_year": 2024, "years_filed": 3,
        "pending_years": 0, "gap_years": 0,
        "series": [{"year": 2022, "participants": 120},
                   {"year": 2023, "participants": 118},
                   {"year": 2024, "participants": 115}],
    }]


STATS = {"plan_year": 2024, "sponsors": 4, "private": 3, "dfe": 1,
         "listed": 0, "ambiguous": 0, "completeness": ""}
MATURE_STATS = {"plan_year": 2024, "sponsors": 4, "candidates": 1,
                "completeness": "", "min_age": 40, "max_participants": 500}


def _page() -> str:
    return shell.render(
        _ctx(), digest=_digest(), details=_details(),
        private=_private(), private_stats=STATS,
        mature=_mature(), mature_stats=MATURE_STATS,
    )


# --- running it ---------------------------------------------------------


def _require_toolchain() -> str:
    """Missing tooling fails, and never skips.

    A skipped test reports the same green as a passing one to anybody reading
    a summary line, which is precisely the failure this file was written to
    stop. If the check cannot run, the suite says so.
    """
    node = shutil.which("node")
    if node is None:
        pytest.fail(
            "node is not on PATH, so the dashboard's script was never run. "
            "Install Node 20+ and run `npm ci` in the repo root."
        )
    if not (REPO / "node_modules" / "jsdom").is_dir():
        pytest.fail(
            "jsdom is not installed, so the dashboard's script was never run. "
            "Run `npm ci` in the repo root. It is a test-only dependency: "
            "nothing in src/ touches node and neither data job does."
        )
    return node


@pytest.fixture(scope="module")
def reports(tmp_path_factory) -> dict[str, dict]:
    """Drive the page once per origin and hand back the two reports."""
    node = _require_toolchain()
    out = tmp_path_factory.mktemp("dashboard")
    page = out / "index.html"
    page.write_text(_page(), encoding="utf-8")

    collected: dict[str, dict] = {}
    for name, url in ORIGINS.items():
        proc = subprocess.run(
            [node, str(PROBE), str(page), url],
            cwd=REPO, capture_output=True, text=True, timeout=180,
        )
        if proc.returncode != 0:
            pytest.fail(
                f"the probe died on the {name} origin "
                f"(exit {proc.returncode}):\n{proc.stderr[-4000:]}"
            )
        collected[name] = json.loads(proc.stdout)
    return collected


@pytest.fixture(scope="module")
def report(reports) -> dict:
    return reports["http"]


def _probe(page_html: str, tmp_path, url: str = ORIGINS["http"]) -> dict:
    """One off-fixture run, for pages the fixture deliberately damages."""
    node = _require_toolchain()
    page = tmp_path / "index.html"
    page.write_text(page_html, encoding="utf-8")
    proc = subprocess.run(
        [node, str(PROBE), str(page), url],
        cwd=REPO, capture_output=True, text=True, timeout=180,
    )
    if proc.returncode != 0:
        pytest.fail(
            f"the probe died (exit {proc.returncode}): "
            + proc.stderr[-4000:]
        )
    return json.loads(proc.stdout)


def step(report: dict, name: str) -> dict:
    for s in report["steps"]:
        if s["name"] == name:
            return s
    raise AssertionError(
        f"the probe never reached step {name!r}; it got as far as "
        + ", ".join(s["name"] for s in report["steps"])
    )


# --- the guard on this file ---------------------------------------------


def test_the_probe_ran_at_all(report) -> None:
    """The check on the check.

    Everything below reads a report. A report that came back empty -- a probe
    that threw before its first click, a selector that matched nothing --
    would make every assertion below vacuously true, which is exactly the
    shape of the problem this file exists to catch. So: the probe must have
    reached the end, and the page must have had something in it.
    """
    names = [s["name"] for s in report["steps"]]
    assert names[0] == "load"
    assert names[-1] == "hash:outcomes", f"the probe stopped early at {names[-1]}"
    assert len(names) >= 20, names
    assert report["renderedPanelCount"] == len(shell.PANELS)


# --- the three breakages that started this -------------------------------


def test_the_page_runs_without_a_single_error(report) -> None:
    """The one that would have caught the ReferenceError.

    `if (!box || !svg) return;` survived an edit that replaced both variables
    with functions. A free identifier is a ReferenceError rather than a falsy
    value, so it did not skip the drawer -- it killed the whole ticker script,
    including the delegated click handlers that have nothing to do with the
    chart. No string assertion can see that. Running it does.

    console.error counts: the runtime catches a throwing binder so one broken
    panel cannot blank the others, and that is right in a browser and wrong to
    let pass here.
    """
    assert report["errors"] == [], json.dumps(report["errors"], indent=2)


def test_every_panel_script_registers_a_binder_and_the_runtime_runs_them(
    report,
) -> None:
    """The one that would have caught four registrations with no consumer.

    Each panel script became `__MR_BINDERS__.push(fn)` and nothing called
    them, so every tab, filter and sort on the page was inert while the
    markup that declared them stayed byte-identical.
    """
    load = step(report, "load")
    # A literal on purpose, and it has to be bumped deliberately. The value of
    # this test is that adding a script without wiring it -- or wiring one twice
    # -- moves this number, which a count derived from the scripts themselves
    # could never notice.
    assert load["binders"] == 5, (
        f"{load['binders']} binders registered; expected screens, feed, deals, "
        "private and dcf"
    )
    assert load["hasShowPanel"], "no runtime: the binders have no consumer"
    # A binder having *run* is the part that matters, and the proof is that
    # something it does is visible: 8 lists in the DOM, 2 of them on screen.
    screens = step(report, "nav:screens")
    assert screens["listsInDom"] > len(screens["visibleLists"]), (
        "no list was hidden, so the screens binder never ran"
    )


def test_the_screens_panel_opens_on_a_tab_that_has_lists_in_it(report) -> None:
    """The one that would have caught `default_tab` being dropped on the floor.

    The markup marked the alphabetically-first tab on each axis current and
    ignored the policy function's answer entirely, so the panel opened on
    etfs/$1-10 -- routinely empty -- while every test of `default_tab` itself
    went on passing.
    """
    screens = step(report, "nav:screens")
    assert screens["visibleLists"], "the screens panel opened on an empty tab"
    # stock/$10+ is what `default_tab` returns for this fixture: BIG carries
    # the loudest liquid move and it is in that band, so the default stands.
    # The policy itself is unit-tested in test_dashboard_panels.py; what is
    # being checked here is that the page honours the answer at all.
    current = {t["value"] for t in screens["secTabs"] if t["current"]}
    assert current == {"stock"}, current
    bands = {t["value"] for t in screens["bandTabs"] if t["current"]}
    assert bands == {"$10+"}, bands


# --- navigation ---------------------------------------------------------


def test_one_panel_is_in_the_dom_and_the_page_still_renders_them_all(
    report,
) -> None:
    """Both halves of the trade.

    The page ships every panel so it degrades to a long readable document with
    the script off; the runtime takes them out so the one you want is not
    below the fold. Neither half is worth much alone.
    """
    assert report["renderedPanelCount"] == len(shell.PANELS)
    for s in report["steps"]:
        assert s["panelsInDom"] == 1, f"{s['name']}: {s['panelsInDom']} panels"


def test_the_sidebar_reaches_every_panel(report) -> None:
    load = step(report, "load")
    assert load["navLinks"] == len(shell.PANELS)
    assert load["panelIds"] == [p.id for p in shell.PANELS]
    for panel in shell.PANELS:
        assert step(report, f"nav:{panel.id}")["open"] == panel.id


def test_the_sidebar_marks_where_you_are(report) -> None:
    """aria-current follows the injection, or the map stops being a map."""
    for panel in shell.PANELS:
        s = step(report, f"nav:{panel.id}")
        assert s["navCurrent"] == panel.id, (
            f"opened {s['open']} but the sidebar points at {s['navCurrent']}"
        )


def test_the_open_panel_is_remembered(report) -> None:
    for panel in shell.PANELS:
        assert step(report, f"nav:{panel.id}")["stored"] == panel.id


def test_a_hash_opens_the_panel_it_names(report) -> None:
    assert step(report, "hash:outcomes")["open"] == "outcomes"


def test_the_page_works_where_localstorage_throws(reports) -> None:
    """A `file://` page is an opaque origin, which is where the dashboard
    actually lives. Every touch of localStorage is wrapped for that reason,
    and this is what proves the wrapping rather than assuming it.
    """
    filed = reports["file"]
    assert filed["errors"] == [], json.dumps(filed["errors"], indent=2)
    assert step(filed, "nav:screens")["open"] == "screens"
    assert step(filed, "nav:screens")["visibleLists"]
    # Either the origin allowed it or the guard caught it -- never a crash.
    assert step(filed, "nav:screens")["stored"] in ("screens", "<threw>", None)


# --- the screens panel --------------------------------------------------


def test_pressing_a_band_tab_changes_which_lists_are_on_screen(report) -> None:
    opened = step(report, "nav:screens")
    switched = next(s for s in report["steps"] if s["name"].startswith("tab:band="))
    assert switched["visibleLists"] != opened["visibleLists"]
    assert switched["visibleLists"], "the band tab emptied the panel"
    band = switched["name"].split("=", 1)[1]
    for title in switched["visibleLists"]:
        assert band in title, f"{title} is not in band {band}"


def test_pressing_a_tab_on_one_axis_leaves_the_other_alone(report) -> None:
    """Two axes, not one filter stack. Changing the band must not silently
    move you to a different security type as well."""
    for s in report["steps"]:
        if not s["name"].startswith("tab:"):
            continue
        assert s["otherAxisUnchanged"], s["name"]
        current = [t for t in s["tabs"] if t["current"]]
        assert len(current) == 1, f"{s['name']}: {len(current)} tabs current"


def test_gainers_and_losers_are_shown_together(report) -> None:
    """Direction is deliberately not an axis: a name near the top of one list
    and the bottom of the other is the case worth seeing, and separating them
    hides it."""
    titles = step(report, "nav:screens")["visibleLists"]
    assert any("gainers" in t for t in titles), titles
    assert any("losers" in t for t in titles), titles


def test_the_gate_swaps_each_list_for_its_gated_twin(report) -> None:
    """The gate is applied in place rather than being a third tab axis: it
    changes which names qualify, not which question is being asked."""
    off = step(report, "gate:off")["visibleLists"]
    on = step(report, "gate:on")["visibleLists"]
    assert off and on, (off, on)
    assert all("ADV" not in t for t in off), off
    assert all("ADV" in t for t in on), on
    assert len(on) == len(off)


def test_the_band_counts_follow_the_chosen_security_type(report) -> None:
    """The number on a band tab has to be the number you get when you press
    it, or it is describing a list you are not looking at."""
    screens = step(report, "nav:screens")
    counts = {t["value"]: t["count"] for t in screens["bandTabs"]}
    assert counts, screens["bandTabs"]
    assert all(c is not None for c in counts.values()), counts
    assert counts["$10+"] not in (None, "0")


# --- the ticker panel ---------------------------------------------------


def test_a_ticker_row_opens_the_ticker_panel_and_draws_into_it(report) -> None:
    """The hand-off the whole rework turns on.

    The chart elements do not exist until the ticker panel is injected, which
    is why the drawing code stopped resolving them at load and why the shell
    has to open the panel before it asks for a draw.
    """
    s = step(report, "click:ticker-row")
    assert s["open"] == "ticker", f"a ticker row left {s['open']} open"
    assert s["clickedSymbol"] == "BIG"
    assert s["drawerSymbol"] == "BIG"
    assert s["drawerHidden"] is False, "the drawer opened still hidden"
    assert s["chartNodes"] > 0, "the ticker panel opened with an empty chart"
    assert s["barRows"] == 1


def test_closing_the_ticker_returns_to_the_panel_it_came_from(report) -> None:
    assert step(report, "click:close")["open"] == "screens"


def test_a_row_with_no_history_does_nothing_at_all(report) -> None:
    """Not "opens an empty chart". The payload carries one symbol out of
    eight, and a click on one of the other seven has nothing to show."""
    s = step(report, "click:row-without-history")
    assert s["clickedSymbol"] != "BIG"
    assert s["open"] == "screens", (
        f"a row with no history switched to {s['open']}"
    )


def test_one_broken_script_does_not_take_the_rest_of_the_page_down(
    tmp_path,
) -> None:
    """The blast radius of a broken script is that script.

    Found by mutation rather than by design: reintroducing the chart's
    ReferenceError failed *eight* tests, because every script was concatenated
    into one tag and an error in the first killed the navigation two scripts
    later. That is the same failure the binder loop catches one level down --
    one broken panel must not blank the others -- so it is fixed the same way,
    and checked the same way: break one on purpose and see what survives.

    The break is injected into the rendered page rather than into a module,
    so this measures the tag layout rather than any particular script.
    """
    page = _page()
    head, sep, tail = page.partition("<script>")
    assert sep, "the page emitted no script at all"
    # At the *top* of the first script, not the bottom. The first version of
    # this test appended the break before the closing tag, where everything
    # had already run and a single shared tag passed it just as happily as
    # separate ones -- a check that could not fail, in the file whose whole
    # subject is checks that cannot fail.
    broken = head + sep + "throw new Error('mr-test-boom');" + tail

    report = _probe(broken, tmp_path)

    messages = " ".join(e["message"] for e in report["errors"])
    assert "mr-test-boom" in messages, (
        "the injected break never ran, so this proves nothing"
    )
    # ...and the runtime, which comes later, still did its whole job.
    assert step(report, "load")["hasShowPanel"]
    assert step(report, "nav:screens")["open"] == "screens"
    assert step(report, "nav:deals")["open"] == "deals"
    assert step(report, "hash:outcomes")["open"] == "outcomes"


# --- what jsdom would not do --------------------------------------------


def test_the_only_unimplemented_calls_are_scrolling(report) -> None:
    """jsdom does no layout, so scrolling is a no-op there. Asserted rather
    than filtered: an unimplemented call the page started *relying* on would
    otherwise disappear into the same silence as everything else here.
    """
    kinds = {m.split(":")[1].strip() for m in report["notImplemented"]}
    assert all("scroll" in k.lower() for k in kinds), kinds
