"""Form 5500 loading and EIN-first resolution. No network.

The measurement that preceded this module is the reason for most of these
assertions: EIN is on 100% of 1,023,597 filings, and normalized-name matching
scores 44.2% precision against EIN ground truth. So the tests are mostly
about names *not* being trusted, and about the counts the loader reports
being the counts it actually has.
"""

from __future__ import annotations

import csv
import zipfile
from pathlib import Path

import duckdb
import pytest

from marketradar.sources import form5500 as f5


MAIN_COLS = [
    "SPONSOR_DFE_NAME", "SPONS_DFE_DBA_NAME", "SPONS_DFE_EIN",
    "BUSINESS_CODE", "SPONS_DFE_MAIL_US_CITY", "SPONS_DFE_MAIL_US_STATE",
    "SPONS_DFE_MAIL_US_ZIP", "PLAN_NAME", "TOT_PARTCP_BOY_CNT",
    "TYPE_DFE_PLAN_ENTITY_CD", "PLAN_EFF_DATE", "SPONS_DFE_PN",
    "TOT_ACT_PARTCP_BOY_CNT", "TYPE_PLAN_ENTITY_CD",
    "TYPE_PENSION_BNFT_CODE",
]
SF_COLS = [
    "SF_SPONSOR_NAME", "SF_SPONSOR_DFE_DBA_NAME", "SF_SPONS_EIN",
    "SF_BUSINESS_CODE", "SF_SPONS_US_CITY", "SF_SPONS_US_STATE",
    "SF_SPONS_US_ZIP", "SF_PLAN_NAME", "SF_TOT_PARTCP_BOY_CNT",
    "SF_PLAN_EFF_DATE", "SF_PLAN_NUM",
    "SF_TOT_ACT_PARTCP_BOY_CNT", "SF_PLAN_ENTITY_CD",
    "SF_TYPE_PENSION_BNFT_CODE",
]


def make_zip(tmp: Path, kind: str, year: int, rows: list[dict]) -> f5.Archive:
    """A DOL-shaped zip with one CSV inside, as the real files are."""
    stem = "f_5500_sf" if kind == "short" else "f_5500"
    member = f"{stem}_{year}_latest.csv"
    cols = SF_COLS if kind == "short" else MAIN_COLS
    csv_path = tmp / member
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})
    zip_path = tmp / (f"F_5500_SF_{year}_Latest.zip" if kind == "short"
                      else f"F_5500_{year}_Latest.zip")
    with zipfile.ZipFile(zip_path, "w") as z:
        z.write(csv_path, member)
    csv_path.unlink()
    return f5.Archive(kind=kind, year=year, path=zip_path)


def main_row(name, ein, naics="541110", participants="50", dfe="",
             eff="1998-07-01", pn="001", active=None, entity="2",
             pension="2E2J2K", **kw):
    # Active defaults to the total: most tests are about something other than
    # the retiree overhang, and the ones that are about it say so.
    row = {
        "SPONSOR_DFE_NAME": name, "SPONS_DFE_EIN": ein,
        "BUSINESS_CODE": naics, "SPONS_DFE_MAIL_US_STATE": "TX",
        "TOT_PARTCP_BOY_CNT": participants,
        "TYPE_DFE_PLAN_ENTITY_CD": dfe, "PLAN_NAME": f"{name} 401(K)",
        "PLAN_EFF_DATE": eff, "SPONS_DFE_PN": pn,
        "TOT_ACT_PARTCP_BOY_CNT": participants if active is None else active,
        "TYPE_PLAN_ENTITY_CD": entity, "TYPE_PENSION_BNFT_CODE": pension,
    }
    row.update(kw)
    return row


def sf_row(name, ein, naics="621111", participants="12",
           eff="2011-01-01", pn="001", active=None, entity="2",
           pension="2E2J2K", **kw):
    row = {
        "SF_SPONSOR_NAME": name, "SF_SPONS_EIN": ein,
        "SF_BUSINESS_CODE": naics, "SF_SPONS_US_STATE": "OH",
        "SF_TOT_PARTCP_BOY_CNT": participants,
        "SF_PLAN_NAME": f"{name} SIMPLE", "SF_PLAN_EFF_DATE": eff,
        "SF_PLAN_NUM": pn,
        "SF_TOT_ACT_PARTCP_BOY_CNT": participants if active is None else active,
        "SF_PLAN_ENTITY_CD": entity,
        "SF_TYPE_PENSION_BNFT_CODE": pension,
    }
    row.update(kw)
    return row


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    return duckdb.connect()


def filers(con, rows):
    """An SEC filer relation: cik, ein, name, tickers."""
    con.execute("create table filers_t (cik text, ein text, name text, "
                "tickers text)")
    if rows:
        con.executemany("insert into filers_t values (?,?,?,?)", rows)
    return con.table("filers_t")


# --- both forms ---------------------------------------------------------


def test_both_forms_are_loaded_into_one_shape(con, tmp_path) -> None:
    """The short form is 798,006 of 1,023,597 filings for 2024. A loader
    that reads only the main form has a quarter of the data."""
    archives = [
        make_zip(tmp_path, "main", 2024, [main_row("BIG CO", "111111111")]),
        make_zip(tmp_path, "short", 2024, [sf_row("SMALL LLC", "222222222")]),
    ]
    n = f5.load_filings(con, archives, tmp_path / "work")
    assert n == 2
    forms = dict(con.execute(
        "select form, count(*) from f5500_filings group by form").fetchall())
    assert forms == {"main": 1, "short": 1}


def test_the_short_form_has_no_dfe_column_and_that_is_not_an_error(
    con, tmp_path
) -> None:
    archives = [make_zip(tmp_path, "short", 2024,
                         [sf_row("SMALL LLC", "222222222")])]
    f5.load_filings(con, archives, tmp_path / "work")
    assert con.execute(
        "select dfe_code from f5500_filings").fetchone()[0] is None


def test_a_missing_csv_member_is_an_error_not_an_empty_load(
    con, tmp_path
) -> None:
    bad = tmp_path / "F_5500_2024_Latest.zip"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("something_else.csv", "a,b\n1,2\n")
    with pytest.raises(f5.Form5500Error):
        f5.load_filings(con, [f5.Archive("main", 2024, bad)],
                        tmp_path / "work")


# --- EIN handling -------------------------------------------------------


def test_a_placeholder_ein_is_not_an_ein(con, tmp_path) -> None:
    """000000000 and short strings are not identifiers."""
    archives = [make_zip(tmp_path, "main", 2024, [
        main_row("GOOD CO", "123456789"),
        main_row("ZERO CO", "000000000"),
        main_row("SHORT CO", "12345"),
    ])]
    f5.load_filings(con, archives, tmp_path / "work")
    got = dict(con.execute(
        "select sponsor_name, ein from f5500_filings").fetchall())
    assert got["GOOD CO"] == "123456789"
    assert got["ZERO CO"] is None
    assert got["SHORT CO"] is None


def test_sponsors_are_one_row_per_ein_not_per_plan(con, tmp_path) -> None:
    """A company with a 401(k) and a cafeteria plan files twice and is one
    company. Two plans, so two plan numbers -- DOL's own key for a plan."""
    archives = [make_zip(tmp_path, "main", 2024, [
        main_row("ACME INC", "123456789", participants="40", pn="001"),
        main_row("ACME INC", "123456789", participants="25", pn="501"),
    ])]
    f5.load_filings(con, archives, tmp_path / "work")
    assert f5.build_sponsors(con) == 1
    row = con.execute(
        "select plans, participants_sum, participants_max from f5500_sponsors"
    ).fetchone()
    # The sum double-counts the same people, so it is an upper bound and the
    # max a lower one. Both are carried; neither is "the headcount".
    assert row == (2, 65, 40)


def test_two_filings_of_one_plan_are_one_plan(con, tmp_path) -> None:
    """An amended filing repeats the plan. Counting both inflates the
    sponsor's headcount by a whole plan and inflates its plan count too."""
    archives = [make_zip(tmp_path, "main", 2024, [
        main_row("ACME INC", "123456789", participants="40", pn="001"),
        main_row("ACME INC", "123456789", participants="42", pn="001"),
    ])]
    f5.load_filings(con, archives, tmp_path / "work")
    f5.build_sponsors(con)
    row = con.execute(
        "select plans, participants_sum, participants_max from f5500_sponsors"
    ).fetchone()
    assert row == (1, 42, 42), "the amendment is the same plan, not a second one"


def test_a_plan_with_no_number_is_still_one_plan(con, tmp_path) -> None:
    """The number is missing on a small number of filings. Falling back to
    the plan name keeps them as separate plans rather than collapsing every
    unnumbered plan a sponsor has into one."""
    archives = [make_zip(tmp_path, "main", 2024, [
        main_row("ACME INC", "123456789", participants="40", pn="",
                 PLAN_NAME="ACME 401K"),
        main_row("ACME INC", "123456789", participants="25", pn="",
                 PLAN_NAME="ACME CAFETERIA"),
    ])]
    f5.load_filings(con, archives, tmp_path / "work")
    f5.build_sponsors(con)
    assert con.execute(
        "select plans from f5500_sponsors").fetchone()[0] == 2


# --- resolution ---------------------------------------------------------


def test_resolution_keys_on_ein(con, tmp_path) -> None:
    archives = [make_zip(tmp_path, "main", 2024,
                         [main_row("ACME INC", "123456789")])]
    f5.load_filings(con, archives, tmp_path / "work")
    f5.build_sponsors(con)
    res = f5.resolve(con, filers(con, [
        ("320193", "123456789", "COMPLETELY DIFFERENT NAME", "AAPL"),
    ]))
    assert res.by_ein == 1
    assert res.by_ein_listed == 1
    # The name shares nothing, and that is fine: EIN is the identifier.
    assert res.private == 0


def test_a_name_match_with_a_different_ein_is_never_a_resolution(
    con, tmp_path
) -> None:
    """44.2% of these are wrong, so a name match is a question, not a join."""
    archives = [make_zip(tmp_path, "main", 2024,
                         [main_row("ACME INC", "999999999")])]
    f5.load_filings(con, archives, tmp_path / "work")
    f5.build_sponsors(con)
    res = f5.resolve(con, filers(con, [
        ("320193", "123456789", "ACME INC", "ACME"),
    ]))
    assert res.by_ein == 0
    assert res.name_only == 1
    assert res.private == 0

    queue = f5.review_rows(con)
    assert len(queue) == 1
    assert queue[0][0] == "999999999"
    assert queue[0][5] == "exact_name"


def test_a_private_sponsor_matches_nothing_and_that_is_the_point(
    con, tmp_path
) -> None:
    archives = [make_zip(tmp_path, "short", 2024,
                         [sf_row("JOE'S DENTAL PRACTICE PC", "555555555")])]
    f5.load_filings(con, archives, tmp_path / "work")
    f5.build_sponsors(con)
    res = f5.resolve(con, filers(con, [
        ("320193", "123456789", "APPLE INC", "AAPL"),
    ]))
    assert res.private == 1
    assert res.name_only == 0
    assert f5.review_rows(con) == []


def test_resolution_refuses_to_change_the_row_count(con, tmp_path) -> None:
    """One normalized key can span several exact names, so a lookup grouped
    on both and joined on one fans out. It took the sponsor set from 858,480
    rows to 866,175 and inflated every count without failing anything."""
    archives = [make_zip(tmp_path, "main", 2024,
                         [main_row("ENERGY CORP", "777777777")])]
    f5.load_filings(con, archives, tmp_path / "work")
    f5.build_sponsors(con)
    # Three filers whose names all normalize to 'energy'.
    res = f5.resolve(con, filers(con, [
        ("1", "111111111", "ENERGY INC", ""),
        ("2", "222222222", "THE ENERGY COMPANY", ""),
        ("3", "333333333", "ENERGY HOLDINGS LLC", ""),
    ]))
    assert res.sponsors == 1
    queue = f5.review_rows(con)
    assert len(queue) == 1
    # The candidate count is what tells a reader the name is worthless here.
    assert queue[0][6] == 3


# --- DFE ----------------------------------------------------------------


def test_dfe_filings_are_flagged_not_dropped(con, tmp_path) -> None:
    """A DFE is a trustee. Dropping the rows would hide them; keeping them
    unflagged makes 'private companies' come back as BNY Mellon."""
    archives = [make_zip(tmp_path, "main", 2024, [
        main_row("REAL EMPLOYER INC", "111111111"),
        main_row("BIG BANK MASTER TRUST", "222222222", dfe="M"),
    ])]
    f5.load_filings(con, archives, tmp_path / "work")
    f5.build_sponsors(con)
    rows = dict(con.execute(
        "select sponsor_name, is_dfe from f5500_sponsors").fetchall())
    assert rows["REAL EMPLOYER INC"] is False
    assert rows["BIG BANK MASTER TRUST"] is True

    res = f5.resolve(con, filers(con, []))
    assert res.dfe == 1
    assert res.sponsors == 2, "the DFE row is still present"


@pytest.mark.parametrize("code", sorted(f5.DFE_CODES))
def test_every_documented_dfe_code_is_recognised(con, tmp_path, code) -> None:
    archives = [make_zip(tmp_path, "main", 2024,
                         [main_row(f"TRUST {code}", "111111111", dfe=code)])]
    f5.load_filings(con, archives, tmp_path / "work")
    f5.build_sponsors(con)
    assert con.execute("select is_dfe from f5500_sponsors").fetchone()[0] is True


# --- partial years ------------------------------------------------------


def test_a_partial_plan_year_says_so(con) -> None:
    """2025 held a third of 2024's filings while it was still being filed. A
    count that is small because the year is young must not read as a decline."""
    complete, why = f5.completeness(con, 2025, 300_000, baseline=1_023_597)
    assert complete is False
    assert "still being filed" in why
    assert "29%" in why


def test_a_complete_plan_year_says_that_too(con) -> None:
    complete, why = f5.completeness(con, 2024, 1_023_597, baseline=1_023_597)
    assert complete is True
    assert "complete" in why


def test_a_future_year_at_parity_is_accepted(con) -> None:
    """Once next year's filings catch up, it stops being partial."""
    complete, _ = f5.completeness(con, 2025, 1_000_000, baseline=1_023_597)
    assert complete is True


# --- names are for display, never for joining ---------------------------


def test_normalization_keeps_words_that_carry_meaning_for_a_sponsor(con) -> None:
    """'trust' and 'employees' are stripped by many normalisers. For a plan
    sponsor they are part of the name, and folding them away was part of why
    name matching scores 44.2%."""
    sql = f5.normalize_sql("name")
    con.execute("create table t (name text)")
    con.executemany("insert into t values (?)", [
        ("ACME INC",), ("ACME CORPORATION",),
        ("STATE STREET BANK AND TRUST",), ("ACME EMPLOYEES TRUST",),
    ])
    got = dict(con.execute(f"select name, {sql} from t").fetchall())
    assert got["ACME INC"] == "acme"
    assert got["ACME CORPORATION"] == "acme"
    assert "trust" in got["STATE STREET BANK AND TRUST"]
    assert got["ACME EMPLOYEES TRUST"] != got["ACME INC"]


def test_dataset_url_comes_from_the_manifest() -> None:
    """No literal DOL URL in the module; the invariant test enforces it."""
    url = f5.dataset_url("main", 2024)
    assert "2024" in url and "F_5500_2024" in url
    assert "SF" not in url.rsplit("/", 1)[1]
    assert "SF" in f5.dataset_url("short", 2024).rsplit("/", 1)[1]
    with pytest.raises(f5.Form5500Error):
        f5.dataset_url("nonsense", 2024)


def test_two_loads_of_the_same_input_agree_exactly(tmp_path) -> None:
    """Jobs are idempotent. One EIN can carry several name spellings across
    its plans, and picking one with any_value() gave a different answer every
    run: three consecutive loads of identical input produced 22,680, 22,685
    and 22,686 review rows, because the name chosen decides whether the
    sponsor name-matches at all."""
    rows = [
        main_row("ACME INC", "111111111"),
        main_row("ACME, INC.", "111111111"),
        main_row("ACME INCORPORATED", "111111111"),
        main_row("BETA LLC", "222222222"),
        main_row("BETA HOLDINGS LLC", "222222222"),
    ]
    sec = [("320193", "999999999", "ACME INC", "ACME"),
           ("320194", "888888888", "BETA LLC", "BETA")]

    seen = []
    for attempt in range(3):
        work = tmp_path / f"run{attempt}"
        archive = make_zip(work.parent, "main", 2024, rows)
        con = duckdb.connect()
        f5.load_filings(con, [archive], work)
        f5.build_sponsors(con)
        res = f5.resolve(con, filers(con, sec))
        seen.append((
            con.execute(
                "select sponsor_name from f5500_sponsors order by ein"
            ).fetchall(),
            res.name_only,
            tuple(sorted(r[0] for r in f5.review_rows(con))),
        ))
        con.close()

    assert seen[0] == seen[1] == seen[2], (
        "the same input produced different output across runs"
    )


def test_the_two_forms_do_not_share_an_entity_vocabulary(con, tmp_path) -> None:
    """``SF_PLAN_ENTITY_CD`` looks like ``TYPE_PLAN_ENTITY_CD`` and is not.

    On the main form 1 means multiemployer; on the short form it means
    single-employer and covers 795,824 of 798,006 filings for 2024. Reading
    both with one mapping marked the entire private population as union
    trusts and removed 800,287 sponsors from the mature-target screen -- and
    it read as a working filter, because the survivors were plausible and the
    only visible symptom was a smaller number.

    So the short form contributes no entity code at all, the same way it
    contributes no DFE code.
    """
    archives = [
        make_zip(tmp_path, "main", 2024,
                 [main_row("UNION TRUST BOARD", "111111111", entity="1")]),
        make_zip(tmp_path, "short", 2024,
                 [sf_row("SMALL DENTAL PRACTICE", "222222222", entity="1")]),
    ]
    f5.load_filings(con, archives, tmp_path / "work")
    f5.build_sponsors(con)
    got = dict(con.execute(
        "select sponsor_name, is_multiemployer from f5500_sponsors").fetchall())
    assert got["UNION TRUST BOARD"] is True
    assert got["SMALL DENTAL PRACTICE"] is False, (
        "the short form's code 1 is single-employer, not multiemployer")

