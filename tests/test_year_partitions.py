"""Year partitioning, and the freshness contract a closed year deserves.

A closed year cannot be *fresh* — 2020's newest bar is years old and always
will be — but "not fresh" must not become "not checked". A historical pull
that quietly stopped in June is exactly the failure worth catching, and it
produces a large, valid, correctly typed partition. So only the wall-clock
test is dropped; row count and both year boundaries are still asserted, which
is a stricter contract than the live partition gets.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import duckdb
import pytest

from marketradar import manifest
from marketradar.freshness import StaleDataError
from marketradar.sources import tiingo

YEARS = [str(y) for y in range(2016, 2027)]


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    lines = ["[prices_eod_raw]"]
    for y in YEARS:
        target = (tmp_path / f"prices_{y}.parquet").as_posix()
        lines.append(f'{y} = {{ location = "{target}", backend = "r2" }}')
    lines += ["", "[tiingo_api]",
              'base = { location = "https://example.invalid", backend = "vendor_api" }']
    override = tmp_path / "manifest.toml"
    override.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setenv(manifest.OVERRIDE_ENV, str(override))
    manifest.clear_cache()
    yield tmp_path, duckdb.connect()
    manifest.clear_cache()


def stage(tmp_path: Path, name: str, rows) -> Path:
    staging = tmp_path / name
    staging.mkdir(exist_ok=True)
    con = duckdb.connect()
    con.execute(
        "create table c (ticker varchar, date date, open decimal(18,6), "
        "high decimal(18,6), low decimal(18,6), close decimal(18,6), "
        "volume bigint, exchange varchar, security_type varchar, "
        "source varchar, ingested_at timestamptz)"
    )
    ing = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
    for ticker, day in rows:
        con.execute(
            "insert into c values (?, ?, 1, 1, 1, 1, 1000, 'NYSE', 'stock', "
            "'tiingo', ?)", [ticker, day, ing])
    con.execute(
        f"copy c to '{(staging / 'chunk_00000.parquet').as_posix()}' (format parquet)")
    return staging


def full_year(year: int, tickers=("AAA",)):
    """Bars spanning a complete year, including both boundary sessions."""
    days = [date(year, 1, 2), date(year, 6, 15), date(year, 12, 30)]
    return [(t, d) for t in tickers for d in days]


def rows_at(con, path: Path) -> int:
    return int(con.execute(
        "select count(*) from read_parquet(?)", [path.as_posix()]).fetchone()[0])


# --- splitting ----------------------------------------------------------


def test_a_multi_year_stage_writes_one_partition_per_year(env) -> None:
    tmp_path, con = env
    staging = stage(tmp_path, "backfill",
                    full_year(2023) + full_year(2024) + full_year(2025))

    observed = tiingo.publish_all(staging, con=con)

    assert [o.partition for o in observed] == ["2023", "2024", "2025"]
    for y in (2023, 2024, 2025):
        assert rows_at(con, tmp_path / f"prices_{y}.parquet") == 3


def test_a_partition_never_takes_another_year_s_rows(env) -> None:
    """The bug this prevents: a sweep crossing New Year putting both years in
    whichever partition the caller happened to name."""
    tmp_path, con = env
    staging = stage(tmp_path, "newyear",
                    full_year(2024) + full_year(2025))

    tiingo.publish_all(staging, con=con)

    for y in (2024, 2025):
        got = con.execute(
            "select distinct year(date) from read_parquet(?)",
            [(tmp_path / f"prices_{y}.parquet").as_posix()],
        ).fetchall()
        assert got == [(y,)]


def test_staged_years_reports_what_is_present(env) -> None:
    tmp_path, con = env
    staging = stage(tmp_path, "s", full_year(2019) + full_year(2026))
    assert tiingo.staged_years(con, staging) == ["2019", "2026"]


# --- the closed-year contract ------------------------------------------


def test_a_closed_year_does_not_fail_on_wall_clock_staleness(env) -> None:
    """2016 is a decade old. That is not a defect."""
    tmp_path, con = env
    observed = tiingo.publish_all(stage(tmp_path, "old", full_year(2016)), con=con)
    assert observed[0].partition == "2016"
    assert observed[0].row_count == 3


def test_a_closed_year_that_stops_early_fails(env) -> None:
    """The pull that quietly stopped in June. Large, valid, and wrong."""
    tmp_path, con = env
    truncated = [("AAA", date(2020, 1, 2)), ("AAA", date(2020, 6, 30))]
    with pytest.raises(StaleDataError, match="stopped early"):
        tiingo.publish_all(stage(tmp_path, "trunc", truncated), con=con)


def test_a_closed_year_that_starts_late_fails(env) -> None:
    """Truncation at the front is just as real and would otherwise pass."""
    tmp_path, con = env
    late = [("AAA", date(2020, 7, 1)), ("AAA", date(2020, 12, 31))]
    with pytest.raises(StaleDataError, match="started late"):
        tiingo.publish_all(stage(tmp_path, "late", late), con=con)


@pytest.mark.parametrize("last", [date(2021, 12, 29), date(2021, 12, 30),
                                  date(2021, 12, 31)])
def test_any_real_last_session_of_a_year_is_accepted(env, last) -> None:
    """The last US session is the 31st, or the Friday before when it is a
    weekend -- so always the 29th, 30th or 31st. No calendar dependency."""
    tmp_path, con = env
    rows = [("AAA", date(2021, 1, 4)), ("AAA", last)]
    observed = tiingo.publish_all(stage(tmp_path, f"y{last.day}", rows), con=con)
    assert observed[0].row_count == 2


def test_a_closed_year_still_asserts_row_count(env) -> None:
    tmp_path, con = env
    staging = stage(tmp_path, "thin", full_year(2018))
    with pytest.raises(StaleDataError, match="at least"):
        tiingo.publish(staging, "2018", con=con, min_rows=10_000)


def test_the_current_year_keeps_the_wall_clock_contract(env) -> None:
    """For the live partition staleness is the real signal, so it stays."""
    tmp_path, con = env
    stale = [("AAA", date(2026, 1, 5)), ("AAA", date(2026, 1, 6))]
    with pytest.raises(StaleDataError, match="days before"):
        tiingo.publish(stage(tmp_path, "stale26", stale), "2026",
                       con=con, min_rows=1, max_staleness_days=4)


def test_the_contract_is_chosen_by_year_not_by_flag(env) -> None:
    """No caller has to remember which contract applies."""
    import inspect

    sig = inspect.signature(tiingo._assert_partition_fresh)
    assert "partition" in sig.parameters
    assert not any(
        "closed" in name or "historical" in name for name in sig.parameters
    )


def test_a_closed_year_still_records_its_max_date(env) -> None:
    """Skipping the staleness test must not skip *measuring* the date.

    assert_fresh with date_column=None does not compute one, so without
    putting it back dataset_stats records NULL for every historical partition
    and the freshness history has a hole exactly where the backfill is.
    """
    tmp_path, con = env
    observed = tiingo.publish_all(stage(tmp_path, "y", full_year(2022)), con=con)
    assert observed[0].partition == "2022"
    assert observed[0].max_date == date(2022, 12, 30)


def test_the_current_year_still_reports_its_max_date(env) -> None:
    tmp_path, con = env
    rows = [("AAA", date(2026, 9, 3)), ("AAA", date(2026, 9, 4))]
    observed = tiingo.publish_all(stage(tmp_path, "cur", rows), con=con,
                                  max_staleness_days=10_000)
    assert observed[0].max_date == date(2026, 9, 4)
