"""Per-ticker detail: OHLCV candles at three resolutions, actions, and gaps.

Only the names on screen. Embedding eleven years for 14,000 tickers would be a
gigabyte; the twenty-four lists hold ~345 distinct names and those are the only
ones clickable.

**Candles aggregate, they never drop rows.** The old series was a thinned line of
closes -- 180 points sampled out of up to 3,112 sessions -- which is a fine way to
draw a line and a wrong way to draw a candle. A candle is an aggregate of a period,
so a sampled candle is a bar that never traded: its open, high, low and close all
come from one arbitrary session standing in for a month. So the series is
*aggregated* into real periods instead, with open from the first session, close from
the last, high the maximum, low the minimum and volume the sum.

**Three resolutions, because one cannot serve eight timeframes.**

    d   daily, the last 252 sessions     1D 1W 1M 3M YTD 1Y
    w   weekly, the last 260 weeks       5Y
    m   monthly, the whole history       All

Measured 2026-09-12 over the real 345-name set: 207 daily, 135 weekly and 46
monthly bars per ticker on average, for a **4.8 MB payload against 0.83 MB** for the
thinned line -- the page goes from 1.9 MB to about 5.9 MB. That is the price of
candles over eight timeframes and it is paid deliberately. `DAILY_SESSIONS` and
`WEEKLY_BARS` are the two knobs if it ever needs to come back down.

Exact recent values are read off the tail of the daily series rather than shipped a
second time. They used to be a separate `r` array, which was the same numbers in two
places -- and two copies of one fact is the defect this codebase keeps finding.

Dates travel as day-offsets from an epoch rather than ISO strings -- the same payload
is ~40% smaller and the page has to open from disk.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Final, Iterable

import duckdb

log = logging.getLogger(__name__)

#: Day 0 for the compact date encoding. The first backfilled session.
EPOCH: Final[date] = date(2016, 1, 1)

#: Daily sessions carried. 252 is a trading year, which is the longest timeframe
#: that should be drawn as daily candles -- past that they are thinner than a pixel
#: and the aggregate is the honest picture.
DAILY_SESSIONS: Final[int] = 252

#: Weekly bars carried. 260 weeks is five years, the longest weekly timeframe.
WEEKLY_BARS: Final[int] = 260

#: Exact bars listed in the table beside the chart, taken off the daily tail.
RECENT_BARS: Final[int] = 12

#: Matches the screen's guard, so what is marked here is exactly what is
#: suppressed there.
GAP_DAYS: Final[int] = 30

#: ``(day, open, high, low, close, volume)``.
Bar = tuple[int, float, float, float, float, int]


class DetailError(RuntimeError):
    """The detail payload could not be built from the prices given."""


def _d(value: date) -> int:
    return (value - EPOCH).days


@dataclass(frozen=True, slots=True)
class Detail:
    ticker: str
    daily: list[Bar] = field(default_factory=list)
    weekly: list[Bar] = field(default_factory=list)
    monthly: list[Bar] = field(default_factory=list)
    actions: list[tuple[int, str, str]] = field(default_factory=list)
    gaps: list[tuple[int, int, int]] = field(default_factory=list)

    def payload(self) -> dict[str, Any]:
        return {
            "d": [list(b) for b in self.daily],
            "w": [list(b) for b in self.weekly],
            "m": [list(b) for b in self.monthly],
            "a": [list(row) for row in self.actions],
            "g": [list(row) for row in self.gaps],
        }


def aggregate(rows: list[tuple[date, float, float, float, float, int]],
              key: Any) -> list[Bar]:
    """Daily bars into periods: first open, last close, max high, min low, sum volume.

    ``key`` maps a date to its period. Rows must be in date order -- open and close
    are positional, so an unsorted input would silently take the open from whichever
    row happened to come first, which is the `any_value` failure wearing a different
    hat.

    The period is stamped with the **first session in it**, not the calendar start of
    the period. A week whose Monday was a holiday opens on the Tuesday, and labelling
    it Monday would claim a session that did not happen.
    """
    out: list[Bar] = []
    current = None
    day = 0
    op = hi = lo = cl = 0.0
    vol = 0
    for when, o, h, l, c, v in rows:
        bucket = key(when)
        if bucket != current:
            if current is not None:
                out.append((day, op, hi, lo, cl, vol))
            current, day, op, hi, lo, cl, vol = bucket, _d(when), o, h, l, c, v
            continue
        hi = max(hi, h)
        lo = min(lo, l)
        cl = c
        vol += v
    if current is not None:
        out.append((day, op, hi, lo, cl, vol))
    return out


def _week(when: date) -> tuple[int, int]:
    iso = when.isocalendar()
    return (iso[0], iso[1])


def _month(when: date) -> tuple[int, int]:
    return (when.year, when.month)


def build(
    con: duckdb.DuckDBPyConnection,
    prices: duckdb.DuckDBPyRelation,
    tickers: Iterable[str],
    actions: duckdb.DuckDBPyRelation | None = None,
) -> dict[str, dict[str, Any]]:
    """One payload per ticker. Two queries total, not two per ticker."""
    wanted = sorted({t for t in tickers if t})
    if not wanted:
        return {}
    quoted = ", ".join("'" + t.replace("'", "''") + "'" for t in wanted)

    con.register("detail_px", prices)
    # **`distinct`, because the input is not keyed the way it looks.** The price
    # staging holds 2,042 `(ticker, date)` pairs twice, byte-identical in OHLCV and
    # differing only in `ingested_at` -- an artifact of overlapping sweep chunks.
    # A line of closes did not care; `sum(volume)` over an aggregate does, and would
    # have doubled those sessions' volume with nothing to show it had.
    bars = con.execute(f"""
        select distinct ticker, date, open, high, low, close, volume
        from detail_px where ticker in ({quoted})
        order by ticker, date
    """).fetchall()

    by_ticker: dict[str, list] = {}
    for row in bars:
        by_ticker.setdefault(row[0], []).append(row)

    action_rows: dict[str, list] = {}
    if actions is not None:
        con.register("detail_act", actions)
        for t, ex, sf, dc in con.execute(f"""
            select distinct ticker, ex_date, split_factor, div_cash from detail_act
            where ticker in ({quoted}) order by ticker, ex_date
        """).fetchall():
            action_rows.setdefault(t, []).append((_d(ex), str(sf), str(dc)))

    out: dict[str, dict[str, Any]] = {}
    for ticker, rows in by_ticker.items():
        # Distinct resolved the duplicates above only because they were identical in
        # every price column. Two *different* bars for one session is a restatement
        # and the chart must not pick one: it would draw a candle that is half of
        # each, which is the shape of wrong answer this whole file is arranged to
        # avoid. So it raises rather than aggregating something ambiguous.
        days = [r[1] for r in rows]
        if len(set(days)) != len(days):
            dupes = sorted({d for d in days if days.count(d) > 1})[:3]
            raise DetailError(
                f"{ticker} has more than one distinct bar for "
                f"{', '.join(str(d) for d in dupes)}. Aggregating would average two "
                "different sessions into one candle, so this stops instead."
            )

        gaps: list[tuple[int, int, int]] = []
        for i in range(1, len(rows)):
            delta = (rows[i][1] - rows[i - 1][1]).days
            if delta > GAP_DAYS:
                gaps.append((_d(rows[i - 1][1]), _d(rows[i][1]), delta))

        ohlcv = [
            (r[1], float(r[2]), float(r[3]), float(r[4]), float(r[5]), int(r[6]))
            for r in rows
        ]
        out[ticker] = Detail(
            ticker=ticker,
            daily=[(_d(w), o, h, l, c, v) for w, o, h, l, c, v
                   in ohlcv[-DAILY_SESSIONS:]],
            weekly=aggregate(ohlcv, _week)[-WEEKLY_BARS:],
            monthly=aggregate(ohlcv, _month),
            actions=action_rows.get(ticker, [])[-RECENT_BARS:],
            gaps=gaps,
        ).payload()
    return out
