"""The two Form 5500 panels: the participant trend, and mature targets.

Both render bounds rather than measurements, and every test here is about the
panel saying which bound it is showing. The trend cell is the one that
matters most: three different absences reach it -- lapsed, pending, gap --
and collapsing any of them into "declined" is the failure the whole series
exists to prevent.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from marketradar.dashboard import panels, shell


def sponsor(**kw) -> dict:
    """A private sponsor row as the CLI hands it to the panel."""
    row = {
        "ein": "111111111",
        "sponsor_name": "OLD MACHINE SHOP INC",
        "state": "TX",
        "naics": "332710",
        "plans": 1,
        "participants_max": 115,
        "participants_sum": 115,
        "is_dfe": False,
        "trend": "flat",
        "status": "filing",
        "pct_change": -0.04,
        "first_year": 2022,
        "last_year": 2024,
        "years_filed": 3,
        "pending_years": 0,
        "gap_years": 0,
        "series": [{"year": 2022, "participants": 120},
                   {"year": 2023, "participants": 118},
                   {"year": 2024, "participants": 115}],
    }
    row.update(kw)
    return row


STATS = {"plan_year": 2024, "sponsors": 4, "private": 3, "dfe": 1,
         "listed": 0, "ambiguous": 0, "completeness": ""}


# --- the trend cell -----------------------------------------------------


def test_a_lapsed_sponsor_reads_as_lapsed_not_as_a_fall_to_zero() -> None:
    """The single most important cell in either panel. A sponsor that stopped
    filing may have terminated, been acquired, changed EIN or dropped below
    the threshold -- and if the cell showed a direction, the one it would show
    is a crash to nothing."""
    cell = panels.trend_html(sponsor(status="lapsed", last_year=2022,
                                     trend="unknown"))
    assert "lapsed" in cell
    assert "2022" in cell
    for forbidden in ("declining", "-100%", "0%"):
        assert forbidden not in cell


def test_a_pending_year_is_labelled_as_outstanding() -> None:
    """Filings lag the plan year by about eighteen months, so the newest year
    is thin for everyone and an absence there means nothing."""
    cell = panels.trend_html(sponsor(pending_years=1))
    assert "pending" in cell
    assert "lapsed" not in cell


def test_a_gap_year_is_shown_apart_from_a_pending_one() -> None:
    """A complete year the sponsor skipped is an oddity worth seeing; a year
    still being filed is not. Same absence, different meanings."""
    cell = panels.trend_html(sponsor(gap_years=1))
    assert "gap" in cell
    assert "pending" not in cell


def test_the_trend_carries_a_glyph_and_a_word_not_only_a_colour() -> None:
    """Status rule: colour never carries the meaning on its own."""
    for trend, word in (("growing", "growing"), ("declining", "declining"),
                        ("flat", "flat")):
        cell = panels.trend_html(sponsor(trend=trend))
        assert word in cell
        assert f"tr-{trend}" in cell
        # and a direction glyph beside it
        assert any(g in cell for g in ("↑", "→", "↓"))


def test_one_filed_year_says_so_rather_than_showing_flat() -> None:
    cell = panels.trend_html(sponsor(trend="unknown", years_filed=1,
                                     pct_change=None, first_year=2024))
    assert "one year" in cell
    assert "flat" not in cell


# --- the sparkline ------------------------------------------------------


def test_the_sparkline_draws_a_missing_year_as_a_gap_not_a_zero() -> None:
    """A zero-height bar and a missing year look identical and mean opposite
    things."""
    bars = panels.series_bars(sponsor(series=[
        {"year": 2022, "participants": 120},
        {"year": 2024, "participants": 115},
    ]))
    assert 'class="sp gap"' in bars
    assert "no filing for 2023" in bars


def test_a_single_year_has_no_sparkline() -> None:
    assert "&mdash;" in panels.series_bars(
        sponsor(series=[{"year": 2024, "participants": 115}]))


# --- U8 -----------------------------------------------------------------


def test_the_private_panel_has_a_trend_column() -> None:
    page = panels.private_html([sponsor()], STATS)
    assert "<th data-s=\"t\">trend</th>" in page
    assert "flat" in page
    assert 'class="spark"' in page


def test_the_private_panel_says_the_trend_skips_partial_years() -> None:
    """A reader who does not know filings lag the plan year will read the
    newest year's thinness as a collapse."""
    page = panels.private_html([sponsor()], STATS)
    assert "only between plan years" in page
    assert "eighteen months" in page


def test_the_private_panel_still_shows_headcount_as_a_range() -> None:
    page = panels.private_html(
        [sponsor(participants_max=80, participants_sum=140)], STATS)
    assert "80&ndash;140" in page


# --- the mature-target panel --------------------------------------------


def target(**kw) -> dict:
    row = {
        "ein": "111111111",
        "sponsor_name": "OLD MACHINE SHOP INC",
        "naics": "332710",
        "city": "AUSTIN",
        "state": "TX",
        "oldest_plan_eff": date(1979, 1, 1),
        "age_years": 47.7,
        "participants_last": 148,
        "participants_sum": 148,
        "active_last": 115,
        "active_sum": 115,
        "trend": "flat",
        "status": "filing",
        "pct_change": -0.04,
        "first_year": 2022,
        "last_year": 2024,
        "years_filed": 3,
        "pending_years": 0,
        "gap_years": 0,
        "series": [{"year": 2022, "participants": 120},
                   {"year": 2023, "participants": 118},
                   {"year": 2024, "participants": 115}],
    }
    row.update(kw)
    return row


def test_the_age_column_is_shown_as_a_floor() -> None:
    """A company founded in 1971 whose plan started in 1985 reads as 1985.
    Printed as a bare number it is an incorporation date that is wrong."""
    page = panels.mature_html([target()], {})
    assert "&ge;48y" in page
    assert "Age is a floor" in page
    assert "at least this old" in page


def test_the_panel_states_that_a_lapse_is_not_a_decline() -> None:
    page = panels.mature_html([target()], {})
    assert "lapse is not a decline" in page
    assert "excluded rather than ranked" in page


def test_the_population_is_reported_beside_the_candidates() -> None:
    """A short list is either selective or broken, and the funnel is the
    difference."""
    page = panels.mature_html([target()], {
        "candidates": 1,
        "population": {"sponsors": 800000, "private": 750000,
                       "not_dfe": 745000, "still_filing": 500000},
    })
    assert "800,000" in page
    assert "still filing 500,000" in page


def test_an_empty_panel_says_which_command_fills_it() -> None:
    page = panels.mature_html([], {})
    assert "mr targets" in page
    assert "mr form5500" in page


def test_the_mature_panel_reaches_the_shell_and_stays_self_contained() -> None:
    ctx = shell.Context(generated_at=datetime.now(timezone.utc), postgres=True)
    page = shell.render(ctx, mature=[target()],
                        mature_stats={"years": (2022, 2023, 2024),
                                      "candidates": 1, "population": {}})
    assert 'id="mature-rows"' in page
    for forbidden in ("http://", "https://", "src=", "<link", "fetch("):
        assert forbidden not in page


def test_the_panel_waits_rather_than_lying_when_one_year_is_loaded() -> None:
    """One plan year cannot produce a trend, and the panel has to say that
    rather than render an empty table that reads as 'no targets exist'."""
    ctx = shell.Context(generated_at=datetime.now(timezone.utc), postgres=True)
    shell.render(ctx, mature=[], mature_stats={"years": (2024,)})
    panel = next(p for p in shell.PANELS if p.id == "mature")
    state, detail = panel.resolve(ctx)
    assert state == shell.WAITING
    assert "two complete years" in detail


def test_the_mature_panel_shows_the_measure_the_screen_filtered_on() -> None:
    """The screen bands and orders on active participants. Rendering the
    total beside it put 474 in a column whose band is 20-1,000 and whose CLI
    row said 96 -- one sponsor, two measures, nothing saying which."""
    page = panels.mature_html([target()], {})
    assert ">115<" in page, "the active count, not the 148 total"
    assert "148" not in page
