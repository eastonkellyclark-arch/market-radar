"""The participant time series, and the absences it must not misread.

Every test here exists because the same mistake is available in four
different disguises: treating "the sponsor is not in this year's file" as
"the sponsor shrank". A sponsor can be missing because it terminated the
plan, because it was acquired, because its EIN changed, because it dropped
below the filing threshold, or -- for the newest year, in the overwhelming
majority of cases -- because filings lag the plan year by about eighteen
months and it simply has not filed yet.

None of those is a headcount decline, and the mature-target screen ranks on
headcount decline. So the series keeps presence and trend in separate columns
and these tests hold them apart.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb
import pytest

from marketradar.screens import mature_target as mt
from marketradar.sources import form5500 as f5

from test_form5500 import filers, main_row, make_zip


def year_parquet(tmp: Path, year: int, rows: list[dict],
                 sec: list[tuple] = ()) -> Path:
    """One plan year, taken all the way to a published sponsor parquet.

    Deliberately goes through the real loader and ``published_sql`` rather
    than hand-writing a parquet: the series reads published files, so a test
    that wrote its own shape would pass while the two drifted apart.
    """
    con = duckdb.connect()
    work = tmp / f"y{year}"
    work.mkdir(parents=True, exist_ok=True)
    archive = make_zip(work, "main", year, rows)
    f5.load_filings(con, [archive], tmp / f"work{year}")
    f5.build_sponsors(con)
    f5.resolve(con, filers(con, list(sec)))
    dest = tmp / f"form5500_sponsors_{year}.parquet"
    con.execute(
        f"copy ({f5.published_sql()}) to '{dest.as_posix()}' (format parquet)")
    con.close()
    return dest


def series(tmp: Path, years: dict[int, list[dict]]) -> duckdb.DuckDBPyConnection:
    """Build history + trend over several plan years."""
    paths = {y: year_parquet(tmp, y, rows) for y, rows in years.items()}
    con = duckdb.connect()
    f5.build_history(con, paths)
    f5.build_trend(con)
    return con


def trend_row(con: duckdb.DuckDBPyConnection, ein: str) -> dict:
    cur = con.execute(f"select * from f5500_trend where ein = '{ein}'")
    row = cur.fetchone()
    if row is None:
        return {}
    return dict(zip([d[0] for d in cur.description], row))


# --- the effective date -------------------------------------------------


def test_the_oldest_plan_is_a_floor_on_entity_age(con, tmp_path) -> None:
    """A sponsor's oldest plan bounds how long the company has existed. It is
    never the company's age -- the plan can only be younger than the firm."""
    archives = [make_zip(tmp_path, "main", 2024, [
        main_row("OLD CO", "111111111", eff="1985-04-01"),
        main_row("OLD CO", "111111111", eff="2011-01-01"),
    ])]
    f5.load_filings(con, archives, tmp_path / "work")
    f5.build_sponsors(con)
    got = con.execute(
        "select oldest_plan_eff from f5500_sponsors").fetchone()[0]
    assert got == date(1985, 4, 1)


def test_an_impossible_effective_date_is_dropped_not_carried(
    con, tmp_path
) -> None:
    """The column runs 1876-11-11 to 2027-08-01 in the real 2024 file. ERISA
    is from 1974, so the early end is a typo and the late end is in the
    future; a floor on entity age built from either is fiction."""
    archives = [make_zip(tmp_path, "main", 2024, [
        main_row("TYPO CO", "111111111", eff="1876-11-11"),
        main_row("TYPO CO", "111111111", eff="1992-06-15"),
        main_row("FUTURE CO", "222222222", eff="2027-08-01"),
    ])]
    f5.load_filings(con, archives, tmp_path / "work")
    f5.build_sponsors(con)
    got = dict(con.execute(
        "select sponsor_name, oldest_plan_eff from f5500_sponsors").fetchall())
    assert got["TYPO CO"] == date(1992, 6, 15), "the 1876 date is not a floor"
    assert got["FUTURE CO"] is None, "a plan cannot take effect after its year"


def test_a_sponsor_with_only_bad_dates_has_no_age_rather_than_a_wrong_one(
    con, tmp_path
) -> None:
    archives = [make_zip(tmp_path, "main", 2024,
                         [main_row("ONLY TYPOS", "111111111", eff="1801-01-01")])]
    f5.load_filings(con, archives, tmp_path / "work")
    f5.build_sponsors(con)
    assert con.execute(
        "select oldest_plan_eff from f5500_sponsors").fetchone()[0] is None


# --- a missing column is a message, not a BinderException ---------------


def test_a_renamed_dol_column_fails_with_the_column_name(con, tmp_path) -> None:
    """DOL changes the schema between plan years and we load four of them. The
    failure has to name the file, the year and the column -- a DuckDB binder
    error naming a generated SELECT does not."""
    import csv
    import zipfile

    member = "f_5500_2022_latest.csv"
    csv_path = tmp_path / member
    cols = [c for c in ("SPONSOR_DFE_NAME", "SPONS_DFE_EIN", "BUSINESS_CODE",
                        "SPONS_DFE_MAIL_US_CITY", "SPONS_DFE_MAIL_US_STATE",
                        "SPONS_DFE_MAIL_US_ZIP", "PLAN_NAME",
                        "TOT_PARTCP_BOY_CNT", "TYPE_DFE_PLAN_ENTITY_CD",
                        "SPONS_DFE_DBA_NAME")]
    # Every mapped column except PLAN_EFF_DATE, which DOL is imagined to have
    # renamed between plan years.
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols + ["PLAN_EFFECTIVE_DATE"])
        w.writeheader()
        w.writerow({c: "" for c in cols} | {"PLAN_EFFECTIVE_DATE": "1990-01-01"})
    zip_path = tmp_path / "F_5500_2022_Latest.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        z.write(csv_path, member)
    csv_path.unlink()

    with pytest.raises(f5.Form5500Error) as exc:
        f5.load_filings(con, [f5.Archive("main", 2022, zip_path)],
                        tmp_path / "work")
    message = str(exc.value)
    assert "PLAN_EFF_DATE" in message
    assert "2022" in message
    # And it points at the near-match, so the fix is one edit rather than a
    # diff of a several-hundred-column header.
    assert "PLAN_EFFECTIVE_DATE" in message


# --- absence is not decline ---------------------------------------------


def test_a_sponsor_that_stopped_filing_is_lapsed_not_declining(tmp_path) -> None:
    """The failure this whole module is shaped around. BETA files 2022 and
    then vanishes; a series that treated the absence as a data point would
    read it as a fall to zero and rank it first in a decline screen."""
    con = series(tmp_path, {
        2022: [main_row("ALPHA INC", "111111111", participants="100"),
               main_row("BETA LLC", "222222222", participants="40")],
        2023: [main_row("ALPHA INC", "111111111", participants="98")],
        2024: [main_row("ALPHA INC", "111111111", participants="96")],
    })
    beta = trend_row(con, "222222222")
    assert beta["status"] == f5.STATUS_LAPSED
    assert beta["trend"] == f5.TREND_UNKNOWN, "one year is not a trend"
    assert beta["pct_change"] is None
    assert beta["participants_last"] == 40, "never zero-filled"
    assert beta["years_filed"] == 1


def test_absence_from_a_year_still_being_filed_is_not_a_lapse(tmp_path) -> None:
    """2025 held a third of 2024's filings while it was still being filed.
    Every sponsor missing from it would otherwise read as having stopped."""
    con = series(tmp_path, {
        2023: [main_row("ALPHA INC", "111111111", participants="100"),
               main_row("EARLY FILER", "888888888")],
        2024: [main_row("ALPHA INC", "111111111", participants="100"),
               main_row("EARLY FILER", "888888888")],
        # ALPHA has not filed for 2025 yet. EARLY FILER has.
        2025: [main_row("EARLY FILER", "888888888")],
    })
    row = trend_row(con, "111111111")
    assert row["status"] == f5.STATUS_FILING
    assert row["pending_years"] == 1, "2025 is outstanding, not missed"
    assert row["gap_years"] == 0


def test_a_partial_year_never_contributes_to_the_trend(tmp_path) -> None:
    """A sponsor's 2025 filing is real but its 2025 *population* is not, and
    the trend is a statement about the population. Measuring into a year that
    is a third filed compares a sponsor against an incomplete cohort."""
    con = series(tmp_path, {
        2023: [main_row("ALPHA INC", "111111111", participants="100")],
        2024: [main_row("ALPHA INC", "111111111", participants="100")],
        2025: [main_row("ALPHA INC", "111111111", participants="10")],
    })
    row = trend_row(con, "111111111")
    assert row["last_year"] == 2024, "the trend stops at the newest complete year"
    assert row["participants_last"] == 100
    assert row["trend"] == f5.TREND_FLAT, "the partial year's 10 is not a crash"


def test_a_sponsor_seen_only_in_a_partial_year_is_counted_not_dropped(
    tmp_path
) -> None:
    """It has no complete year, so it has no series -- but a caller comparing
    the history to the trend would otherwise find rows missing and no reason."""
    paths = {
        2024: year_parquet(tmp_path, 2024,
                           [main_row("ALPHA INC", "111111111")]),
        2025: year_parquet(tmp_path, 2025, [
            main_row("ALPHA INC", "111111111"),
            main_row("BRAND NEW LLC", "999999999"),
        ]),
    }
    con = duckdb.connect()
    f5.build_history(con, paths)
    built = f5.build_trend(con)
    assert built.sponsors == 1
    assert built.only_partial == 1
    assert built.partial_years == (2025,)
    assert "no series yet" in str(built)


def test_a_skipped_complete_year_is_a_gap_not_a_lapse(tmp_path) -> None:
    con = series(tmp_path, {
        2022: [main_row("ALPHA INC", "111111111", participants="100"),
               main_row("STEADY CO", "888888888")],
        2023: [main_row("STEADY CO", "888888888")],
        2024: [main_row("ALPHA INC", "111111111", participants="100"),
               main_row("STEADY CO", "888888888")],
    })
    row = trend_row(con, "111111111")
    assert row["gap_years"] == 1
    assert row["status"] == f5.STATUS_FILING
    assert row["years_filed"] == 2


def test_a_missing_plan_year_does_not_invent_a_gap_for_everyone(
    tmp_path
) -> None:
    """Loading 2022 and 2024 without 2023 is a hole in *our* data, not in the
    sponsor's filing history, and counting it against the span would mark
    every sponsor in the file as having skipped a year."""
    con = series(tmp_path, {
        2022: [main_row("ALPHA INC", "111111111", participants="100")],
        2024: [main_row("ALPHA INC", "111111111", participants="100")],
    })
    assert trend_row(con, "111111111")["gap_years"] == 0


# --- the trend itself ---------------------------------------------------


@pytest.mark.parametrize("counts,expected", [
    (("100", "98", "97"), f5.TREND_FLAT),
    (("100", "90", "70"), f5.TREND_DECLINING),
    (("100", "130", "180"), f5.TREND_GROWING),
])
def test_the_trend_reads_the_filed_years(tmp_path, counts, expected) -> None:
    con = series(tmp_path, {
        year: [main_row("ALPHA INC", "111111111", participants=c)]
        for year, c in zip((2022, 2023, 2024), counts)
    })
    assert trend_row(con, "111111111")["trend"] == expected


def test_one_filed_year_has_no_trend_rather_than_a_flat_one(tmp_path) -> None:
    """'unknown' is a real answer and the most common one. A single year read
    as flat would put every one-year sponsor into the mature-target screen."""
    con = series(tmp_path, {
        2024: [main_row("ALPHA INC", "111111111", participants="100")],
    })
    assert trend_row(con, "111111111")["trend"] == f5.TREND_UNKNOWN


def test_a_plan_being_added_is_not_hiring(tmp_path) -> None:
    """A sponsor that opens a second 401(k) doubles the sum across plans
    without employing one more person. The trend compares only the plan both
    years carry, and reports the new one as a count instead."""
    con = series(tmp_path, {
        2023: [main_row("ALPHA INC", "111111111", participants="100",
                        pn="001")],
        2024: [main_row("ALPHA INC", "111111111", participants="100",
                        pn="001"),
               main_row("ALPHA INC", "111111111", participants="95",
                        pn="501")],
    })
    row = trend_row(con, "111111111")
    assert row["participants_sum"] == 195, "the sum did double-count"
    assert row["common_plans"] == 1
    assert row["plans_added"] == 1
    assert row["matched_first"] == 100 and row["matched_last"] == 100
    assert row["trend"] == f5.TREND_FLAT, "and the trend did not follow it"


def test_a_plan_that_is_not_in_the_later_file_is_not_a_redundancy(
    tmp_path
) -> None:
    """The defect this whole comparison exists for, measured on the real
    file: Edward Don & Company filed two plans for 2022 and one for 2024 and
    read as -46%. A plan missing from a year is a plan missing from a year --
    terminated, merged, or filed late -- and none of those is the sponsor
    employing half as many people."""
    con = series(tmp_path, {
        2023: [main_row("EDWARD DON", "111111111", participants="800",
                        pn="001"),
               main_row("EDWARD DON", "111111111", participants="760",
                        pn="501")],
        2024: [main_row("EDWARD DON", "111111111", participants="790",
                        pn="001")],
    })
    row = trend_row(con, "111111111")
    assert row["plans_dropped"] == 1
    assert row["common_plans"] == 1
    # 800 -> 790 on the plan they share, not 1,560 -> 790.
    assert row["matched_first"] == 800 and row["matched_last"] == 790
    assert row["trend"] == f5.TREND_FLAT
    assert row["pct_change"] == pytest.approx(-0.0125, abs=1e-4)


def test_a_sponsor_whose_plans_all_changed_has_no_comparable_pair(
    tmp_path
) -> None:
    """No plan in common means no like-for-like comparison exists. Saying
    'unknown' is the honest answer; comparing the totals anyway is the bug."""
    con = series(tmp_path, {
        2023: [main_row("ALPHA INC", "111111111", participants="100",
                        pn="001")],
        2024: [main_row("ALPHA INC", "111111111", participants="40",
                        pn="002")],
    })
    row = trend_row(con, "111111111")
    assert row["common_plans"] == 0
    assert row["trend"] == f5.TREND_UNKNOWN
    assert row["pct_change"] is None


def test_the_largest_single_plan_can_move_without_the_headcount_moving(
    tmp_path
) -> None:
    """The Juilliard School's largest plan went 1,473 -> 500 -> 981 across
    three years while its total barely moved: with six plans, *which* one is
    largest keeps changing. A trend read off the max alone called that -33%."""
    con = series(tmp_path, {
        2022: [main_row("JUILLIARD", "111111111", participants="1473",
                        pn="001"),
               main_row("JUILLIARD", "111111111", participants="500",
                        pn="002")],
        2024: [main_row("JUILLIARD", "111111111", participants="500",
                        pn="001"),
               main_row("JUILLIARD", "111111111", participants="1450",
                        pn="002")],
    })
    row = trend_row(con, "111111111")
    assert row["matched_first"] == 1973 and row["matched_last"] == 1950
    assert row["trend"] == f5.TREND_FLAT, (
        "the two plans swapped sizes; nobody left")


def test_the_display_name_is_the_same_across_years(tmp_path) -> None:
    """The same min() rule as build_sponsors, for the same reason: one EIN
    carries several spellings, and picking one per run made the review-queue
    count drift between identical loads."""
    years = {
        2023: [main_row("ALPHA, INC.", "111111111"),
               main_row("ALPHA INCORPORATED", "111111111")],
        2024: [main_row("ALPHA INC", "111111111")],
    }
    seen = set()
    for attempt in range(3):
        con = series(tmp_path / f"run{attempt}", years)
        seen.add(trend_row(con, "111111111")["sponsor_name"])
        con.close()
    assert seen == {"ALPHA INC"}, (
        f"the display name is not the same every run: {seen}")


def test_two_builds_of_the_same_years_agree_exactly(tmp_path) -> None:
    """Identical input, identical output -- counts included. The defect this
    guards is not a duplicate write; it is the same load reporting 22,680
    then 22,685 rows while every idempotency test still passed."""
    years = {
        2023: [main_row("ALPHA INC", "111111111", participants="100"),
               main_row("ALPHA CO", "111111111", participants="80"),
               main_row("BETA LLC", "222222222", participants="30")],
        2024: [main_row("ALPHA INCORPORATED", "111111111", participants="90"),
               main_row("BETA LLC", "222222222", participants="30")],
    }
    paths = {y: year_parquet(tmp_path, y, rows) for y, rows in years.items()}

    seen = []
    for _ in range(3):
        con = duckdb.connect()
        f5.build_history(con, paths)
        built = f5.build_trend(con)
        seen.append((
            built,
            con.execute("select ein, sponsor_name, matched_first, "
                        "matched_last, participants_last, trend, status, "
                        "common_plans, series "
                        "from f5500_trend order by ein").fetchall(),
        ))
        con.close()
    assert seen[0] == seen[1] == seen[2]


def test_the_same_plan_year_loaded_twice_is_an_error(tmp_path) -> None:
    """Two files carrying one plan year would double every delta below."""
    path = year_parquet(tmp_path, 2024, [main_row("ALPHA INC", "111111111")])
    # A file in the cache named for one plan year while holding another --
    # the shape a bad download or a hand-copied file actually takes.
    misnamed = tmp_path / "form5500_sponsors_2023.parquet"
    misnamed.write_bytes(path.read_bytes())
    con = duckdb.connect()
    with pytest.raises(f5.Form5500Error, match="appear twice"):
        f5.build_history(con, {2023: misnamed, 2024: path})


def test_a_parquet_from_an_older_loader_says_to_republish(tmp_path) -> None:
    """oldest_plan_eff was added after the first published files. A join that
    silently skipped those years would produce a shorter series and no
    complaint."""
    path = year_parquet(tmp_path, 2024, [main_row("ALPHA INC", "111111111")])
    stale = tmp_path / "stale.parquet"
    con = duckdb.connect()
    con.execute(f"""
        copy (select * exclude (oldest_plan_eff)
              from read_parquet('{path.as_posix()}'))
        to '{stale.as_posix()}' (format parquet)
    """)
    with pytest.raises(f5.Form5500Error, match="oldest_plan_eff"):
        f5.build_history(con, {2024: stale})


def test_a_year_with_no_complete_data_refuses_to_trend(tmp_path) -> None:
    con = duckdb.connect()
    f5.build_history(con, {
        2025: year_parquet(tmp_path, 2025, [main_row("ALPHA INC", "111111111")]),
    })
    with pytest.raises(f5.Form5500Error, match="complete"):
        f5.build_trend(con)


def test_series_shape_marks_the_partial_year(tmp_path) -> None:
    con = series(tmp_path, {
        2024: [main_row("ALPHA INC", "111111111")],
        2025: [main_row("ALPHA INC", "111111111")],
    })
    shape = {s.plan_year: s for s in f5.series_shape(con)}
    assert shape[2024].complete is True
    assert shape[2025].complete is False
    assert shape[2025].note == "still being filed"


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    return duckdb.connect()


# --- participants are not employees -------------------------------------


def test_retirees_cashing_out_are_not_a_shrinking_workforce(tmp_path) -> None:
    """The defect that shaped the first version of the mature-target screen.

    ``TOT_PARTCP_BOY_CNT`` counts everyone with a balance -- retirees and
    separated ex-employees included. An old employer's plan sheds those every
    year whether or not it employs one fewer person, so a trend read off the
    total ranks universities, hospitals and charities as collapsing. Measured
    on the real file: Boca Raton Regional Hospital reports 934 participants
    and 388 active; J M Smith 890 and 362.
    """
    con = series(tmp_path, {
        2023: [main_row("OLD HOSPITAL", "111111111",
                        participants="1700", active="390")],
        2024: [main_row("OLD HOSPITAL", "111111111",
                        participants="934", active="388")],
    })
    row = trend_row(con, "111111111")
    # The total halved. The staff did not.
    assert row["participants_last"] == 934
    assert row["matched_first"] == 390 and row["matched_last"] == 388
    assert row["trend"] == f5.TREND_FLAT, (
        "a 45% fall in total participants with flat active staff is a plan "
        "paying people out, not an employer shrinking")


def test_a_plan_with_no_active_count_is_left_out_of_the_comparison(
    tmp_path
) -> None:
    """Active is missing on 4.8% of main-form filings. Coalescing that to
    zero would read as the plan's entire workforce being laid off."""
    con = series(tmp_path, {
        2023: [main_row("ALPHA INC", "111111111", participants="100",
                        active="90", pn="001"),
               main_row("ALPHA INC", "111111111", participants="60",
                        active="", pn="501")],
        2024: [main_row("ALPHA INC", "111111111", participants="100",
                        active="88", pn="001"),
               main_row("ALPHA INC", "111111111", participants="60",
                        active="", pn="501")],
    })
    row = trend_row(con, "111111111")
    assert row["common_plans"] == 1, "the unreportable plan is not compared"
    assert row["matched_first"] == 90 and row["matched_last"] == 88
    assert row["trend"] == f5.TREND_FLAT


def test_the_floor_date_itself_is_a_placeholder_not_a_date(tmp_path) -> None:
    """1900-01-01 is carried by 10 sponsors and is not a date any of them
    started a plan on. Once the screen sorted by age it took the top three
    slots -- one of them an LLC, a form that did not exist in 1900."""
    con = duckdb.connect()
    archives = [make_zip(tmp_path, "main", 2024, [
        main_row("SHOALS MPE, LLC", "111111111", eff="1900-01-01"),
        main_row("REAL OLD CO", "222222222", eff="1900-11-10"),
    ])]
    f5.load_filings(con, archives, tmp_path / "work")
    f5.build_sponsors(con)
    got = dict(con.execute(
        "select sponsor_name, oldest_plan_eff from f5500_sponsors").fetchall())
    assert got["SHOALS MPE, LLC"] is None
    assert got["REAL OLD CO"] == date(1900, 11, 10), "a real date near the floor"


def test_a_january_first_plan_date_is_kept(tmp_path) -> None:
    """A plan year almost always begins on 1 January, so it is the single most
    common legitimate effective date. Filtering round dates as placeholders
    would discard 116 real ones to remove 10 fake."""
    con = duckdb.connect()
    archives = [make_zip(tmp_path, "main", 2024,
                         [main_row("NORMAL CO", "111111111", eff="1975-01-01")])]
    f5.load_filings(con, archives, tmp_path / "work")
    f5.build_sponsors(con)
    assert con.execute(
        "select oldest_plan_eff from f5500_sponsors").fetchone()[0] == date(1975, 1, 1)
