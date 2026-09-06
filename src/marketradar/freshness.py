"""The assertion that makes a green run mean something.

A job that exits 0 on empty data is the failure mode this project cares most
about. Every pipeline stage ends with :func:`assert_fresh`, and it *raises* —
it never warns, never logs and continues, never returns a boolean the caller
can forget to check.

Written as an explicit call rather than a decorator or a base class. A
decorator can only see return values and would need per-dataset thresholds
threaded through its arguments; a base class would couple every source to a
shared parent, which the one-module-per-source rule forbids. An explicit call
is greppable, shows up in every diff, and needs no mocking to test. Universal
adoption is enforced by ``tests/test_repo_invariants.py`` instead.

Note the staleness check counts *calendar* days, not trading days. A market
calendar would be a dependency and a source of false alarms on holidays; the
failure actually worth catching is a feed that has stopped, and four calendar
days catches that while surviving a Friday close read on a Monday holiday.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timezone
from typing import Any, Final

from marketradar.manifest import Freshness

#: Survives a Friday close read the following Monday, including when that
#: Monday is a holiday. Long enough to avoid false alarms, short enough that a
#: genuinely stopped feed is caught the next morning.
DEFAULT_MAX_STALENESS_DAYS: Final[int] = 4

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class StaleDataError(RuntimeError):
    """A pipeline stage produced data that fails its freshness contract.

    Raised for empty results, short results, missing columns, and data whose
    newest row is older than the caller allows.
    """


def utc_today() -> date:
    """Today in UTC. All storage is UTC; conversion happens at the digest."""
    return datetime.now(timezone.utc).date()


def assert_fresh(
    dataset: str,
    relation: Any,
    *,
    partition: str = "all",
    min_rows: int,
    date_column: str | None = "date",
    max_staleness_days: int = DEFAULT_MAX_STALENESS_DAYS,
    expect_cols: Iterable[str] = (),
    today: date | None = None,
) -> Freshness:
    """Assert a relation is populated, complete, and current.

    Args:
        dataset: name used in error messages and recorded observations.
        relation: a DuckDB relation, or anything exposing ``columns`` and
            ``query(alias, sql)``.
        partition: partition label for the recorded observation.
        min_rows: fail below this. Pick a real floor, not 1 — a market-wide
            price load returning 12 rows is broken even though it is not empty.
        date_column: column holding the observation date. None skips the
            staleness check entirely, for datasets with no time dimension.
        max_staleness_days: how many calendar days behind ``today`` the newest
            row may be.
        expect_cols: columns that must be present. Checked before anything
            else so a schema change reports as a schema change.
        today: injectable for tests. Defaults to :func:`utc_today`.

    Returns:
        The :class:`~marketradar.manifest.Freshness` observation, ready to
        hand to :func:`marketradar.manifest.record_stats`.

    Raises:
        StaleDataError: on any failed check.
        ValueError: if ``date_column`` is not a plain identifier.
    """
    where = f"{dataset}/{partition}"
    today = today or utc_today()

    # Validate the identifier before anything touches it: a malformed
    # date_column is a programming error, not a data problem, and must not be
    # reported as a schema change.
    if date_column is not None and not _IDENTIFIER.match(date_column):
        raise ValueError(f"date_column {date_column!r} is not a plain identifier")

    columns: Sequence[str] = tuple(getattr(relation, "columns", ()) or ())
    _assert_columns(where, columns, expect_cols, date_column)

    if date_column is not None and date_column in columns:
        sql = f'SELECT count(*) AS n, max("{date_column}") AS newest FROM rel'
    else:
        sql = "SELECT count(*) AS n, NULL AS newest FROM rel"

    row = relation.query("rel", sql).fetchone()
    row_count = int(row[0])
    newest = _as_date(row[1])

    if row_count < min_rows:
        raise StaleDataError(
            f"{where}: expected at least {min_rows:,} rows, found {row_count:,}. "
            f"{'The source returned nothing.' if row_count == 0 else 'The load is incomplete.'}"
        )

    if date_column is not None:
        if newest is None:
            raise StaleDataError(
                f"{where}: {row_count:,} rows but every {date_column!r} is NULL, "
                "so freshness cannot be established."
            )
        age = (today - newest).days
        if age > max_staleness_days:
            raise StaleDataError(
                f"{where}: newest {date_column} is {newest.isoformat()}, "
                f"{age} days before {today.isoformat()} "
                f"(limit {max_staleness_days}). The feed has probably stopped."
            )
        if age < 0:
            raise StaleDataError(
                f"{where}: newest {date_column} is {newest.isoformat()}, which is "
                f"after {today.isoformat()}. Check the source's timezone handling."
            )

    return Freshness(
        dataset=dataset, partition=partition, row_count=row_count, max_date=newest
    )


def _assert_columns(
    where: str,
    columns: Sequence[str],
    expect_cols: Iterable[str],
    date_column: str | None,
) -> None:
    required = list(expect_cols)
    if date_column is not None:
        required.append(date_column)

    missing = [c for c in dict.fromkeys(required) if c not in columns]
    if missing:
        raise StaleDataError(
            f"{where}: missing column(s) {', '.join(repr(c) for c in missing)}. "
            f"Found: {', '.join(columns) or '<no columns>'}. "
            "The source's schema changed."
        )


def _as_date(value: Any) -> date | None:
    """Coerce whatever DuckDB hands back for a max() into a date."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])
