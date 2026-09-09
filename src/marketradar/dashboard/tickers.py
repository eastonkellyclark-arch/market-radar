"""Per-ticker detail: the chart series, recent bars, actions, and the gaps.

Only the names on screen. Embedding eleven years for 14,000 tickers would be
a gigabyte; the twenty-four lists hold ~330 distinct names and those are the
only ones clickable.

**Full history, downsampled -- not a recent window.** The discontinuities
worth seeing are the ones the gap guard suppresses, and those are measured in
years: AAAP's hole is 3,045 days. A one-year chart would show none of them. So
the series spans everything available and is thinned to a drawable number of
points, with the bars either side of every gap forced in so a discontinuity
survives the thinning that would otherwise average it away.

Exact recent values live in a small table beside the chart rather than in it.
A downsampled line answers "what shape" and a table answers "what price"; one
mark cannot do both honestly.

Dates travel as day-offsets from an epoch rather than ISO strings -- the same
payload is ~40% smaller and the page has to open from disk.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Final, Iterable

import duckdb

log = logging.getLogger(__name__)

#: Day 0 for the compact date encoding. The first backfilled session.
EPOCH: Final[date] = date(2016, 1, 1)

#: Points per chart. Enough to read eleven years of shape; small enough that
#: 330 of them fit in a file that opens from disk.
CHART_POINTS: Final[int] = 180

#: Exact bars shown in the table beside the chart.
RECENT_BARS: Final[int] = 12

#: Matches the screen's guard, so what is marked here is exactly what is
#: suppressed there.
GAP_DAYS: Final[int] = 30


def _d(value: date) -> int:
    return (value - EPOCH).days


@dataclass(frozen=True, slots=True)
class Detail:
    ticker: str
    series: list[tuple[int, float]] = field(default_factory=list)
    recent: list[tuple[int, float, float, float, float, int]] = field(
        default_factory=list
    )
    actions: list[tuple[int, str, str]] = field(default_factory=list)
    gaps: list[tuple[int, int, int]] = field(default_factory=list)

    def payload(self) -> dict[str, Any]:
        return {
            "s": [[d, c] for d, c in self.series],
            "r": [list(row) for row in self.recent],
            "a": [list(row) for row in self.actions],
            "g": [list(row) for row in self.gaps],
        }


def _thin(rows: list[tuple[date, float]], keep: set[int]) -> list[tuple[int, float]]:
    """Even sampling, with the indices in ``keep`` forced in.

    A gap boundary dropped by the sampler is a discontinuity the chart would
    silently smooth over, which is the one thing this series exists to show.
    """
    n = len(rows)
    if n <= CHART_POINTS:
        wanted = set(range(n))
    else:
        step = n / CHART_POINTS
        wanted = {int(i * step) for i in range(CHART_POINTS)}
        wanted.add(n - 1)
    wanted |= keep
    return [(_d(rows[i][0]), float(rows[i][1])) for i in sorted(wanted)]


def build(
    con: duckdb.DuckDBPyConnection,
    prices: duckdb.DuckDBPyRelation,
    tickers: Iterable[str],
    actions: duckdb.DuckDBPyRelation | None = None,
) -> dict[str, dict[str, Any]]:
    """One payload per ticker. Three queries total, not three per ticker."""
    wanted = sorted({t for t in tickers if t})
    if not wanted:
        return {}
    quoted = ", ".join("'" + t.replace("'", "''") + "'" for t in wanted)

    con.register("detail_px", prices)
    bars = con.execute(f"""
        select ticker, date, open, high, low, close, volume
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
            select ticker, ex_date, split_factor, div_cash from detail_act
            where ticker in ({quoted}) order by ticker, ex_date
        """).fetchall():
            action_rows.setdefault(t, []).append((_d(ex), str(sf), str(dc)))

    out: dict[str, dict[str, Any]] = {}
    for ticker, rows in by_ticker.items():
        gaps: list[tuple[int, int, int]] = []
        boundary: set[int] = set()
        for i in range(1, len(rows)):
            delta = (rows[i][1] - rows[i - 1][1]).days
            if delta > GAP_DAYS:
                gaps.append((_d(rows[i - 1][1]), _d(rows[i][1]), delta))
                boundary.add(i - 1)
                boundary.add(i)

        detail = Detail(
            ticker=ticker,
            series=_thin([(r[1], r[5]) for r in rows], boundary),
            recent=[
                (_d(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]),
                 int(r[6]))
                for r in rows[-RECENT_BARS:]
            ],
            actions=action_rows.get(ticker, [])[-RECENT_BARS:],
            gaps=gaps,
        )
        out[ticker] = detail.payload()
    return out
