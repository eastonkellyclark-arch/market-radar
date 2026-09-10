"""The mature-target screen: old, sized, still filing, not growing.

The screen's whole risk is that three of its four inputs are bounds rather
than measurements -- age is a floor, headcount is a range, and an absence is
not a decline -- so most of these tests are about it refusing to treat a
bound as a fact.
"""

from __future__ import annotations

from datetime import date

import pytest

from marketradar.screens import mature_target as mt
from marketradar.sources import form5500 as f5

from test_form5500 import main_row
from test_form5500_trend import series

#: Fixed so the age arithmetic in the assertions does not drift with the
#: calendar. Every test that cares about age passes this explicitly.
AS_OF = date(2026, 9, 10)


def steady(name, ein, participants="100", eff="1980-01-01"):
    """The same sponsor across 2022-2024, unchanged: old, flat, in-band."""
    return {
        year: [main_row(name, ein, participants=participants, eff=eff)]
        for year in (2022, 2023, 2024)
    }


def names(targets) -> list[str]:
    return [t.sponsor_name for t in targets]


# --- what qualifies -----------------------------------------------------


def test_an_old_flat_private_sponsor_is_a_candidate(tmp_path) -> None:
    con = series(tmp_path, steady("OLD MACHINE SHOP INC", "111111111"))
    got = mt.candidates(con, as_of=AS_OF)
    assert names(got) == ["OLD MACHINE SHOP INC"]
    assert got[0].trend == f5.TREND_FLAT
    assert got[0].age_years == pytest.approx(46.7, abs=0.2)


def test_the_age_is_reported_as_a_floor_not_an_age(tmp_path) -> None:
    """A company founded in 1971 whose 401(k) started in 1985 reads as 1985.
    The column can only understate, and the label has to say so -- otherwise
    it is an incorporation date that happens to be wrong."""
    con = series(tmp_path, steady("OLD CO", "111111111", eff="1985-01-01"))
    note = mt.candidates(con, as_of=AS_OF)[0].age_note
    assert note.startswith("at least ")
    assert "1985-01-01" in note


def test_headcount_is_shown_as_a_range_when_the_plans_overlap(tmp_path) -> None:
    """A sponsor with two plans counts the same people twice, so the sum is an
    upper bound and the largest single plan a lower one."""
    con = series(tmp_path, {
        year: [main_row("TWO PLAN CO", "111111111", participants="80",
                        eff="1980-01-01", pn="001"),
               main_row("TWO PLAN CO", "111111111", participants="60",
                        eff="1990-01-01", pn="501")]
        for year in (2022, 2023, 2024)
    })
    target = mt.candidates(con, as_of=AS_OF)[0]
    assert target.participants_last == 80
    assert target.participants_sum == 140
    assert target.headcount_note == "80–140"


def test_the_filter_reads_the_lower_bound_of_the_headcount(tmp_path) -> None:
    """Two plans of 15 sum to 30 and would clear a floor of 20 on the sum.
    Filtering on the sum admits a sponsor that may employ fifteen people."""
    con = series(tmp_path, {
        year: [main_row("SMALL CO", "111111111", participants="15",
                        eff="1980-01-01", pn="001"),
               main_row("SMALL CO", "111111111", participants="15",
                        eff="1985-01-01", pn="501")]
        for year in (2022, 2023, 2024)
    })
    assert mt.candidates(con, as_of=AS_OF, min_participants=20) == []


# --- what is excluded, and why ------------------------------------------


def test_a_lapsed_sponsor_is_never_a_candidate(tmp_path) -> None:
    """The one exclusion the screen cannot afford to get wrong. A sponsor that
    stopped filing may have terminated, been acquired, or changed EIN -- and
    read as a decline it would rank *first* in a screen for shrinking firms."""
    con = series(tmp_path, {
        2022: [main_row("GONE CO", "111111111", participants="200",
                        eff="1970-01-01"),
               main_row("STILL HERE", "222222222", participants="100",
                        eff="1980-01-01")],
        2023: [main_row("STILL HERE", "222222222", participants="100",
                        eff="1980-01-01")],
        2024: [main_row("STILL HERE", "222222222", participants="100",
                        eff="1980-01-01")],
    })
    got = names(mt.candidates(con, as_of=AS_OF))
    assert "GONE CO" not in got
    assert got == ["STILL HERE"]


def test_a_dfe_is_never_a_candidate(tmp_path) -> None:
    """A master trust is old, enormous and perfectly flat, which is to say it
    scores well on every axis while being a trustee rather than an employer."""
    con = series(tmp_path, {
        year: [main_row("BIG BANK MASTER TRUST", "111111111",
                        participants="900", eff="1960-01-01", dfe="M"),
               main_row("REAL EMPLOYER", "222222222", participants="100",
                        eff="1980-01-01")]
        for year in (2022, 2023, 2024)
    })
    assert names(mt.candidates(con, as_of=AS_OF)) == ["REAL EMPLOYER"]
    # Flagged rather than deleted, so asking for them explicitly still works.
    assert len(mt.candidates(con, as_of=AS_OF, include_dfe=True)) == 2


def test_a_growing_sponsor_is_excluded_rather_than_ranked_last(tmp_path) -> None:
    con = series(tmp_path, {
        2022: [main_row("GROWING CO", "111111111", participants="40",
                        eff="1980-01-01")],
        2023: [main_row("GROWING CO", "111111111", participants="70",
                        eff="1980-01-01")],
        2024: [main_row("GROWING CO", "111111111", participants="110",
                        eff="1980-01-01")],
    })
    assert mt.candidates(con, as_of=AS_OF) == []


def test_a_young_sponsor_is_excluded(tmp_path) -> None:
    con = series(tmp_path, steady("NEW SAAS CO", "111111111", eff="2019-01-01"))
    assert mt.candidates(con, as_of=AS_OF) == []


def test_a_sponsor_with_no_usable_plan_date_is_excluded(tmp_path) -> None:
    """Its age is unknown, not zero and not infinite. A screen that sorts on
    age has nothing to say about it, and guessing is how a 1876 typo becomes
    a 150-year-old company at the top of the list."""
    con = series(tmp_path, steady("NO DATE CO", "111111111", eff="1801-01-01"))
    assert mt.candidates(con, as_of=AS_OF) == []


def test_a_sponsor_that_resolves_to_an_sec_filer_is_excluded(tmp_path) -> None:
    """It is a public company we already track from the other end."""
    from test_form5500_trend import year_parquet

    sec = [("320193", "111111111", "COMPLETELY DIFFERENT NAME", "AAPL")]
    paths = {
        year: year_parquet(
            tmp_path, year,
            [main_row("PUBLIC CO", "111111111", participants="100",
                      eff="1980-01-01")],
            sec)
        for year in (2022, 2023, 2024)
    }
    import duckdb
    con = duckdb.connect()
    f5.build_history(con, paths)
    f5.build_trend(con)
    assert mt.candidates(con, as_of=AS_OF) == []


def test_a_name_match_with_a_disagreeing_ein_is_excluded(tmp_path) -> None:
    """It is a question in the review queue, not a private company. Counting
    it as private is the 44.2%-precision mistake in a different costume."""
    from test_form5500_trend import year_parquet

    import duckdb

    sec = [("320193", "999999999", "ACME INC", "ACME")]
    paths = {
        year: year_parquet(
            tmp_path, year,
            [main_row("ACME INC", "111111111", participants="100",
                      eff="1980-01-01")],
            sec)
        for year in (2022, 2023, 2024)
    }
    con = duckdb.connect()
    f5.build_history(con, paths)
    f5.build_trend(con)
    assert mt.candidates(con, as_of=AS_OF) == []


def test_a_sponsor_with_one_filed_year_has_no_trend_and_is_excluded(
    tmp_path
) -> None:
    con = series(tmp_path, {
        2023: [main_row("ONE YEAR CO", "111111111", participants="100",
                        eff="1980-01-01"),
               main_row("STEADY CO", "222222222", participants="100",
                        eff="1980-01-01")],
        2024: [main_row("STEADY CO", "222222222", participants="100",
                        eff="1980-01-01")],
    })
    assert names(mt.candidates(con, as_of=AS_OF)) == ["STEADY CO"]


# --- ordering and reporting ---------------------------------------------


def test_the_list_is_ordered_by_age_floor_by_default(tmp_path) -> None:
    """The one input whose direction is not a judgement call. Whether a
    steeper decline means a motivated seller or a broken business is a
    question about the thesis, so it is a sort option and not a weight."""
    con = series(tmp_path, {
        year: [main_row("OLDER CO", "111111111", participants="100",
                        eff="1960-01-01"),
               main_row("NEWER CO", "222222222", participants="100",
                        eff="1995-01-01")]
        for year in (2022, 2023, 2024)
    })
    assert names(mt.candidates(con, as_of=AS_OF)) == ["OLDER CO", "NEWER CO"]


def test_an_unknown_sort_is_refused_rather_than_ignored(tmp_path) -> None:
    con = series(tmp_path, steady("OLD CO", "111111111"))
    with pytest.raises(ValueError, match="unknown sort"):
        mt.candidates(con, as_of=AS_OF, sort="whatever")


def test_a_multiemployer_plan_is_never_a_candidate(tmp_path) -> None:
    """A Taft-Hartley board of trustees covers a whole trade in a region, so
    it is old, large, and declining wherever the building trades are -- which
    put five boards of trustees for tile layers, masons and carpenters into
    the first twelve candidates this screen ever produced. Same trap as the
    DFE flag, one category over."""
    con = series(tmp_path, {
        year: [main_row("BOARD OF TRUSTEES TILE LAYERS LOCAL 52", "111111111",
                        participants="900", eff="1953-01-01", entity="1"),
               main_row("REAL EMPLOYER", "222222222", participants="100",
                        eff="1980-01-01", entity="2")]
        for year in (2022, 2023, 2024)
    })
    assert names(mt.candidates(con, as_of=AS_OF)) == ["REAL EMPLOYER"]
    # Flagged, not dropped -- the same treatment DFEs get.
    assert len(mt.candidates(con, as_of=AS_OF,
                             include_multiemployer=True)) == 2


def test_a_declining_sponsor_sorts_first_when_asked(tmp_path) -> None:
    con = series(tmp_path, {
        2022: [main_row("FLAT CO", "111111111", participants="100",
                        eff="1980-01-01"),
               main_row("DECLINING CO", "222222222", participants="100",
                        eff="1980-01-01")],
        2023: [main_row("FLAT CO", "111111111", participants="100",
                        eff="1980-01-01"),
               main_row("DECLINING CO", "222222222", participants="88",
                        eff="1980-01-01")],
        2024: [main_row("FLAT CO", "111111111", participants="100",
                        eff="1980-01-01"),
               main_row("DECLINING CO", "222222222", participants="80",
                        eff="1980-01-01")],
    })
    assert names(mt.candidates(con, as_of=AS_OF,
                               sort="decline"))[0] == "DECLINING CO"


def test_the_order_is_stable_across_runs(tmp_path) -> None:
    """Same input, same list, same order -- ties included. A screen whose
    order moves between runs cannot be reviewed from the top down."""
    con = series(tmp_path, {
        year: [main_row(f"CO {i}", f"{i}" * 9, participants="100",
                        eff="1980-01-01")
               for i in range(1, 6)]
        for year in (2022, 2023, 2024)
    })
    runs = [[t.ein for t in mt.candidates(con, as_of=AS_OF)] for _ in range(3)]
    assert runs[0] == runs[1] == runs[2]
    assert len(runs[0]) == 5


def test_population_reports_what_each_filter_removed(tmp_path) -> None:
    """A short list is either selective or broken, and these counts are the
    difference."""
    con = series(tmp_path, {
        year: [main_row("REAL EMPLOYER", "111111111", participants="100",
                        eff="1980-01-01"),
               main_row("A MASTER TRUST", "222222222", participants="900",
                        eff="1960-01-01", dfe="M")]
        for year in (2022, 2023, 2024)
    })
    pop = mt.population(con)
    assert pop["sponsors"] == 2
    assert pop["private"] == 2
    assert pop["not_dfe"] == 1
    assert pop["still_filing"] == 1


def test_summarise_reports_the_oldest_plan_not_the_newest(tmp_path) -> None:
    con = series(tmp_path, {
        year: [main_row("OLDER CO", "111111111", participants="100",
                        eff="1972-01-01"),
               main_row("NEWER CO", "222222222", participants="100",
                        eff="1995-01-01")]
        for year in (2022, 2023, 2024)
    })
    got = mt.summarise(mt.candidates(con, as_of=AS_OF))
    assert got["candidates"] == 2
    assert got["oldest"] == date(1972, 1, 1)


# --- nonprofits: set aside, with the evidence on the row ----------------


def test_a_403b_sponsor_is_flagged_structurally(tmp_path) -> None:
    """Only a 501(c)(3) or a public school may sponsor a 403(b), so this tier
    is a fact about the sponsor rather than a guess about its sector."""
    con = series(tmp_path, {
        year: [main_row("WATERSIDE SCHOOL", "111111111", participants="100",
                        eff="1960-01-01", naics="611000", pension="2F2G2L3D"),
               main_row("MACHINE SHOP INC", "222222222", participants="100",
                        eff="1960-01-01", naics="332900", pension="2E2J2K")]
        for year in (2022, 2023, 2024)
    })
    assert names(mt.candidates(con, as_of=AS_OF)) == ["MACHINE SHOP INC"]
    only = mt.candidates(con, as_of=AS_OF, nonprofits="only")
    assert names(only) == ["WATERSIDE SCHOOL"]
    assert only[0].nonprofit_basis == f5.NONPROFIT_BOTH


def test_a_nonprofit_naics_alone_is_labelled_as_a_guess(tmp_path) -> None:
    """The sector tier also catches for-profit hospitals and trade schools,
    so it is carried as its own basis rather than merged with the certain
    one. The set-aside is inspectable; that is the whole point of a flag."""
    con = series(tmp_path, {
        year: [main_row("A HOSPITAL GROUP", "111111111", participants="100",
                        eff="1960-01-01", naics="622000", pension="2E2J2K")]
        for year in (2022, 2023, 2024)
    })
    assert mt.candidates(con, as_of=AS_OF) == []
    only = mt.candidates(con, as_of=AS_OF, nonprofits="only")
    assert only[0].nonprofit_basis == f5.NONPROFIT_NAICS_ONLY


def test_nonprofits_are_flagged_never_dropped(tmp_path) -> None:
    con = series(tmp_path, {
        year: [main_row("A COLLEGE", "111111111", participants="100",
                        eff="1960-01-01", naics="611000", pension="2L"),
               main_row("MACHINE SHOP INC", "222222222", participants="100",
                        eff="1960-01-01", naics="332900")]
        for year in (2022, 2023, 2024)
    })
    assert len(mt.candidates(con, as_of=AS_OF, nonprofits="include")) == 2
    pop = mt.population(con)
    assert pop["an_employer"] == 2
    assert pop["for_profit"] == 1
    assert pop["set_aside_both"] == 1, "counted, not silently subtracted"


def test_an_unknown_nonprofit_mode_is_refused(tmp_path) -> None:
    con = series(tmp_path, steady("OLD CO", "111111111"))
    with pytest.raises(ValueError, match="exclude/include/only"):
        mt.candidates(con, as_of=AS_OF, nonprofits="maybe")
