"""The XBRL tag map and resolver, on a synthetic quarter.

Synthetic because the real quarterly zips are ~100 MB each and a parser test
fixture is "small and deliberately chosen" per CLAUDE.md, never a bulk archive.
Every filer below exists to pin one decision, named in its comment, and the
coverage numbers the map records were measured on the real 2024q1 -- those are
checked by ``test_the_map_reproduces_its_own_measurement``, which is skipped
only when the cache is absent, and never silently: a skip here says the real
data is not on this machine, not that the map agrees with it.
"""

from __future__ import annotations

import csv
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb
import pytest

from marketradar.sources.xbrl import download as fetch_mod
from marketradar.sources.xbrl import resolve, tag_map

# --- a quarter, built by hand -------------------------------------------

SUB_COLS = ("adsh", "cik", "name", "sic", "form", "period", "fy", "fp", "filed")
NUM_COLS = ("adsh", "tag", "version", "ddate", "qtrs", "uom", "segments",
            "coreg", "value")
PRE_COLS = ("adsh", "report", "line", "stmt", "inpth", "tag")

FY_END = "20231231"
PRIOR_END = "20221231"
FILED = "20240215"

NOW = datetime(2026, 9, 10, 21, 0, tzinfo=timezone.utc)


def sub(adsh: str, name: str, sic: str, form: str = "10-K",
        period: str = FY_END, fp: str = "FY") -> dict:
    return {"adsh": adsh, "cik": adsh[:7].replace("-", ""), "name": name,
            "sic": sic, "form": form, "period": period, "fy": "2023", "fp": fp,
            "filed": FILED}


def num(adsh: str, tag: str, value: str | None, *, qtrs: int,
        ddate: str = FY_END, uom: str = "USD", segments: str = "",
        coreg: str = "") -> dict:
    return {"adsh": adsh, "tag": tag, "version": "us-gaap/2023",
            "ddate": ddate, "qtrs": str(qtrs), "uom": uom,
            "segments": segments, "coreg": coreg,
            "value": "" if value is None else value}


def pre(adsh: str, tag: str, line: int = 1, stmt: str = "IS") -> dict:
    return {"adsh": adsh, "report": "2", "line": str(line), "stmt": stmt,
            "inpth": "0", "tag": tag}


def complete(adsh: str, *, revenue: str = "1000") -> list[dict]:
    """Every concept stated, so a filer is in the population for all six."""
    return [
        num(adsh, "Revenues", revenue, qtrs=4),
        num(adsh, "NetIncomeLoss", "100", qtrs=4),
        num(adsh, "Assets", "5000", qtrs=0),
        num(adsh, "Liabilities", "2000", qtrs=0),
        num(adsh, "StockholdersEquity", "3000", qtrs=0),
        num(adsh, "NetCashProvidedByUsedInOperatingActivities", "250", qtrs=4),
    ]


#: One filer per decision. The id is the decision.
CLEAN = "0000000-24-000001"          # operating, everything stated
BANK = "0000000-24-000002"           # SIC 6022 -- a different table
NO_SIC = "0000000-24-000003"         # no SIC -- unknown, not operating
QUARTERLY = "0000000-24-000004"      # 10-Q -- a different period
PRE606 = "0000000-24-000005"         # FY began before the ASC 606 boundary
NIL_REVENUE = "0000000-24-000006"    # tags revenue, reports no amount
NEW_TAG = "0000000-24-000007"        # a revenue tag the map does not carry
SEGMENTS = "0000000-24-000008"       # revenue only disaggregated
CANADIAN = "0000000-24-000009"       # reports in CAD
PRIOR_ONLY = "0000000-24-000010"     # revenue only for the prior year
NO_STATEMENT = "0000000-24-000011"   # opens with R&D: pre-revenue
BOTH_INCOMES = "0000000-24-000012"   # NetIncomeLoss and ProfitLoss, differing


def write_quarter(work: Path, quarter: str = "2024q1") -> dict[str, Path]:
    subs = [
        sub(CLEAN, "CLEAN CO", "3711"),
        sub(BANK, "BANK CO", "6022"),
        sub(NO_SIC, "MYSTERY CO", ""),
        sub(QUARTERLY, "QUARTERLY CO", "3711", form="10-Q", fp="Q1"),
        # FY ending 2018-11-30 began 2017-12-01, one day inside the pre-606
        # era. The off-by-one this pins is the whole reason fiscal_start exists.
        sub(PRE606, "OLD ERA CO", "3711", period="20181130"),
        sub(NIL_REVENUE, "NIL BIOTECH", "2836"),
        sub(NEW_TAG, "NEW TAG CO", "7372"),
        sub(SEGMENTS, "SEGMENTS CO", "3711"),
        sub(CANADIAN, "CANADIAN CO", "1040"),
        sub(PRIOR_ONLY, "PRIOR ONLY CO", "3711"),
        sub(NO_STATEMENT, "PRE REVENUE CO", "2836"),
        sub(BOTH_INCOMES, "CONSOLIDATOR CO", "3711"),
    ]
    nums = [
        *complete(CLEAN),
        *complete(BANK),
        *complete(NO_SIC),
        *complete(QUARTERLY),
        *complete(PRE606),
        # Tagged for its own year, with no amount. XBRL nil: the filer has said
        # it has no revenue, which is evidence rather than an absence of it.
        num(NIL_REVENUE, "RevenueFromContractWithCustomerExcludingAssessedTax",
            None, qtrs=4),
        num(NIL_REVENUE, "Assets", "900", qtrs=0),
        # A real revenue line under a tag the map does not carry.
        num(NEW_TAG, "ConsultingFees", "400", qtrs=4),
        num(NEW_TAG, "Assets", "800", qtrs=0),
        # Revenue by segment only; no consolidated total.
        num(SEGMENTS, "Revenues", "600", qtrs=4, segments="ProductA"),
        num(SEGMENTS, "Revenues", "300", qtrs=4, segments="ProductB"),
        num(SEGMENTS, "Assets", "700", qtrs=0),
        num(CANADIAN, "Revenues", "1200", qtrs=4, uom="CAD"),
        num(CANADIAN, "Assets", "2400", qtrs=0, uom="CAD"),
        # Only the comparative year, which is not this filing's period.
        num(PRIOR_ONLY, "Revenues", "500", qtrs=4, ddate=PRIOR_END),
        num(PRIOR_ONLY, "Assets", "600", qtrs=0),
        num(NO_STATEMENT, "Assets", "300", qtrs=0),
        # The definition case: both tags, different values. 1,280 real filers
        # report both and 616 of them differ.
        *complete(BOTH_INCOMES),
        num(BOTH_INCOMES, "ProfitLoss", "175", qtrs=4),
        num(BOTH_INCOMES,
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
            "3400", qtrs=0),
    ]
    pres = [
        pre(CLEAN, "Revenues"),
        pre(BANK, "InterestAndDividendIncomeOperating"),
        pre(NIL_REVENUE, "RevenueFromContractWithCustomerExcludingAssessedTax"),
        pre(NEW_TAG, "ConsultingFees"),
        pre(SEGMENTS, "Revenues"),
        pre(CANADIAN, "Revenues"),
        pre(PRIOR_ONLY, "Revenues"),
        # Opens with an expense: pre-revenue, and nothing to look for.
        pre(NO_STATEMENT, "ResearchAndDevelopmentExpense"),
        pre(BOTH_INCOMES, "Revenues"),
    ]
    work.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for name, cols, rows in (("sub", SUB_COLS, subs), ("num", NUM_COLS, nums),
                             ("pre", PRE_COLS, pres)):
        path = work / f"{quarter}_{name}.txt"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=cols, delimiter="\t",
                                    lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        paths[name] = path
    return paths


@pytest.fixture
def quarter(tmp_path: Path) -> dict[str, Path]:
    return write_quarter(tmp_path / "work")


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    return duckdb.connect()


def built(con, quarter, **kw):
    return resolve.build("2024q1", con=con, tables=quarter, **kw)


def status_of(con, concept: str, adsh: str) -> tuple[str, str | None, object]:
    return con.execute(
        "select status, tag, value from fundamentals "
        "where concept = ? and adsh = ?", [concept, adsh]
    ).fetchone()


# --- eras ---------------------------------------------------------------


def test_the_era_boundary_is_on_the_fiscal_year_s_start() -> None:
    """A fiscal year ending 2018-12-31 began 2018-01-01 and is post-606.

    The off-by-one version -- subtracting a year and not adding the day -- puts
    every December filer on the wrong side of the cliff. That is half the
    market, and both answers resolve cleanly, so nothing about it would look
    wrong.
    """
    assert tag_map.fiscal_start(date(2018, 12, 31)) == date(2018, 1, 1)
    assert tag_map.era_for(date(2018, 12, 31)) == tag_map.POST_606
    # A November year end began in the previous December, before the boundary.
    assert tag_map.fiscal_start(date(2018, 11, 30)) == date(2017, 12, 1)
    assert tag_map.era_for(date(2018, 11, 30)) == tag_map.PRE_606
    # The boundary itself is inclusive: "beginning on or after".
    assert tag_map.era_for(date(2018, 12, 14)) == tag_map.POST_606


def test_a_leap_day_year_end_does_not_raise() -> None:
    assert tag_map.fiscal_start(date(2024, 2, 29)) == date(2023, 3, 1)


def test_the_pre_606_map_is_empty_and_says_so() -> None:
    """v1 is post-606 only. A pre-606 filing must resolve to nothing rather
    than be run through a map built from the other side of the cliff, which
    would resolve, and be wrong, and look identical."""
    assert tag_map.TAGS_BY_ERA[tag_map.PRE_606] == {}
    assert tag_map.concept("revenue", tag_map.PRE_606) is None
    with pytest.raises(ValueError, match="pre-606 map is empty"):
        resolve._resolve_concept(duckdb.connect(), "revenue", tag_map.PRE_606)


# --- who is in the population ------------------------------------------


def test_only_operating_companies_reach_the_table(con, quarter) -> None:
    built(con, quarter)
    names = {row[0] for row in con.execute(
        "select distinct company from fundamentals").fetchall()}
    assert "BANK CO" not in names, "a bank's statements are a different shape"
    assert "MYSTERY CO" not in names, "no SIC is unknown, not operating"
    assert "QUARTERLY CO" not in names, "a 10-Q is a different period"
    assert "OLD ERA CO" not in names, "pre-606 is out of v1"
    assert "CLEAN CO" in names


def test_the_funnel_names_every_stage_that_removed_anything(con, quarter) -> None:
    """The counts are the only thing that says whether a short list is
    selective or broken, which is why a normalizer gets a funnel too."""
    _, result = built(con, quarter)
    stages = {s.name: s.remaining for s in result.funnel.stages}
    assert stages["submissions"] == 12
    assert stages["annual report"] == 11          # the 10-Q is out
    assert stages["classified by SIC"] == 10      # no-SIC is out
    assert stages["operating company"] == 9       # the bank is out
    assert stages["post-606 era"] == 8            # the 2018 filer is out
    assert result.filings == 8
    for stage in result.funnel.stages:
        assert stage.why, f"{stage.name} carries a count with no reason"


def test_every_filing_gets_a_row_for_every_concept(con, quarter) -> None:
    """The unresolved are the deliverable too: a concept at 51% and one at 99%
    look identical downstream unless the misses are rows."""
    _, result = built(con, quarter)
    rows = int(con.execute("select count(*) from fundamentals").fetchone()[0])
    assert rows == result.filings * len(tag_map.CONCEPTS)
    assert con.execute(
        "select count(*) from fundamentals where status is null"
    ).fetchone()[0] == 0


# --- the statuses -------------------------------------------------------


def test_a_clean_filer_resolves_every_concept(con, quarter) -> None:
    built(con, quarter)
    for name in tag_map.CONCEPTS:
        status, tag, value = status_of(con, name, CLEAN)
        assert status == tag_map.STATED, f"{name} did not resolve"
        assert tag and value is not None


def test_a_nil_revenue_tag_is_absent_and_not_unmapped(con, quarter) -> None:
    """The filer tagged revenue for its own year and reported no amount, which
    is it saying it has none. Treating that as a mapping failure put 19 real
    clinical-stage biotechs into a work queue under a tag the map carries.
    """
    built(con, quarter)
    status, tag, value = status_of(con, "revenue", NIL_REVENUE)
    assert status == tag_map.ABSENT
    assert value is None, "a nil tag is not a reported zero"
    assert tag is None


def test_a_pre_revenue_income_statement_is_absent(con, quarter) -> None:
    """Opens with research and development expense. 8.9% of real operating
    filers do, and there is nothing to go and find for them."""
    built(con, quarter)
    assert status_of(con, "revenue", NO_STATEMENT)[0] == tag_map.ABSENT


def test_an_unknown_revenue_tag_is_unmapped_and_names_itself(
    con, quarter
) -> None:
    """The one status that is a work queue, and it has to name the tag or it is
    an investigation rather than a list."""
    _, result = built(con, quarter)
    assert status_of(con, "revenue", NEW_TAG)[0] == tag_map.UNMAPPED
    cov = next(c for c in result.coverage if c.concept == "revenue")
    assert ("ConsultingFees", 1) in cov.unmapped_tags


def test_segment_only_revenue_is_not_a_mapping_failure(con, quarter) -> None:
    built(con, quarter)
    assert status_of(con, "revenue", SEGMENTS)[0] == tag_map.SEGMENT_ONLY


def test_a_foreign_currency_filer_is_out_of_scope_not_unmapped(
    con, quarter
) -> None:
    """A CAD reporter is not a gap in the map, and counting it as one inflates
    the work queue with work that does not exist."""
    built(con, quarter)
    assert status_of(con, "revenue", CANADIAN)[0] == tag_map.NOT_USD


def test_a_prior_year_only_value_is_a_period_mismatch(con, quarter) -> None:
    """The comparative column is not this filing's year, and taking it would
    silently date every number one year early."""
    built(con, quarter)
    status, _tag, value = status_of(con, "revenue", PRIOR_ONLY)
    assert status == tag_map.PERIOD_MISMATCH
    assert value is None


def test_no_concept_is_left_without_a_status(con, quarter) -> None:
    built(con, quarter)
    seen = {row[0] for row in con.execute(
        "select distinct status from fundamentals").fetchall()}
    assert seen <= set(tag_map.STATUSES), seen - set(tag_map.STATUSES)


def test_the_statuses_a_reader_would_act_on_differently_stay_apart(
    con, quarter
) -> None:
    """Five ways not to resolve, four of which need no work. Summing them into
    "missing" would describe none of them -- the same reason `pending_years`
    and `gap_years` are separate columns in the Form 5500 series.
    """
    _, result = built(con, quarter)
    cov = next(c for c in result.coverage if c.concept == "revenue")
    assert cov.by_status[tag_map.ABSENT] == 2          # nil tag, and pre-revenue
    assert cov.by_status[tag_map.UNMAPPED] == 1
    assert cov.by_status[tag_map.SEGMENT_ONLY] == 1
    assert cov.by_status[tag_map.NOT_USD] == 1
    assert cov.by_status[tag_map.PERIOD_MISMATCH] == 1
    assert sum(cov.by_status.values()) == cov.population


# --- which tag won, and why it matters ----------------------------------


def test_the_preferred_tag_wins_and_the_row_says_which(con, quarter) -> None:
    """The measurement that makes this a correctness test rather than a style
    one: 1,280 of 2,804 real filers report both ``NetIncomeLoss`` and
    ``ProfitLoss`` and **616 report different values**, because one excludes
    noncontrolling interests and the other does not. An arbitrary pick makes
    the column's meaning depend on scan order.
    """
    built(con, quarter)
    status, tag, value = status_of(con, "net_income", BOTH_INCOMES)
    assert status == tag_map.STATED
    assert tag == "NetIncomeLoss", "the parent-attributable tag is preferred"
    assert int(value) == 100, "ProfitLoss (175) is the other definition"
    # Equity follows the same choice, so the two are consistent with each other.
    assert status_of(con, "equity", BOTH_INCOMES)[1] == "StockholdersEquity"


def test_three_loads_of_identical_input_agree_exactly(con, quarter) -> None:
    """Idempotent is not enough; deterministic is the requirement.

    ``build_sponsors`` produced 22,680, 22,685 and 22,686 review rows from
    byte-identical input because a name was picked with an arbitrary-row
    function. Every write was a correct upsert and every test passed, so this
    one compares the whole output three times, counts included.
    """
    shots = []
    for _ in range(3):
        fresh = duckdb.connect()
        resolve.build("2024q1", con=fresh, tables=quarter)
        shots.append(fresh.execute(
            "select adsh, concept, tag, value, status from fundamentals "
            "order by adsh, concept"
        ).fetchall())
    assert shots[0] == shots[1] == shots[2]
    assert shots[0], "the comparison is vacuous if nothing was loaded"


# --- the published partition --------------------------------------------


def test_the_load_writes_a_parquet_and_asserts_it(con, quarter, tmp_path,
                                                  monkeypatch) -> None:
    from marketradar import manifest

    override = tmp_path / "manifest.toml"
    target = (tmp_path / "out").as_posix()
    override.write_text(
        "[xbrl_fundamentals]\n"
        f'2024q1 = {{ location = "{target}", backend = "github_release" }}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv(manifest.OVERRIDE_ENV, str(override))
    manifest.clear_cache()
    try:
        result = resolve.load("2024q1", tmp_path / "out", con=con,
                              tables=quarter, min_rows=10)
        assert result.target and result.target.exists()
        assert result.observed.row_count == result.rows
        # The max date is put back after the staleness check is skipped, or
        # dataset_stats records NULL for every historical partition.
        assert result.observed.max_date == date(2023, 12, 31)
    finally:
        manifest.clear_cache()


def test_a_quarter_that_resolved_nothing_fails_rather_than_publishing(
    con, tmp_path, monkeypatch
) -> None:
    """The failure this project cares most about: a job exiting green on empty
    data. Every filing still produces six rows carrying a status, so a row
    count over the whole table would pass -- which is why the assertion is on
    the *resolved* rows.
    """
    from marketradar import manifest
    from marketradar.freshness import StaleDataError

    work = tmp_path / "work"
    write_quarter(work)
    # Strip every value, keeping the rows. The table stays full and nothing
    # resolves.
    num_path = work / "2024q1_num.txt"
    lines = num_path.read_text(encoding="utf-8").splitlines()
    head, body = lines[0], lines[1:]
    num_path.write_text(
        "\n".join([head] + [row.rsplit("\t", 1)[0] + "\t" for row in body]) + "\n",
        encoding="utf-8",
    )

    override = tmp_path / "manifest.toml"
    override.write_text(
        "[xbrl_fundamentals]\n"
        f'2024q1 = {{ location = "{(tmp_path / "out").as_posix()}", '
        'backend = "github_release" }\n',
        encoding="utf-8",
    )
    monkeypatch.setenv(manifest.OVERRIDE_ENV, str(override))
    manifest.clear_cache()
    tables = {n: work / f"2024q1_{n}.txt" for n in ("sub", "num", "pre")}
    try:
        with pytest.raises(StaleDataError):
            resolve.load("2024q1", tmp_path / "out", con=con, tables=tables,
                         min_rows=10)
    finally:
        manifest.clear_cache()


def test_a_private_backend_is_refused(con, quarter, tmp_path, monkeypatch) -> None:
    """The licensing boundary, asserted in the direction that matters here.

    SEC data is public domain and belongs in a Release. R2 is where
    vendor-derived data goes, and a dataset drifting across that line is the
    one mistake the manifest's backend column exists to make visible -- the
    same check tiingo.publish makes, pointing the other way.
    """
    from marketradar import manifest

    override = tmp_path / "manifest.toml"
    override.write_text(
        "[xbrl_fundamentals]\n"
        '2024q1 = { location = "r2://market-radar/x.parquet", backend = "r2" }\n',
        encoding="utf-8",
    )
    monkeypatch.setenv(manifest.OVERRIDE_ENV, str(override))
    manifest.clear_cache()
    try:
        with pytest.raises(ValueError, match="public domain"):
            resolve.load("2024q1", tmp_path / "out", con=con, tables=quarter,
                         min_rows=10)
    finally:
        manifest.clear_cache()


# --- the map's own claims -----------------------------------------------


def test_every_concept_carries_a_coverage_figure_and_a_definition() -> None:
    """A concept at 51% and one at 99% are both just a number in a column
    otherwise. The definition matters for the same reason: "net income" is two
    different numbers."""
    for name, got in tag_map.CONCEPTS.items():
        assert got.tags, name
        assert 0.0 < got.coverage_2024q1 <= 1.0, name
        assert got.definition.strip(), name
        assert got.qtrs in (tag_map.INSTANT, tag_map.ANNUAL), name


def test_only_revenue_has_topline_evidence() -> None:
    """What a filer puts at the top of its income statement is evidence about
    revenue and about nothing else. Applying it to all six produced a
    liabilities work queue of 445 filings whose suggested fixes were revenue
    tags -- sorted, plausible, and meaningless.
    """
    assert tag_map.CONCEPTS["revenue"].unmapped_when is not None
    for name, got in tag_map.CONCEPTS.items():
        if name != "revenue":
            assert got.unmapped_when is None, (
                f"{name} would suggest income-statement tags as fixes"
            )


def test_the_deferred_concepts_are_named_with_their_coverage() -> None:
    """Dropped from v1, not forgotten: each comes back carrying its own
    number rather than as a speculative column."""
    assert set(tag_map.DEFERRED_CONCEPTS) == {
        "capex", "shares", "cash", "operating_income"}
    assert not set(tag_map.DEFERRED_CONCEPTS) & set(tag_map.CONCEPTS)


def test_the_rejected_liabilities_derivation_is_recorded() -> None:
    """It would lift coverage 83.2% -> 99.0% and be wrong by 52x for ProKidney,
    because mezzanine equity sits in neither tag. Recorded rather than
    implemented, so the next reader does not re-derive it."""
    assert "ProKidney" in tag_map.DERIVED_LIABILITIES_REJECTED
    assert tag_map.CONCEPTS["liabilities"].tags == ("Liabilities",)


def test_asking_for_a_deferred_concept_is_an_error_that_explains_itself(
    con, quarter
) -> None:
    with pytest.raises(ValueError, match="measured and"):
        built(con, quarter, concepts=("capex",))


def test_one_concept_can_be_asked_for_alone(con, quarter) -> None:
    """The point of the long shape: revenue alone should cost the coverage of
    revenue alone, not of six concepts the caller never reads."""
    _, result = built(con, quarter, concepts=("revenue",))
    assert [c.concept for c in result.coverage] == ["revenue"]
    assert con.execute(
        "select count(distinct concept) from fundamentals").fetchone()[0] == 1


# --- fetch --------------------------------------------------------------


def test_quarter_names_are_validated() -> None:
    """The name is interpolated into a URL and into filenames."""
    for bad in ("2024", "2024q5", "24q1", "../etc", "2024q1; drop"):
        with pytest.raises(fetch_mod.XbrlFetchError):
            fetch_mod.fetch(bad)


def test_a_quarter_range_is_inclusive_and_ordered() -> None:
    assert fetch_mod.quarters("2023q3", "2024q2") == [
        "2023q3", "2023q4", "2024q1", "2024q2"]
    assert fetch_mod.quarters("2024q1", "2024q1") == ["2024q1"]
    with pytest.raises(fetch_mod.XbrlFetchError, match="before"):
        fetch_mod.quarters("2024q2", "2024q1")


def test_a_missing_user_agent_refuses_before_any_request(monkeypatch) -> None:
    """SEC blocks traffic without a real contact address, and asking anyway is
    how an IP gets flagged."""
    monkeypatch.delenv(fetch_mod.ENV_USER_AGENT, raising=False)
    with pytest.raises(fetch_mod.XbrlFetchError, match="contact address"):
        fetch_mod.user_agent()
    monkeypatch.setenv(fetch_mod.ENV_USER_AGENT, "market-radar")
    with pytest.raises(fetch_mod.XbrlFetchError, match="email"):
        fetch_mod.user_agent()


# --- against the real quarter -------------------------------------------

REAL_CACHE = Path(__file__).resolve().parents[1] / ".cache" / "xbrl"
REAL_ZIP = REAL_CACHE / "2024q1.zip"


@pytest.mark.skipif(
    not REAL_ZIP.exists(),
    reason=("the 2024q1 zip is not in .cache/xbrl on this machine. Run "
            "`mr xbrl --quarter 2024q1` to fetch it. Skipped rather than "
            "failed because this is a ~100 MB download from SEC, unlike the "
            "node toolchain the DOM suite needs -- which is installable "
            "offline and therefore fails."),
)
def test_the_map_reproduces_its_own_measurement() -> None:
    """Every coverage figure the map records, re-measured from the real data.

    This is what stops the map's own numbers from becoming folklore. Baseline
    tag churn between sampled years ran 11-20%, so these drift; the tolerance
    is half a point, which is tight enough to catch a map that has rotted and
    loose enough to survive the data set being restated.
    """
    # Extracted from the cached zip rather than read off disk: the loader
    # prunes unpacked tables after each quarter, so requiring them to be
    # already-unpacked would turn a normal backfill into a skipped test -- and a
    # skip reports the same green as a pass to anyone reading a summary line.
    # The zip stays, so this costs an unzip and no network.
    tables = fetch_mod.fetch("2024q1", cache=REAL_CACHE).tables
    con = duckdb.connect()
    con.execute("set preserve_insertion_order=false")
    _, result = resolve.build("2024q1", con=con, tables=tables)
    assert result.filings == 2_804, (
        f"the population is {result.filings}, not the 2,804 the map's coverage "
        "figures were measured over"
    )
    for cov in result.coverage:
        assert abs(cov.drift) <= 0.006, (
            f"{cov.concept}: {cov.rate:.1%} now against "
            f"{tag_map.CONCEPTS[cov.concept].coverage_2024q1:.1%} in the map "
            f"({cov.drift:+.1%})"
        )


# --- U10: the panel, which ships with the map --------------------------


def test_the_panel_waits_until_a_quarter_is_normalized() -> None:
    from marketradar.dashboard import shell

    state, detail = shell._probe_xbrl(
        shell.Context(generated_at=NOW, postgres=True))
    assert state == shell.WAITING
    assert "mr xbrl" in detail, "a waiting panel names the command that fixes it"


def test_the_panel_headline_reports_the_weakest_concept() -> None:
    """Not the average and not the best.

    Six concepts individually clear 98% and all six on one filer is 64%, so an
    average describes a table nobody reads. The number worth a headline is the
    concept a consumer is most likely to be disappointed by.
    """
    from conftest import XBRL_COVERAGE
    from marketradar.dashboard import shell

    state, detail = shell._probe_xbrl(
        shell.Context(generated_at=NOW, postgres=True, xbrl=XBRL_COVERAGE))
    assert state == shell.LIVE
    assert "revenue" in detail, detail      # 87.9%, the weaker of the two
    assert "assets" not in detail, detail   # 99.5%
    assert "2024q1" in detail


def test_the_panel_shows_the_work_queue_and_marks_drift() -> None:
    """The panel exists to find what the map needs next, so the two things it
    must never bury are the tag to add and a coverage figure that has fallen
    below what the map claims for itself."""
    from conftest import XBRL_COVERAGE
    from marketradar.dashboard import panels

    out = panels.xbrl_html(
        XBRL_COVERAGE["coverage"], XBRL_COVERAGE["funnel"], quarter="2024q1")
    assert "ConsultingFees" in out, "the unmapped tag is the deliverable"
    assert "unmapped" in out
    # Every status appears with a count, so four reasons that need no work
    # cannot be read as the same thing as the one that does.
    for status in ("absent", "segment_only", "period_mismatch"):
        assert status in out, status
    assert "banks and REITs" in out, "the funnel's stage reasons render too"


def test_a_concept_below_its_recorded_coverage_is_marked() -> None:
    from marketradar.dashboard import panels

    rotted = [{"concept": "revenue", "population": 100, "resolved": 50,
               "rate": 0.50, "drift": -0.38, "by_status": {"stated": 50},
               "unmapped_tags": [("SomeNewTag", 12)]}]
    assert 'class="rot"' in panels.xbrl_html(rotted)
    healthy = [dict(rotted[0], drift=0.001)]
    assert 'class="rot"' not in panels.xbrl_html(healthy)


def test_the_dashboard_derives_coverage_from_the_rows_it_stored(
    con, quarter, tmp_path, monkeypatch
) -> None:
    """The panel's numbers are a group-by over the partition, never a stored
    summary beside it.

    That is only possible because every filing gets a row carrying a status. A
    second copy of a number is a thing that can disagree with the first, and
    this is the number that says whether the map still works.
    """
    from marketradar import manifest
    from marketradar.cli import _xbrl_coverage

    out = tmp_path / "out"
    override = tmp_path / "manifest.toml"
    override.write_text(
        "[xbrl_fundamentals]\n"
        f'2024q1 = {{ location = "{out.as_posix()}", '
        'backend = "github_release" }\n',
        encoding="utf-8",
    )
    monkeypatch.setenv(manifest.OVERRIDE_ENV, str(override))
    manifest.clear_cache()
    try:
        loaded = resolve.load("2024q1", out, con=con, tables=quarter,
                              min_rows=10)
    finally:
        manifest.clear_cache()

    derived = _xbrl_coverage(duckdb.connect(), out)
    assert derived["quarter"] == "2024q1"
    from_load = {c.concept: c.resolved for c in loaded.coverage}
    from_file = {c["concept"]: c["resolved"] for c in derived["coverage"]}
    assert from_file == from_load
    rev = next(c for c in derived["coverage"] if c["concept"] == "revenue")
    assert rev["population"] == loaded.filings
    assert ("ConsultingFees", 1) in rev["unmapped_tags"]


def test_no_stored_partition_is_an_empty_dict_not_a_crash(tmp_path) -> None:
    """`mr dashboard` runs before the first normalization and must still draw
    the page; the panel says what to run."""
    from marketradar.cli import _xbrl_coverage

    assert _xbrl_coverage(duckdb.connect(), tmp_path) == {}


# --- the shape that bit tiingo.publish ----------------------------------


def test_two_quarters_in_one_process_do_not_cross(tmp_path, monkeypatch) -> None:
    """Each partition holds its own quarter and nothing else.

    This is the direct analogue of
    ``test_a_partition_never_takes_another_year_s_rows``, and it is here because
    ``load`` has the shape that bit ``tiingo.publish``: write a file, read it
    back, assert on what came back. That was safe for weeks and then published
    2023's rows into the 2024 partition the first time it ran on a fast POSIX
    runner.

    The difference is that tiingo's scratch path did **not** vary while its
    partition did, and this one's does -- the file is named for the quarter. So
    this test is the thing that says the difference still holds, and it can only
    fail on a platform where the stale read happens at all, which is why CI
    exists and runs Linux.
    """
    from marketradar import manifest

    out = tmp_path / "out"
    first = write_quarter(tmp_path / "w1", "2023q4")
    # A second quarter whose only resolved filer is different, so a crossed
    # read shows up as the wrong company rather than as a count that happens
    # to match.
    second_dir = tmp_path / "w2"
    second = write_quarter(second_dir, "2024q1")
    _only_clean(second_dir, "2024q1")

    override = tmp_path / "manifest.toml"
    override.write_text(
        "[xbrl_fundamentals]\n"
        f'2023q4 = {{ location = "{out.as_posix()}", backend = "local" }}\n'
        f'2024q1 = {{ location = "{out.as_posix()}", backend = "local" }}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv(manifest.OVERRIDE_ENV, str(override))
    manifest.clear_cache()
    shared = duckdb.connect()
    try:
        resolve.load("2023q4", out, con=shared, tables=first, min_rows=1)
        resolve.load("2024q1", out, con=shared, tables=second, min_rows=1)
    finally:
        manifest.clear_cache()

    for quarter, expected in (("2023q4", 8), ("2024q1", 1)):
        path = out / f"xbrl_fundamentals_{quarter}.parquet"
        got = shared.execute(
            "select count(distinct adsh), count(distinct src_quarter), "
            "min(src_quarter) from read_parquet(?)", [path.as_posix()]
        ).fetchone()
        assert got[0] == expected, (
            f"{quarter} holds {got[0]} filings, not {expected} -- the partitions "
            "crossed"
        )
        assert got[1] == 1 and got[2] == quarter, (
            f"{quarter}'s file carries src_quarter {got[2]!r}"
        )


def _only_clean(work: Path, quarter: str) -> None:
    """Strip the second fixture down to one filer, so a crossed read is loud."""
    sub_path = work / f"{quarter}_sub.txt"
    lines = sub_path.read_text(encoding="utf-8").splitlines()
    kept = [lines[0]] + [ln for ln in lines[1:] if ln.startswith(CLEAN)]
    sub_path.write_text("\n".join(kept) + "\n", encoding="utf-8")


def test_the_load_leaves_no_scratch_file_behind(con, quarter, tmp_path,
                                                monkeypatch) -> None:
    """The portable half of the check above.

    A shared mutable path is visible as the file it leaves lying around, on any
    platform -- which is how the tiingo version is pinned locally rather than
    only on a Linux runner.
    """
    from marketradar import manifest

    out = tmp_path / "out"
    override = tmp_path / "manifest.toml"
    override.write_text(
        "[xbrl_fundamentals]\n"
        f'2024q1 = {{ location = "{out.as_posix()}", backend = "local" }}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv(manifest.OVERRIDE_ENV, str(override))
    manifest.clear_cache()
    try:
        resolve.load("2024q1", out, con=con, tables=quarter, min_rows=10)
    finally:
        manifest.clear_cache()
    leftovers = sorted(p.name for p in out.iterdir()
                       if p.name != "xbrl_fundamentals_2024q1.parquet")
    assert leftovers == [], f"load left {leftovers} beside the partition"


# --- the range: resumable, pruned, and measured per quarter -------------


def _load_two(tmp_path, monkeypatch, con) -> Path:
    """Two quarters into one out_dir, so the matrix has a series to read."""
    from marketradar import manifest

    out = tmp_path / "out"
    first = write_quarter(tmp_path / "w1", "2019q1")
    second = write_quarter(tmp_path / "w2", "2019q2")
    override = tmp_path / "manifest.toml"
    override.write_text(
        "[xbrl_fundamentals]\n"
        f'2019q1 = {{ location = "{out.as_posix()}", backend = "local" }}\n'
        f'2019q2 = {{ location = "{out.as_posix()}", backend = "local" }}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv(manifest.OVERRIDE_ENV, str(override))
    manifest.clear_cache()
    try:
        resolve.load("2019q1", out, con=con, tables=first, min_rows=1)
        resolve.load("2019q2", out, con=duckdb.connect(), tables=second,
                     min_rows=1)
    finally:
        manifest.clear_cache()
    return out


def test_the_matrix_reports_each_concept_against_each_quarter(
    tmp_path, monkeypatch, con
) -> None:
    """The series, not a single number.

    Measured on the real data, revenue holds 85.9-91.2% across 2019-2024 with no
    trend while liabilities runs 74.4% -> 83.2%, because filers increasingly tag
    a total liabilities line. A 2019 quarter nine points below the map's 2024
    figure is therefore not rot, it is 2019 -- and only the series distinguishes
    those. A check that cannot make that distinction becomes noise and then gets
    ignored, which is worse than not having it.
    """
    out = _load_two(tmp_path, monkeypatch, con)
    quarters, rows = resolve.coverage_matrix(duckdb.connect(), out)
    assert quarters == ["2019q1", "2019q2"]
    by_concept = {r["concept"]: r for r in rows}
    assert set(by_concept) == set(tag_map.CONCEPTS)
    rev = by_concept["revenue"]
    assert set(rev["by_quarter"]) == {"2019q1", "2019q2"}
    assert rev["low"] <= rev["high"]
    assert 0.0 < rev["pooled"] <= 1.0
    # One q1 in range, so there is nothing to compare it to. Explicitly None
    # rather than a span against 2019q2, which is a different population.
    assert rev["span"] is None
    assert rev["span_quarter"] == "q1"


def test_the_span_compares_the_same_quarter_of_year_only(
    tmp_path, monkeypatch, con
) -> None:
    """The defect this pins was in the first version of the metric.

    It compared the oldest loaded quarter to the newest whatever they were, so
    over the real range it read 2019q1 against 2026q2 and reported revenue
    falling 8.1pp. Revenue is not falling: q1 carries the December fiscal year
    ends and averages 2,940 filings, q2-q4 are everyone else at a tenth the size
    and resolve a few points lower, and the "fall" was the calendar. Same
    mistake as comparing a sponsor's 2022 plan set against its 2024 one.
    """
    from marketradar import manifest

    out = tmp_path / "out"
    override = tmp_path / "manifest.toml"
    override.write_text(
        "[xbrl_fundamentals]\n"
        + "".join(f'{q} = {{ location = "{out.as_posix()}", '
                  'backend = "local" }\n'
                  for q in ("2019q1", "2019q2", "2020q1")),
        encoding="utf-8",
    )
    monkeypatch.setenv(manifest.OVERRIDE_ENV, str(override))
    manifest.clear_cache()
    try:
        for quarter in ("2019q1", "2019q2", "2020q1"):
            tables = write_quarter(tmp_path / quarter, quarter)
            if quarter == "2019q2":
                # Make the off-quarter deliberately different, so a span that
                # included it could not accidentally agree.
                _only_clean(tmp_path / quarter, quarter)
            resolve.load(quarter, out, con=duckdb.connect(), tables=tables,
                         min_rows=1)
    finally:
        manifest.clear_cache()

    _, rows = resolve.coverage_matrix(duckdb.connect(), out)
    rev = next(r for r in rows if r["concept"] == "revenue")
    expected = rev["by_quarter"]["2020q1"] - rev["by_quarter"]["2019q1"]
    assert rev["span"] == pytest.approx(expected), (
        "the span crossed into an off-quarter, which is a different population"
    )


def test_the_matrix_is_derived_not_stored(tmp_path, monkeypatch, con) -> None:
    """Nothing beside the partitions holds the coverage figures.

    They are a group-by over rows that every filing contributes to, which is
    the whole reason the unresolved are rows at all. A stored summary is a
    second copy of a number and the two drift.
    """
    out = _load_two(tmp_path, monkeypatch, con)
    written = sorted(p.name for p in out.iterdir())
    assert written == ["xbrl_fundamentals_2019q1.parquet",
                       "xbrl_fundamentals_2019q2.parquet"], written


def test_the_matrix_renders_without_any_partitions(tmp_path) -> None:
    quarters, rows = resolve.coverage_matrix(duckdb.connect(), tmp_path)
    assert (quarters, rows) == ([], [])
    assert resolve.matrix_lines(quarters, rows) == [
        "no partitions loaded; nothing to compare"]


def test_pruning_keeps_the_submissions_table_and_drops_the_bulk(tmp_path) -> None:
    """``sub.txt`` is 1.8 MB and the other two are 580 MB between them.

    Keeping every quarter's submissions table costs 54 MB across the range and is
    what the point-in-time filer universe reads, so it is cheaper to keep than to
    re-extract. ``num`` and ``pre`` are only needed during resolution.
    """
    cache = tmp_path / "cache"
    work = cache / "work"
    write_quarter(work, "2024q1")
    zip_path = cache / "2024q1.zip"
    zip_path.write_bytes(b"not a real zip, but a file that must survive")

    freed = fetch_mod.prune("2024q1", cache=cache)

    assert freed > 0
    assert (work / "2024q1_sub.txt").exists(), (
        "sub.txt went, so the filer universe now needs a re-extract"
    )
    for gone in ("num", "pre"):
        assert not (work / f"2024q1_{gone}.txt").exists(), f"{gone} survived"
    assert zip_path.exists(), "the zip went without being asked to"
    # Idempotent: pruning twice is not an error.
    assert fetch_mod.prune("2024q1", cache=cache) == 0


def test_dropping_the_zip_is_asked_for_and_not_assumed(tmp_path) -> None:
    """4.3 GB of zips for 8 MB of partitions is a bad trade on a full disk, and
    the zip buys one thing: a re-resolve without a re-download, which is ~30
    requests and twenty minutes. Worth a flag, not worth a default -- the
    reference quarter's zip is wanted, because a test re-measures the map's own
    coverage figures against it.
    """
    cache = tmp_path / "cache"
    write_quarter(cache / "work", "2024q1")
    zip_path = cache / "2024q1.zip"
    zip_path.write_bytes(b"x" * 2048)

    freed = fetch_mod.prune("2024q1", cache=cache, drop_zip=True)

    assert not zip_path.exists()
    assert freed >= 2048
    assert (cache / "work" / "2024q1_sub.txt").exists(), (
        "dropping the zip must not also drop the one table worth keeping"
    )


def test_a_quarter_already_loaded_is_skipped_unless_restart(
    tmp_path, monkeypatch, capsys
) -> None:
    """Resumable by default, per the sweep rule: a 30-quarter range will be
    interrupted, and re-resolving what already landed is wasted time.
    ``--restart`` is the explicit flag, never the default."""
    import argparse

    from marketradar import manifest
    from marketradar.cli import _cmd_xbrl

    out = tmp_path / "out"
    out.mkdir()
    (out / "xbrl_fundamentals_2019q1.parquet").write_bytes(b"")
    override = tmp_path / "manifest.toml"
    override.write_text(
        "[xbrl_fundamentals]\n"
        f'2019q1 = {{ location = "{out.as_posix()}", backend = "local" }}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv(manifest.OVERRIDE_ENV, str(override))
    manifest.clear_cache()
    args = argparse.Namespace(
        quarter=["2019q1"], through=None, out=str(out), cache=None,
        concept=None, no_load=False, restart=False, keep_extracts=True,
        matrix=False)
    try:
        assert _cmd_xbrl(args) == 0
    finally:
        manifest.clear_cache()
    printed = capsys.readouterr().out
    assert "already loaded" in printed
    assert "--restart" in printed, "the skip has to name the way to override it"


def test_a_range_expands_to_every_quarter_in_it() -> None:
    """2019q1 through 2026q2 is 30 quarters, and 2019q1 is the first that can
    be in this table at all: the ASC 606 boundary falls on fiscal years
    beginning 2017-12-15."""
    span = fetch_mod.quarters("2019q1", "2026q2")
    assert len(span) == 30
    assert span[0] == "2019q1" and span[-1] == "2026q2"
    assert span[4] == "2020q1"


def test_every_declared_partition_is_a_real_quarter() -> None:
    """The manifest carries one hand-written line per quarter, which is four a
    year. A typo there is a partition nothing can ever write to."""
    from marketradar import manifest

    declared = sorted(ref.partition for ref in manifest.datasets()
                      if ref.dataset == "xbrl_fundamentals")
    assert declared, "no xbrl partitions are declared"
    for name in declared:
        assert fetch_mod.QUARTER.match(name), name
    assert declared == sorted(
        fetch_mod.quarters(declared[0], declared[-1])), (
        "the declared partitions have a gap in them")


def test_the_matrix_ignores_another_dataset_in_the_same_directory(
    tmp_path, monkeypatch, con
) -> None:
    """The filer universe is written beside the partitions, so the reader has to
    glob the dataset prefix and not ``*.parquet``.

    Found by doing it wrong: a scratch script globbed everything, unioned
    ``sec_filers.parquet`` into the fundamentals, and DuckDB reported a missing
    ``period_end`` column. That is the *lucky* version -- two datasets that
    happened to share a column set would have unioned silently.
    """
    out = _load_two(tmp_path, monkeypatch, con)
    duckdb.connect().execute(
        "copy (select 1 as cik, 'X' as company) to ? (format parquet)",
        [(out / "sec_filers.parquet").as_posix()],
    )
    quarters, rows = resolve.coverage_matrix(duckdb.connect(), out)
    assert quarters == ["2019q1", "2019q2"], (
        "a neighbouring dataset was read as a partition"
    )
    assert {r["concept"] for r in rows} == set(tag_map.CONCEPTS)


def test_an_already_extracted_table_needs_neither_zip_nor_network(
    tmp_path, monkeypatch
) -> None:
    """The property that makes dropping the zips safe.

    ``fetch`` checks the extracts **before** the zip, so a quarter whose tables
    are unpacked needs no download. The other ordering -- which is what it had --
    would quietly re-fetch 3 GB to read files already on disk, and the only
    symptom would be a slow command.

    Proved by removing the zip and breaking the credentials, so any attempt to
    reach SEC raises rather than succeeding and hiding the fetch.
    """
    cache = tmp_path / "cache"
    write_quarter(cache / "work", "2024q1")
    assert not (cache / "2024q1.zip").exists()
    monkeypatch.setenv(fetch_mod.ENV_USER_AGENT, "")

    got = fetch_mod.fetch("2024q1", cache=cache, tables=("sub",))

    assert got.downloaded is False
    assert got.tables["sub"].exists()


def test_a_missing_table_still_needs_the_zip(tmp_path, monkeypatch) -> None:
    """The other half: asking for a table that is not unpacked must not silently
    report success. A quarter with only sub.txt cannot answer a resolve."""
    cache = tmp_path / "cache"
    write_quarter(cache / "work", "2024q1")
    (cache / "work" / "2024q1_num.txt").unlink()
    monkeypatch.setenv(fetch_mod.ENV_USER_AGENT, "")

    with pytest.raises(fetch_mod.XbrlFetchError, match="contact address"):
        fetch_mod.fetch("2024q1", cache=cache, tables=("sub", "num"))
