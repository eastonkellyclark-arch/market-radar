"""A publish merges; it never overwrites.

The bug this file exists for: ``publish()`` rewrote the year partition from
the sweep's staging directory alone, so every night silently discarded every
session outside the sweep window. It produced a partition that was valid,
fresh, correctly typed, and two days long. ``assert_fresh`` passed it every
time, because the data *was* fresh — there was just almost none of it.

Nothing here touches the network. The manifest is overridden to point the
partition at a local file, so the whole merge runs against real Parquet.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pytest

from marketradar import manifest
from marketradar.sources import tiingo

PARTITION = "2026"


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    """A manifest pointing the partition at a local Parquet file.

    Declared ``r2`` because that is what the licensing guard requires and the
    guard is production code that should not be relaxed for a test. The
    location is local; only the label is borrowed.
    """
    target = tmp_path / "prices_2026.parquet"
    override = tmp_path / "manifest.toml"
    override.write_text(
        "[prices_eod_raw]\n"
        f'{PARTITION} = {{ location = "{target.as_posix()}", backend = "r2" }}\n'
        "\n[tiingo_api]\n"
        'base = { location = "https://example.invalid", backend = "vendor_api" }\n',
        encoding="utf-8",
    )
    monkeypatch.setenv(manifest.OVERRIDE_ENV, str(override))
    manifest.clear_cache()
    con = duckdb.connect()
    yield tmp_path, target, con
    manifest.clear_cache()


def stage(tmp_path: Path, name: str, rows, ingested: datetime | None = None) -> Path:
    """Write one staging directory holding a single chunk of bars."""
    staging = tmp_path / name
    staging.mkdir(exist_ok=True)
    ingested = ingested or datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
    con = duckdb.connect()
    con.execute(
        "create table c (ticker varchar, date date, open decimal(18,6), "
        "high decimal(18,6), low decimal(18,6), close decimal(18,6), "
        "volume bigint, exchange varchar, security_type varchar, "
        "source varchar, ingested_at timestamptz)"
    )
    for ticker, day, close in rows:
        con.execute(
            "insert into c values (?, ?, ?, ?, ?, ?, ?, 'NYSE', 'stock', "
            "'tiingo', ?)",
            [ticker, day, close, close, close, close, 1_000_000, ingested],
        )
    con.execute(
        f"copy c to '{(staging / 'chunk_00000.parquet').as_posix()}' (format parquet)"
    )
    return staging


def rows_at(con, target: Path) -> int:
    return int(
        con.execute(
            "select count(*) from read_parquet(?)", [target.as_posix()]
        ).fetchone()[0]
    )


def published(con, target: Path):
    return con.execute(
        "select ticker, date, close from read_parquet(?) order by ticker, date",
        [target.as_posix()],
    ).fetchall()


D1, D2, D3 = date(2026, 9, 2), date(2026, 9, 3), date(2026, 9, 4)


# --- the core proof -----------------------------------------------------


def test_a_second_sweep_grows_the_partition(env) -> None:
    """Two non-overlapping windows. The partition must accumulate.

    This is the regression in one test: under the old code the second publish
    left two rows, because staging was the only input.
    """
    tmp_path, target, con = env

    first = stage(tmp_path, "sweep1", [("AAA", D1, 10), ("BBB", D1, 20)])
    tiingo.publish(first, PARTITION, con=con, min_rows=1,
                   max_staleness_days=10_000)
    assert rows_at(con, target) == 2

    second = stage(tmp_path, "sweep2", [("AAA", D2, 11), ("BBB", D2, 21)])
    tiingo.publish(second, PARTITION, con=con, min_rows=1,
                   max_staleness_days=10_000)

    assert rows_at(con, target) == 4
    assert {r[1] for r in published(con, target)} == {D1, D2}


def test_history_outside_the_sweep_window_is_carried_forward(env) -> None:
    tmp_path, target, con = env
    tiingo.publish(stage(tmp_path, "s1", [("AAA", D1, 10)]), PARTITION,
                   con=con, min_rows=1, max_staleness_days=10_000)
    tiingo.publish(stage(tmp_path, "s2", [("AAA", D3, 12)]), PARTITION,
                   con=con, min_rows=1, max_staleness_days=10_000)

    assert published(con, target) == [
        ("AAA", D1, pytest.approx(10)),
        ("AAA", D3, pytest.approx(12)),
    ]


# --- dedupe -------------------------------------------------------------


def test_an_overlapping_window_replaces_rather_than_duplicates(env) -> None:
    """Re-running a window must not double the bars in it."""
    tmp_path, target, con = env
    early = datetime(2026, 9, 8, 1, 0, tzinfo=timezone.utc)
    late = early + timedelta(hours=6)

    tiingo.publish(stage(tmp_path, "s1", [("AAA", D1, 10), ("AAA", D2, 11)],
                         ingested=early),
                   PARTITION, con=con, min_rows=1, max_staleness_days=10_000)
    tiingo.publish(stage(tmp_path, "s2", [("AAA", D2, 99), ("AAA", D3, 12)],
                         ingested=late),
                   PARTITION, con=con, min_rows=1, max_staleness_days=10_000)

    got = published(con, target)
    assert len(got) == 3, "one row per (ticker, date, source)"
    # The newer ingest wins: a revised bar replaces the one already stored.
    assert dict((r[1], float(r[2])) for r in got)[D2] == pytest.approx(99)


def test_a_stale_restatement_does_not_overwrite_a_newer_bar(env) -> None:
    """Dedupe is by ingested_at, not by which file was read last."""
    tmp_path, target, con = env
    late = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
    early = late - timedelta(days=1)

    tiingo.publish(stage(tmp_path, "s1", [("AAA", D1, 10)], ingested=late),
                   PARTITION, con=con, min_rows=1, max_staleness_days=10_000)
    tiingo.publish(stage(tmp_path, "s2", [("AAA", D1, 77)], ingested=early),
                   PARTITION, con=con, min_rows=1, max_staleness_days=10_000)

    assert float(published(con, target)[0][2]) == pytest.approx(10)


# --- the invariant ------------------------------------------------------


def test_a_publish_can_never_shrink_a_partition(env, monkeypatch) -> None:
    """The assertion this needed on day one.

    Under a merge a shrink is unreachable -- the union always contains every
    prior row -- so the prior count is forced high to simulate the *next*
    version of this bug: someone changing the source back to staging alone.
    That is exactly the shape the original had, and it passed every other
    check because the rows it did write were fresh and correctly typed.
    """
    tmp_path, target, con = env
    tiingo.publish(stage(tmp_path, "s1", [("AAA", D1, 10), ("BBB", D1, 20)]),
                   PARTITION, con=con, min_rows=1, max_staleness_days=10_000)
    assert rows_at(con, target) == 2

    monkeypatch.setattr(tiingo, "_existing_rows", lambda con, loc: 9_999)

    with pytest.raises(tiingo.PartitionShrankError, match="9,999"):
        tiingo.publish(stage(tmp_path, "s2", [("AAA", D2, 11)]), PARTITION,
                       con=con, min_rows=1, max_staleness_days=10_000)

    assert rows_at(con, target) == 2, "the published file must be untouched"


def test_the_guard_names_the_direction_not_just_the_numbers(env, monkeypatch) -> None:
    tmp_path, target, con = env
    tiingo.publish(stage(tmp_path, "s1", [("AAA", D1, 10)]), PARTITION,
                   con=con, min_rows=1, max_staleness_days=10_000)
    monkeypatch.setattr(tiingo, "_existing_rows", lambda con, loc: 500)

    with pytest.raises(tiingo.PartitionShrankError) as exc:
        tiingo.publish(stage(tmp_path, "s2", [("AAA", D2, 11)]), PARTITION,
                       con=con, min_rows=1, max_staleness_days=10_000)
    assert "does not remove them" in str(exc.value)
    assert "restate=True" in str(exc.value)


# --- restatement --------------------------------------------------------


def test_restate_replaces_the_partition_and_permits_removal(env) -> None:
    """Merging makes removal impossible without this.

    Rows already published are carried forward forever, so purging symbols
    that should never have been swept -- ZVZZT and the rest of the exchange
    test tickers -- needs a way to say "the partition is exactly this".
    """
    tmp_path, target, con = env
    tiingo.publish(stage(tmp_path, "s1", [("AAA", D1, 10), ("ZVZZT", D1, 100)]),
                   PARTITION, con=con, min_rows=1, max_staleness_days=10_000)
    assert rows_at(con, target) == 2

    clean = stage(tmp_path, "clean", [("AAA", D1, 10)])

    # A normal publish carries ZVZZT forward: the merge cannot remove it.
    tiingo.publish(clean, PARTITION, con=con, min_rows=1,
                   max_staleness_days=10_000)
    assert rows_at(con, target) == 2
    assert "ZVZZT" in {r[0] for r in published(con, target)}

    # A restatement replaces it outright.
    tiingo.publish(clean, PARTITION, con=con, min_rows=1,
                   max_staleness_days=10_000, restate=True)
    assert rows_at(con, target) == 1
    assert published(con, target)[0][0] == "AAA"


def test_restate_is_never_the_default(env) -> None:
    """A caller that forgets the flag gets the safe behaviour."""
    import inspect

    sig = inspect.signature(tiingo.publish)
    assert sig.parameters["restate"].default is False
    assert sig.parameters["restate"].kind is inspect.Parameter.KEYWORD_ONLY


# --- schema ------------------------------------------------------------


def test_schema_drift_is_refused_rather_than_null_filled(env) -> None:
    """UNION ALL BY NAME would quietly NULL-fill a renamed column."""
    tmp_path, target, con = env
    tiingo.publish(stage(tmp_path, "s1", [("AAA", D1, 10)]), PARTITION,
                   con=con, min_rows=1, max_staleness_days=10_000)

    drifted = tmp_path / "drifted"
    drifted.mkdir()
    chunk = (tmp_path / "s1" / "chunk_00000.parquet").as_posix()
    out = (drifted / "chunk_00000.parquet").as_posix()
    con.execute(
        f"copy (select * exclude (exchange), exchange as venue "
        f"from read_parquet('{chunk}')) to '{out}' (format parquet)"
    )

    with pytest.raises(tiingo.TiingoError, match="schemas differ"):
        tiingo.publish(drifted, PARTITION, con=con, min_rows=1,
                       max_staleness_days=10_000)


def test_first_publish_needs_no_existing_partition(env) -> None:
    tmp_path, target, con = env
    assert not target.exists()
    tiingo.publish(stage(tmp_path, "s1", [("AAA", D1, 10)]), PARTITION,
                   con=con, min_rows=1, max_staleness_days=10_000)
    assert rows_at(con, target) == 1
