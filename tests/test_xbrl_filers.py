"""The point-in-time filer universe.

The module's whole claim is that it knows about companies that no longer exist,
so the fixtures are built around one that stops filing, one that keeps going,
and one that is merely late -- because the third is the one that makes the other
two mean anything.
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

import duckdb
import pytest

from marketradar.sources.xbrl import filers

SUB_COLS = ("adsh", "cik", "name", "sic", "form", "period", "fy", "fp", "filed")

#: Newest fiscal period end across the fixtures. The lag window is measured
#: back from this, not from today, so the fixtures do not rot.
NEWEST = "20260331"

ALIVE = "0000000100"      # files right up to the newest period
STOPPED = "0000000200"    # last filed for 2021, long before the window
LATE = "0000000300"       # stale period, recent filing: caught up late
RENAMED = "0000000400"    # two names, and the newer one must win
BANK = "0000000500"       # SIC 6022, still a filer -- identity is not fundamentals


def row(adsh: str, cik: str, name: str, period: str, filed: str, *,
        form: str = "10-K", sic: str = "3711") -> dict:
    return {"adsh": adsh, "cik": cik, "name": name, "sic": sic, "form": form,
            "period": period, "fy": period[:4], "fp": "FY", "filed": filed}


def write_sub(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SUB_COLS, delimiter="\t",
                                lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return path


@pytest.fixture
def subs(tmp_path: Path) -> dict[str, Path]:
    """Two quarters, so a filer can appear in one and not the other."""
    first = write_sub(tmp_path / "2021q1_sub.txt", [
        row("a-1", ALIVE, "ALIVE CO", "20201231", "20210215"),
        row("b-1", STOPPED, "DOOMED CO", "20201231", "20210215"),
        row("d-1", RENAMED, "OLD NAME INC", "20201231", "20210215"),
        row("e-1", BANK, "A BANK NA", "20201231", "20210215", sic="6022"),
        # A 10-Q, to prove the universe is not 10-K only: identity is a
        # different question from whether a statement is comparable.
        row("f-1", ALIVE, "ALIVE CO", "20210331", "20210501", form="10-Q"),
    ])
    second = write_sub(tmp_path / "2026q1_sub.txt", [
        row("a-2", ALIVE, "ALIVE CO", NEWEST, "20260515"),
        row("b-2", STOPPED, "DOOMED CO", "20211231", "20220215"),
        # A stale period (2023) with a recent filing (2025): caught up late,
        # which looks exactly like `stopped` if only period ends are read.
        row("c-1", LATE, "SLOW CO", "20230630", "20251201"),
        row("d-2", RENAMED, "NEW NAME CORP", NEWEST, "20260515"),
        row("e-2", BANK, "A BANK NA", NEWEST, "20260515", sic="6022"),
    ])
    return {"2021q1": first, "2026q1": second}


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    return duckdb.connect()


def built(con, subs) -> filers.Universe:
    return filers.build(con, list(subs), subs=subs)


def status_of(con, cik: str) -> str:
    return con.execute(
        "select status from sec_filers where cik = ?", [cik]).fetchone()[0]


# --- the three states ---------------------------------------------------


def test_a_filer_still_filing_is_filing(con, subs) -> None:
    built(con, subs)
    assert status_of(con, ALIVE) == filers.FILING


def test_a_filer_whose_history_ended_is_stopped(con, subs) -> None:
    """The population the whole module exists for: a company that no longer
    files is what an acquisition target becomes."""
    built(con, subs)
    assert status_of(con, STOPPED) == filers.STOPPED


def test_a_filer_that_caught_up_late_is_pending_rather_than_stopped(
    con, subs
) -> None:
    """Fourth outing for this distinction, after Form 5500's pending_years, the
    XBRL nil tag and deal_multiples' too_recent.

    SLOW CO's newest fiscal period is 2023 and its newest *filing* is late 2025.
    Read on period ends alone it is indistinguishable from a company that
    stopped -- and calling it stopped would put a live filer into the acquisition
    population, where its "deals" would be measured as takeouts.
    """
    built(con, subs)
    assert status_of(con, LATE) == filers.PENDING


def test_the_lag_is_measured_from_the_newest_loaded_period(con, subs) -> None:
    """Not from today. The universe is a property of the loaded range, so a
    fixture written in 2026 must not start failing in 2028."""
    universe = built(con, subs)
    assert universe.newest_period == date(2026, 3, 31)


def test_the_counts_add_up_to_the_rows(con, subs) -> None:
    universe = built(con, subs)
    assert universe.filing + universe.stopped + universe.pending == universe.rows
    assert universe.rows == 5, "one row per CIK, not per filing"


# --- what the universe is, and is not -----------------------------------


def test_every_form_type_counts_toward_identity(con, subs) -> None:
    """ALIVE filed a 10-Q as well as 10-Ks. The fundamentals table is
    deliberately annual-10-K-only; an identity table that inherited that filter
    would not know a company existed between annual reports."""
    built(con, subs)
    filings = con.execute(
        "select filings from sec_filers where cik = ?", [ALIVE]).fetchone()[0]
    assert filings == 3, "the 10-Q was dropped"


def test_a_bank_is_in_the_universe(con, subs) -> None:
    """Banks are excluded from the fundamentals table because their statements
    are a different shape. That says nothing about whether they exist, and an
    acquired bank is still an acquisition."""
    built(con, subs)
    assert status_of(con, BANK) == filers.FILING
    sic = con.execute(
        "select sic from sec_filers where cik = ?", [BANK]).fetchone()[0]
    assert sic == 6022
    assert filers.classify_sic(sic) != "operating"


def test_a_renamed_filer_keeps_its_newest_name(con, subs) -> None:
    """Picked by ``max_by`` on (filed, period_end), never by an arbitrary-row
    function: two spellings taking turns between loads is exactly what
    ``build_sponsors`` did, and which spelling won decided whether a sponsor
    matched anything at all.
    """
    built(con, subs)
    name, names = con.execute(
        "select company, names from sec_filers where cik = ?", [RENAMED]
    ).fetchone()
    assert name == "NEW NAME CORP"
    assert names == 2, "the older name was not counted"


def test_three_builds_of_the_same_input_agree_exactly(con, subs) -> None:
    """Deterministic, not merely idempotent. The name pick is the part that
    could drift and the part that would matter."""
    shots = []
    for _ in range(3):
        fresh = duckdb.connect()
        filers.build(fresh, list(subs), subs=subs)
        shots.append(fresh.execute(
            "select cik, company, sic, first_period, last_period, status "
            "from sec_filers order by cik").fetchall())
    assert shots[0] == shots[1] == shots[2]
    assert shots[0]


# --- the sweep list ----------------------------------------------------


def test_the_stopped_list_is_the_sweep_population(con, subs) -> None:
    built(con, subs)
    assert filers.stopped_ciks(con) == [STOPPED]


def test_the_stopped_list_is_ordered_deterministically(con, subs) -> None:
    """It is chunked into a checkpointed sweep, and Checkpoint keys on chunk
    index -- so an unstable order would resume against a different work list
    while believing it had resumed correctly."""
    built(con, subs)
    assert filers.stopped_ciks(con) == filers.stopped_ciks(con)


def test_no_quarters_is_an_error_rather_than_an_empty_universe(con) -> None:
    """An empty universe would make every downstream count zero and look like a
    clean answer."""
    with pytest.raises(ValueError, match="no quarters"):
        filers.build(con, [])


# --- the gap this exists to measure ------------------------------------


def test_coverage_against_a_current_only_table_shows_the_hole(con, subs) -> None:
    """The number the whole refinement turns on: a current-only entity list
    knows the filers that are still filing and not the ones that stopped, and
    the hole is shaped exactly like an acquisition.
    """
    built(con, subs)
    con.execute(
        "create or replace table companies_now as "
        f"select * from (values ('{ALIVE}'), ('{RENAMED}'), ('{BANK}')) "
        "as t(cik)"
    )
    cov = filers.coverage_against(con, "companies_now")
    assert cov["by_status"][filers.FILING]["share"] == pytest.approx(1.0)
    assert cov["by_status"][filers.STOPPED]["known"] == 0
    assert cov["by_status"][filers.STOPPED]["share"] == pytest.approx(0.0)


# --- the published table -----------------------------------------------


def test_the_load_writes_a_parquet_and_asserts_it(con, subs, tmp_path,
                                                  monkeypatch) -> None:
    from marketradar import manifest

    out = tmp_path / "out"
    override = tmp_path / "manifest.toml"
    override.write_text(
        "[sec_filers]\n"
        f'all = {{ location = "{out.as_posix()}", backend = "local" }}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv(manifest.OVERRIDE_ENV, str(override))
    manifest.clear_cache()
    try:
        universe, observed = filers.load(con, list(subs), out, subs=subs,
                                         min_rows=1)
    finally:
        manifest.clear_cache()
    assert (out / "sec_filers.parquet").exists()
    assert observed.row_count == universe.rows
    # The max date is put back after the staleness check is skipped, or
    # dataset_stats records NULL for a table that has a perfectly good span.
    assert observed.max_date == date(2026, 3, 31)


def test_a_private_backend_is_refused(con, subs, tmp_path, monkeypatch) -> None:
    """SEC data is public domain and belongs in a Release. R2 is for anything a
    vendor touched, and a dataset drifting across that line is the one mistake
    the manifest's backend column exists to make visible."""
    from marketradar import manifest

    override = tmp_path / "manifest.toml"
    override.write_text(
        "[sec_filers]\n"
        'all = { location = "r2://market-radar/f.parquet", backend = "r2" }\n',
        encoding="utf-8",
    )
    monkeypatch.setenv(manifest.OVERRIDE_ENV, str(override))
    manifest.clear_cache()
    try:
        with pytest.raises(ValueError, match="public domain"):
            filers.load(con, list(subs), tmp_path / "out", subs=subs, min_rows=1)
    finally:
        manifest.clear_cache()


def test_an_empty_load_fails_rather_than_publishing(con, subs, tmp_path,
                                                   monkeypatch) -> None:
    """The failure this project cares most about. A universe of nothing is a
    parse that matched nothing, not a market with no companies in it."""
    from marketradar import manifest
    from marketradar.freshness import StaleDataError

    override = tmp_path / "manifest.toml"
    override.write_text(
        "[sec_filers]\n"
        f'all = {{ location = "{(tmp_path / "out").as_posix()}", '
        'backend = "local" }\n',
        encoding="utf-8",
    )
    monkeypatch.setenv(manifest.OVERRIDE_ENV, str(override))
    manifest.clear_cache()
    try:
        with pytest.raises(StaleDataError):
            filers.load(con, list(subs), tmp_path / "out", subs=subs,
                        min_rows=10_000)
    finally:
        manifest.clear_cache()
