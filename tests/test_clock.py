"""The trading date is not the UTC date.

Every case here is pinned to a real instant rather than "now", because the bug
only exists inside a five-hour window and a test that used the current time
would pass all day and fail at night.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from marketradar import clock
from marketradar.freshness import utc_today
from marketradar.sources import tiingo

#: The cron's own schedule: "30 3 * * 2-6".
CRON_UTC = datetime(2026, 9, 8, 3, 30, tzinfo=timezone.utc)


def test_at_the_cron_hour_the_trading_date_is_the_previous_day() -> None:
    """03:30 UTC is 23:30 the evening before, in ET."""
    assert CRON_UTC.date() == date(2026, 9, 8)
    assert clock.market_today(CRON_UTC) == date(2026, 9, 7)


def test_the_year_boundary_is_the_case_that_corrupts_a_partition() -> None:
    """Dec 31st's session must not be filed into next year's Parquet.

    Partitions are immutable and append-only, so a misfile here is permanent
    and nothing downstream ever revisits it.
    """
    new_year = datetime(2027, 1, 1, 3, 30, tzinfo=timezone.utc)
    assert new_year.date().year == 2027
    assert clock.market_today(new_year) == date(2026, 12, 31)
    assert str(clock.market_today(new_year).year) == "2026"


@pytest.mark.parametrize(
    "moment, expected",
    [
        # EDT, UTC-4: rollover at 04:00 UTC.
        (datetime(2026, 9, 8, 3, 59, tzinfo=timezone.utc), date(2026, 9, 7)),
        (datetime(2026, 9, 8, 4, 0, tzinfo=timezone.utc), date(2026, 9, 8)),
        # EST, UTC-5: rollover at 05:00 UTC.
        (datetime(2026, 1, 15, 4, 59, tzinfo=timezone.utc), date(2026, 1, 14)),
        (datetime(2026, 1, 15, 5, 0, tzinfo=timezone.utc), date(2026, 1, 15)),
        # Mid-session: the two agree, which is why this hid for so long.
        (datetime(2026, 9, 8, 18, 0, tzinfo=timezone.utc), date(2026, 9, 8)),
    ],
)
def test_dst_rollover_handled_by_the_zone_not_by_us(moment, expected) -> None:
    assert clock.market_today(moment) == expected


def test_naive_input_is_treated_as_utc() -> None:
    assert clock.market_today(datetime(2026, 9, 8, 3, 30)) == date(2026, 9, 7)


def test_market_today_and_utc_today_are_both_kept() -> None:
    """They answer different questions and must not be collapsed.

    Staleness is wall-clock; a trading date is a session. They differ by at
    most a day and only ever at night, which is exactly when the cron runs.
    """
    assert (utc_today() - clock.market_today()).days in (0, 1)


def test_default_window_anchors_on_the_trading_date() -> None:
    start, end = tiingo.default_window(days=5, today=clock.market_today(CRON_UTC))
    assert end == date(2026, 9, 7)
    assert start == date(2026, 9, 2)


def test_default_window_no_longer_asks_for_a_future_session() -> None:
    """Regression: the old UTC anchor requested a session that had not run."""
    start, end = tiingo.default_window(days=5)
    assert end <= clock.market_today()
    assert end - start == timedelta(days=5)


def test_no_source_derives_a_trading_date_from_utc_now() -> None:
    """The pattern that caused this, kept out of the loaders by assertion."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "marketradar"
    offenders = []
    for path in list((root / "sources").glob("*.py")) + [root / "cli.py"]:
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "timezone.utc).date()" in line.replace(" ", ""):
                offenders.append(f"{path.name}:{n}: {line.strip()}")
    assert not offenders, (
        "A trading date is being derived from the UTC clock:\n"
        + "\n".join(offenders)
        + "\n\nUse marketradar.clock.market_today(). See that module for why."
    )
