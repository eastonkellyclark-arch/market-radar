"""Find price moves that look like unrecorded corporate actions.

This is the check that would have caught the defect it exists because of.
``corporate_actions`` held 365 splits where the staged parquet held 3,724 --
the upsert reported success having written a tenth of its rows -- and nothing
noticed for as long as the only evidence was a number the loader chose to
print. AYTU's 1-for-20 reverse split of 2023-01-06 sat in the price history
reading as a genuine +1,751% move, in a table the volatility screens adjust
from every morning.

The detector needs no knowledge of the loader. **A split leaves a signature
in the prices themselves**: a one-session move far larger than any real one,
with no corporate action in the interval that would explain it. So this
compares the price series against the action table and reports the
disagreement, which catches a missing split whatever the reason it is
missing -- a broken upsert, a vendor gap, a ticker the sweep skipped.

Direction matters and the two are reported apart:

    a jump   (close / prev >> 1) is a candidate *reverse* split, and left
             unadjusted it corrupts the gainer lists
    a fall   (close / prev << 1) is a candidate *forward* split, and left
             unadjusted it corrupts the loser lists

The fall side is noisier: a 2-for-1 forward split is -50%, and real stocks
fall 50% in a session. The jump side separates cleanly -- reverse splits run
1-for-5 and deeper, so +400% and up -- which is why the jump count is the one
worth waking up for.

Two guards, both borrowed from the volatility screens for the same reasons:
a $0.01 floor, because dividing by a sub-penny quote invents percentages out
of noise, and a 30-day gap guard, because a "move" across a three-year hole
in the series is not a move.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Final

import duckdb

log = logging.getLogger(__name__)

#: A one-session gain past this, with no action on record, is a candidate
#: reverse split. The shallowest reverse split in common use is 1-for-2
#: (+100%); real one-session doubles exist but are rare enough to read.
JUMP_THRESHOLD: Final[Decimal] = Decimal("1.00")

#: A one-session fall past this is a candidate forward split. Deliberately
#: deeper than the jump threshold: a 2-for-1 split and a bad earnings night
#: look identical at -50%, so this is set where real falls thin out.
FALL_THRESHOLD: Final[Decimal] = Decimal("-0.60")

#: Matches the screens. Below it, a percentage is noise about a stale quote.
PRICE_FLOOR: Final[Decimal] = Decimal("0.01")

#: Matches the screens' gap guard. A move across a longer hole is not a move.
MAX_GAP_DAYS: Final[int] = 30

#: Extra history kept when scanning a recent window, so the first session in
#: that window still has a previous close to compare against. Wider than the
#: gap guard, so the guard decides what counts rather than the slice does.
LOOKBACK_BUFFER_DAYS: Final[int] = 45


@dataclass(frozen=True, slots=True)
class Candidate:
    ticker: str
    date: date
    prev_date: date
    prev_close: Decimal
    close: Decimal
    ratio: Decimal
    direction: str          # 'jump' | 'fall'

    @property
    def implied_split(self) -> str:
        """The split ratio this move would imply, as a readable string."""
        if self.ratio == 0:
            return "?"
        if self.direction == "jump":
            return f"1-for-{self.ratio:.4g}"
        inverse = Decimal(1) / self.ratio
        return f"{inverse:.4g}-for-1"


_SQL: Final[str] = """
with px as (
    select ticker, date, cast(close as decimal(38,12)) as close
    from prices
    where close is not null and close >= {floor}
      {since_clause}
),
seq as (
    select ticker, date, close,
           lag(close) over w as prev_close,
           lag(date)  over w as prev_date
    from px
    window w as (partition by ticker order by date)
),
act as (select ticker, ex_date from actions),
-- Every action strictly after the previous bar and up to this one. The same
-- range join the screens use: an equality join would miss a split that
-- happened inside a gap in the series.
joined as (
    select seq.ticker, seq.date, seq.prev_date, seq.close, seq.prev_close,
           count(a.ex_date) as n_actions
    from seq
    left join act a
        on a.ticker = seq.ticker
       and a.ex_date >  seq.prev_date
       and a.ex_date <= seq.date
    where seq.prev_close is not null
      and seq.prev_close >= {floor}
      and date_diff('day', seq.prev_date, seq.date) <= {max_gap}
    group by all
),
scored as (
    select ticker, date, prev_date, prev_close, close, n_actions,
           (close / prev_close) - 1 as move
    from joined
)
select ticker, date, prev_date,
       cast(prev_close as decimal(18,6)) as prev_close,
       cast(close as decimal(18,6)) as close,
       cast(close / prev_close as decimal(18,6)) as ratio,
       case when move > 0 then 'jump' else 'fall' end as direction
from scored
where n_actions = 0
  and (move >= {jump} or move <= {fall})
order by abs(move) desc
"""


def candidates(
    con: duckdb.DuckDBPyConnection,
    prices: duckdb.DuckDBPyRelation,
    actions: duckdb.DuckDBPyRelation,
    *,
    jump: Decimal = JUMP_THRESHOLD,
    fall: Decimal = FALL_THRESHOLD,
    floor: Decimal = PRICE_FLOOR,
    max_gap_days: int = MAX_GAP_DAYS,
    since: date | None = None,
) -> duckdb.DuckDBPyRelation:
    """Sessions whose move no corporate action explains.

    ``since`` restricts the scan to recent sessions, which is what the
    nightly health check wants: the all-time backlog is a fixed number until
    somebody works through it, while a *new* unexplained move means a split
    happened last night and the screens are about to report it as a gain.
    Enough prior history is kept for the first session in the window to have
    something to compare against.
    """
    con.register("prices", prices)
    con.register("actions", actions)
    clause = ""
    if since is not None:
        start = since - timedelta(days=LOOKBACK_BUFFER_DAYS)
        clause = f"and date >= date '{start.isoformat()}'"
    sql = _SQL.format(
        jump=jump, fall=fall, floor=floor, max_gap=int(max_gap_days),
        since_clause=clause,
    )
    if since is None:
        return con.sql(sql)
    con.register("cand_all", con.sql(sql))
    return con.sql(
        f"select * from cand_all where date >= date '{since.isoformat()}'")


def summarize(
    con: duckdb.DuckDBPyConnection, found: duckdb.DuckDBPyRelation
) -> dict[str, Any]:
    """Counts for the health block. Cheap enough to run nightly."""
    con.register("cand", found)
    row = con.execute("""
        select count(*) as total,
               count(*) filter (where direction = 'jump') as jumps,
               count(*) filter (where direction = 'fall') as falls,
               count(distinct ticker) as tickers,
               max(date) as newest
        from cand
    """).fetchone()
    return {
        "total": int(row[0]), "jumps": int(row[1]), "falls": int(row[2]),
        "tickers": int(row[3]), "newest": row[4],
    }


def health_line(stats: dict[str, Any]) -> str:
    """One ASCII line for the digest. The console here is cp1252."""
    if not stats["total"]:
        return "actions: no unexplained moves; every large move has an action"
    return (
        f"actions: {stats['jumps']:,} unexplained jumps and "
        f"{stats['falls']:,} falls across {stats['tickers']:,} tickers "
        "-- candidate missing splits"
    )


def render(rows: list[tuple], limit: int = 25) -> str:
    """Plain text listing, ASCII only."""
    if not rows:
        return "No unexplained moves. Every large single-session move has a\n" \
               "corporate action that accounts for it."
    out = [
        f"{'ticker':<8} {'date':<11} {'prev':>12} {'close':>12} "
        f"{'move':>10}  implies",
        "-" * 68,
    ]
    for r in rows[:limit]:
        cand = Candidate(
            ticker=r[0], date=r[1], prev_date=r[2], prev_close=r[3],
            close=r[4], ratio=r[5], direction=r[6],
        )
        move = (cand.ratio - 1) * 100
        out.append(
            f"{cand.ticker:<8} {cand.date} {cand.prev_close:>12,.4f} "
            f"{cand.close:>12,.4f} {move:>9,.0f}%  {cand.implied_split}"
        )
    if len(rows) > limit:
        out.append(f"... and {len(rows) - limit:,} more")
    return "\n".join(out)
