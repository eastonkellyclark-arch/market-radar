"""What day it is, for the two different definitions of "day" this uses.

The cron fires at 03:30 UTC, which is 23:30 ET during EDT and 22:30 ET during
EST. At that instant the UTC calendar has already rolled over but the US
market has not: the session that just closed is *yesterday* in UTC terms. Any
code deriving a trading date from ``datetime.now(timezone.utc).date()`` is
off by one for the whole window between the close and midnight UTC — which is
precisely the window the nightly sweep runs in.

Two consequences, one harmless and one not:

* The request window shifts a day forward. Tiingo returns what exists, so the
  sweep still succeeds; it just asks for a session that has not happened yet.
* ``partition = str(end.year)`` puts December 31st's bars into *next* year's
  Parquet. Partitions are immutable and append-only, so that is a silent,
  permanent misfile that no later job would ever revisit or notice.

:func:`market_today` is the trading date. :func:`marketradar.freshness.utc_today`
stays UTC and should: staleness is measured against wall-clock time, not
against sessions. The two are different questions and keeping one function for
each is what stops them being confused again.

pytz rather than :mod:`zoneinfo`: Windows ships no IANA database, so
``ZoneInfo("America/New_York")`` raises ``ZoneInfoNotFoundError`` on the
machine this is developed on. pytz carries its own copy and is already a
dependency for DuckDB's Postgres scanner.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Final

import pytz

#: The exchange timezone. Every US equity session is defined against this,
#: and it carries its own DST rules, so EDT/EST needs no special handling.
MARKET_TZ: Final = pytz.timezone("America/New_York")


def market_now(now: datetime | None = None) -> datetime:
    """Now, in the exchange's timezone. ``now`` is injectable for tests."""
    now = now or datetime.now(pytz.utc)
    if now.tzinfo is None:
        now = pytz.utc.localize(now)
    return now.astimezone(MARKET_TZ)


def market_today(now: datetime | None = None) -> date:
    """The trading date in US/Eastern.

    Deliberately *not* a market calendar: it knows nothing about weekends or
    holidays and does not try to. Callers request a window and take what comes
    back — sessions that did not happen simply return no rows, which is both
    correct and far cheaper than maintaining a holiday table.
    """
    return market_now(now).date()
