"""Daily percent-move screens: gainers and losers, split-adjusted at read time.

Three separations, all for the same reason — a list is only useful if the
things in it are comparable to each other:

**Price bands** (sub-$1, $1-10, $10+). A penny stock moving 40% and a $200
stock moving 40% are not the same event, and unbanded the sub-$1 names win
every day.

**Stocks apart from ETFs.** There are thousands of ETFs in the universe and
the leveraged and inverse ones move 3x by construction. They are not
mispriced, they are doing their job, and mixed into one list they bury every
real stock move. Same argument as the bands, so the same treatment: separate
lists, never a filter. Nothing is dropped; it is sorted into the list where it
can be compared fairly.

**A parallel liquid set** gated on >$5M average dollar volume, run alongside
the unfiltered lists rather than replacing them. The illiquid names are often
the interesting ones — they just cannot be traded, which is a different
question from whether they moved.

Adjustment happens *here*, at query time, never in storage. Raw OHLCV plus a
``corporate_actions`` table keeps year-partitioned Parquet immutable: adjusted
history is retroactively rewritten by every split, so a split today would
silently corrupt ``prices_2019.parquet`` that no nightly job ever touches
again. Applying the factor in the query makes a bad adjustment a fixable bug
rather than a re-download.

Splits are adjusted. Dividends are *flagged, not netted* — an ex-dividend drop
is a real price move and netting it would hide a genuine one-day fall. Pass
``adjust_dividends=True`` to add the cash back for a total-return view.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Final, Iterable, Iterator

import duckdb

from marketradar import manifest, storage
from marketradar.clock import market_today
from marketradar.freshness import assert_fresh

log = logging.getLogger(__name__)

DATASET: Final[str] = "prices_eod_raw"

#: Band edges, on the *pre-move* adjusted price. Banding on the prior close
#: rather than today's keeps a name in the band it started the day in — a
#: stock that ran $0.90 -> $3.00 is a sub-$1 event, not a $1-10 one.
BAND_SUB_DOLLAR: Final[str] = "sub$1"
BAND_LOW: Final[str] = "$1-10"
BAND_HIGH: Final[str] = "$10+"
BANDS: Final[tuple[str, ...]] = (BAND_SUB_DOLLAR, BAND_LOW, BAND_HIGH)

SECURITY_TYPES: Final[tuple[str, ...]] = ("stock", "etf")
DIRECTIONS: Final[tuple[str, ...]] = ("gainers", "losers")

#: SEC Rule 612: the minimum pricing increment is a penny at or above $1.00
#: and $0.0001 below it. This is why percent alone is not enough in the sub-$1
#: band -- $0.0002 -> $0.0003 is +50% and a single tick.
TICK_ABOVE_DOLLAR: Final[str] = "0.01"
TICK_BELOW_DOLLAR: Final[str] = "0.0001"

#: Not a liquidity filter. A price below a penny is a data error or a stale
#: quote, and dividing by it manufactures enormous percentages out of noise.
#: Liquidity is handled separately, by the parallel >$5M set.
SANITY_FLOOR: Final[str] = "0.01"

LIQUID_MIN_DOLLAR_VOLUME: Final[int] = 5_000_000

#: Sessions the average dollar volume is taken over. Thirty is about six
#: trading weeks -- long enough that one unusual print does not move it, short
#: enough to still describe the name as it trades now.
ADV_WINDOW_SESSIONS: Final[int] = 30

#: Sessions a name must have before the liquidity gate will speak for it.
#:
#: The decision, made explicit rather than left to a NULL: a name with fewer
#: sessions than this is **excluded from the gated lists and appears in the
#: ungated ones**. The gate claims "liquid over the last 30 sessions", and a
#: name without 30 sessions has not earned that claim -- one big opening print
#: would otherwise carry a two-day-old listing into the liquid lists.
#:
#: Nothing is hidden by this: every such name is still in the ungated list for
#: its band, and the count of exclusions is reported. Lower it to see the
#: previous behaviour.
MIN_ADV_SESSIONS: Final[int] = 30

#: Longest gap between consecutive bars that still counts as one continuous
#: series. Beyond this the "previous close" is not a previous close, it is a
#: different era -- and after a relisting, often a different company.
#:
#: Thirty days, matching the delisting window the sweep already uses. A weekend
#: is 3, a holiday weekend 4, and an SEC trading suspension runs ten business
#: days (about 14), so 30 clears every legitimate interruption. Real recycles
#: leave holes of months or years: AAAP's was 3,045 days.
MAX_GAP_DAYS: Final[int] = 30

#: A one-session *gain* past this, with no corporate action in the interval,
#: is suppressed as an unrecorded reverse split. 1.0 is +100%.
#:
#: Set where real moves thin out rather than where splits begin: a genuine
#: one-session double exists and would be worth seeing, but corporate_actions
#: is known incomplete -- Tiingo's per-bar splitFactor misses splits outright
#: on small tickers -- and a false +2,388% at the top of a gainer list costs
#: more than a true +150% missing from it.
SUSPECT_JUMP: Final[Decimal] = Decimal("1.00")

#: The fall side is *flagged and counted but still shown*, and the asymmetry
#: is the point.
#:
#: A single threshold on abs(move) is silently jumps-only, because a price
#: cannot fall more than 100% -- so the fall side needs its own, shallower
#: number, and at that depth the two populations overlap. An unadjusted
#: forward split is -50% (2-for-1) to -90% (10-for-1), and real one-session
#: falls of that size are *common* in the bands this screen exists to watch:
#: failed trials, fraud, dilution. Forward splits are also the rarer event
#: here, because a cheap company does a reverse split, not a forward one.
#:
#: So suppressing a deep fall would delete real information most of the time,
#: where suppressing a large jump removes obvious garbage most of the time.
#: Different confidence, different treatment, both counted out loud.
SUSPECT_FALL: Final[Decimal] = Decimal("-0.60")

TOP_N: Final[int] = 20

#: A screen that returns nothing must not exit green. This is the freshness
#: assertion for this stage: the input is real prices, so anything that
#: silently produces an empty screen is a broken join or an empty load.
MIN_MOVES: Final[int] = 1


class ScreenError(RuntimeError):
    """The screen could not be built from the data available."""


@dataclass(frozen=True, slots=True)
class Move:
    """One ticker's one-day move, adjusted."""

    ticker: str
    date: date
    security_type: str
    exchange: str
    band: str
    close: Decimal
    adj_prev_close: Decimal
    pct_move: Decimal
    tick_move: Decimal
    tick_size: Decimal
    volume: int
    dollar_volume: Decimal
    avg_dollar_volume: Decimal
    split_factor: Decimal
    div_cash: Decimal
    is_ex_div: bool
    #: Which listing this bar belongs to, identified by that listing's first
    #: session. None when the bar falls outside every range the vendor knows.
    listing_id: date | None = None
    gap_days: int | None = None
    #: Sessions the average dollar volume was actually taken over. A
    #: three-day mean must not be presentable as a thirty-day one.
    adv_sessions: int = 0

    def liquid(self, min_sessions: int = MIN_ADV_SESSIONS) -> bool:
        """Above the dollar gate, on enough history for the gate to mean it.

        A method rather than a property because the session floor is a
        parameter: presenting a two-day average as a liquidity measure is the
        failure this guards, and how much history is enough is a judgement
        that should be movable without editing the class.
        """
        return (
            self.avg_dollar_volume > LIQUID_MIN_DOLLAR_VOLUME
            and self.adv_sessions >= min_sessions
        )


@dataclass(frozen=True, slots=True)
class ScreenList:
    """One top-N list. The unit the digest renders."""

    security_type: str
    band: str
    direction: str
    liquidity: str  # "all" | "liquid"
    rows: list[Move]

    @property
    def title(self) -> str:
        gate = "" if self.liquidity == "all" else " >$5M ADV"
        return (
            f"{self.security_type}s / {self.band} / {self.direction}{gate}"
        )


def _price_years(as_of: date) -> list[str]:
    """Partitions needed to compute a move on ``as_of``.

    The prior year is included when one exists, because the first trading day
    of January needs December's close and a single-partition read would
    silently drop every ticker's first move of the year.
    """
    years = [str(as_of.year)]
    previous = str(as_of.year - 1)
    try:
        manifest.get(DATASET, previous)
    except Exception:  # unknown partition is normal, not an error
        return years
    return [previous] + years


def read_prices(
    con: duckdb.DuckDBPyConnection, as_of: date | None = None
) -> duckdb.DuckDBPyRelation:
    """Raw prices for the partitions a screen on ``as_of`` needs."""
    # Trading date, not local or UTC date -- the screen runs right after the
    # sweep, in the same window where the three disagree.
    as_of = as_of or market_today()
    rels = [
        storage.read_dataset(DATASET, year, con=con) for year in _price_years(as_of)
    ]
    rel = rels[0]
    for other in rels[1:]:
        rel = rel.union(other)
    return rel


def read_actions(con: duckdb.DuckDBPyConnection) -> duckdb.DuckDBPyRelation:
    """Corporate actions from Postgres.

    Only the four columns the adjustment needs — the screen has no use for
    ``id`` or ``ingested_at`` and there is no reason to drag them across the
    wire for every ticker.
    """
    if not storage.postgres_attached(con):
        raise ScreenError(
            "No Postgres attached, so corporate_actions cannot be read. "
            "Refusing to screen on unadjusted prices: a reverse split reads "
            "as a -95% day and would top every loser list."
        )
    return con.sql(
        "SELECT * FROM postgres_query('pg', "
        "'select ticker, ex_date, split_factor, div_cash from corporate_actions')"
    )


_MOVES_SQL: Final[str] = """
with px as (
    select ticker, date, close, volume, security_type, exchange,
           {listing_expr} as listing_id
    from prices
    where close is not null and volume is not null
),
act as (
    select
        ticker,
        ex_date,
        cast(coalesce(split_factor, 1) as decimal(38,12)) as split_factor,
        cast(coalesce(div_cash, 0) as decimal(18,6))      as div_cash
    from actions
),
seq as (
    select
        px.*,
        lag(close) over w as prev_close,
        lag(date)  over w as prev_date
    from px
    -- By listing, not by ticker. A ticker is not an entity across time: 356
    -- active symbols carry two different companies inside a ten-year pull,
    -- and partitioning by ticker alone makes lag() reach across the gap and
    -- compare one company's close against another's. AAAP did exactly that
    -- and produced a clean, plausible -69.26%.
    window w as (partition by ticker, listing_id order by date)
),
adv as (
    -- A TRAILING window, not the whole partition.
    --
    -- This was `group by ticker, listing_id` over every row read, which is
    -- accidentally recent while the partitions hold a few sessions and
    -- silently becomes a multi-year average the moment history lands. A name
    -- that traded $50M/day in early 2025 and $200k/day now would pass a $5M
    -- gate on its own history. Twelve of the twenty-four lists are gated on
    -- this number, and nothing would have failed.
    --
    -- Still partitioned by listing: averaging across a relisting blends two
    -- companies' liquidity and gates on the blend.
    --
    -- adv_sessions is carried so the gate can say how much history the
    -- average actually had, rather than presenting a three-day mean as if it
    -- were thirty.
    select ticker, listing_id, date,
           cast(avg(cast(close as decimal(38,12)) * volume) over w
                as decimal(38,12)) as avg_dollar_volume,
           count(*) over w as adv_sessions
    from px
    window w as (
        partition by ticker, listing_id order by date
        rows between {adv_window} preceding and current row
    )
),
-- Every action strictly after the previous bar and up to and including this
-- one. A range rather than an equality join: a gap in the price series (a
-- halt, a missed load, a long weekend) must not drop the split that happened
-- inside it, or the move comes out uncorrected.
joined as (
    select
        seq.*,
        cast(coalesce(product(act.split_factor), 1) as decimal(38,12)) as split_factor,
        cast(coalesce(sum(act.div_cash), 0) as decimal(18,6))          as div_cash,
        -- Counted, not inferred from split_factor: an action with a factor of
        -- 1 (a dividend) is not the same fact as no action at all, and the
        -- suspect flag below turns on exactly that difference.
        count(act.ex_date)                                             as n_actions
    from seq
    left join act
        on act.ticker = seq.ticker
       and act.ex_date >  seq.prev_date
       and act.ex_date <= seq.date
    where seq.prev_close is not null
    group by all
),
adjusted as (
    select
        joined.*,
        -- The division that matters. Widened to (38,12) so a 1:10 reverse
        -- split on a $0.0002 quote does not lose the tick, then quantized
        -- back to the storage scale on the way out. DuckDB returns DOUBLE
        -- from decimal division, so the final cast is what re-establishes
        -- exactness at (18,6) -- it is not decoration.
        cast(
            cast(prev_close as decimal(38,12))
            / cast(split_factor as decimal(38,12))
            as decimal(18,6)
        ) as adj_prev_close
    from joined
),
priced as (
    select
        adjusted.*,
        case when adj_prev_close < 1 then cast({tick_low} as decimal(18,6))
             else cast({tick_high} as decimal(18,6)) end as tick_size,
        case when adj_prev_close < 1 then '{band_sub}'
             when adj_prev_close < 10 then '{band_low}'
             else '{band_high}' end as band,
        case when {adjust_dividends} then cast(close as decimal(18,6)) + div_cash
             else cast(close as decimal(18,6)) end as effective_close,
        -- Sanity floor, on both ends of the comparison. Not liquidity: a
        -- sub-penny quote is usually stale or erroneous, and dividing by it
        -- invents enormous percentages out of noise.
        --
        -- Computed, not filtered, on purpose. The floor and the tick-count
        -- column disagree about exactly one case: $0.0002 -> $0.0003 is the
        -- canonical "+50% and one tick" example and it sits below $0.01, so a
        -- silent WHERE would delete the sub-$1 band's whole reason to exist
        -- and nobody would see it happen. The caller filters and reports the
        -- count instead.
        (adj_prev_close >= cast({floor} as decimal(18,6))
         and close      >= cast({floor} as decimal(18,6))) as passes_floor,
        (date - prev_date) as gap_days,
        -- Kept even with listing_id, and deliberately so. It catches a long
        -- halt inside one listing, and it catches a listing range that is
        -- simply wrong -- the vendor's own metadata is the thing listing_id
        -- trusts, and this is what covers being let down by it.
        ((date - prev_date) <= {max_gap}) as within_gap,
        -- A move this large with no corporate action to explain it is almost
        -- certainly an unrecorded split. corporate_actions is known
        -- incomplete: Tiingo's per-bar splitFactor misses splits outright on
        -- small tickers and the Power plan has no corporate-actions endpoint,
        -- so this cannot be fixed by loading harder. PHD went 0.40 to 9.95 on
        -- 2026-09-03 -- a 1-for-25 reverse split, +2,388% in the lists.
        --
        -- Suppressed by the caller and counted, never adjusted by an inferred
        -- ratio: a made-up factor is fabricated data in the column the whole
        -- screen trusts, and it would be indistinguishable from a real one
        -- forever after.
        (n_actions = 0 and
         (effective_close - adj_prev_close) / adj_prev_close
            > {suspect_jump}) as action_suspect,
        -- Flagged, not suppressed. See SUSPECT_FALL.
        (n_actions = 0 and
         (effective_close - adj_prev_close) / adj_prev_close
            < {suspect_fall}) as unexplained_fall
    from adjusted
)
select
    priced.ticker,
    priced.date,
    priced.security_type,
    priced.exchange,
    priced.band,
    cast(priced.close as decimal(18,6))                       as close,
    priced.adj_prev_close,
    cast((priced.effective_close - priced.adj_prev_close)
         / priced.adj_prev_close * 100 as decimal(18,6))      as pct_move,
    cast((priced.effective_close - priced.adj_prev_close)
         / priced.tick_size as decimal(18,6))                 as tick_move,
    priced.tick_size,
    priced.volume,
    cast(cast(priced.close as decimal(38,12)) * priced.volume
         as decimal(38,12))                                   as dollar_volume,
    adv.avg_dollar_volume,
    adv.adv_sessions,
    priced.split_factor,
    priced.div_cash,
    priced.div_cash > 0                                       as is_ex_div,
    priced.listing_id,
    priced.gap_days,
    priced.passes_floor,
    priced.within_gap,
    priced.action_suspect,
    priced.unexplained_fall
from priced
join adv on adv.ticker = priced.ticker
         and adv.listing_id is not distinct from priced.listing_id
         and adv.date = priced.date
"""


def moves(
    con: duckdb.DuckDBPyConnection,
    *,
    as_of: date | None = None,
    prices: duckdb.DuckDBPyRelation | None = None,
    actions: duckdb.DuckDBPyRelation | None = None,
    adjust_dividends: bool = False,
    sanity_floor: str = SANITY_FLOOR,
    adv_window: int = ADV_WINDOW_SESSIONS,
) -> duckdb.DuckDBPyRelation:
    """Every adjusted one-day move available, before ranking or filtering.

    Includes rows below the sanity floor, marked ``passes_floor = false``, so
    the caller can report what the floor removed rather than have it vanish.

    ``prices`` and ``actions`` are injectable so the maths can be tested on a
    handful of hand-built rows. Left unset they resolve through the manifest,
    which is the only way production reads anything.
    """
    prices = read_prices(con, as_of) if prices is None else prices
    actions = read_actions(con) if actions is None else actions

    # Data written before listing_id existed has no such column. Synthesising
    # NULL keeps one code path, and NULL is the honest value: every bar for
    # that ticker lands in a single "unknown listing" group, which is what we
    # actually know. The gap guard is what protects those rows.
    if "listing_id" in prices.columns:
        listing_expr = "listing_id"
    else:
        listing_expr = "cast(null as date)"
        log.warning(
            "prices have no listing_id column; falling back to one unknown "
            "listing per ticker. Moves across a relisting are then caught by "
            "the %d-day gap guard alone.", MAX_GAP_DAYS,
        )

    con.register("prices", prices)
    con.register("actions", actions)

    sql = _MOVES_SQL.format(
        tick_low=TICK_BELOW_DOLLAR,
        tick_high=TICK_ABOVE_DOLLAR,
        band_sub=BAND_SUB_DOLLAR,
        band_low=BAND_LOW,
        band_high=BAND_HIGH,
        floor=sanity_floor,
        adjust_dividends="true" if adjust_dividends else "false",
        listing_expr=listing_expr,
        max_gap=MAX_GAP_DAYS,
        adv_window=max(0, adv_window - 1),
        suspect_jump=SUSPECT_JUMP,
        suspect_fall=SUSPECT_FALL,
    )
    return con.sql(sql)


def latest_date(rel: duckdb.DuckDBPyRelation) -> date | None:
    row = rel.query("m", "select max(date) from m").fetchone()
    return row[0] if row else None


def _to_move(row: tuple[Any, ...]) -> Move:
    return Move(
        ticker=row[0], date=row[1], security_type=row[2], exchange=row[3],
        band=row[4], close=row[5], adj_prev_close=row[6], pct_move=row[7],
        tick_move=row[8], tick_size=row[9], volume=int(row[10]),
        dollar_volume=row[11], avg_dollar_volume=row[12],
        split_factor=row[14], div_cash=row[15], is_ex_div=bool(row[16]),
        listing_id=row[17],
        gap_days=None if row[18] is None else int(row[18]),
        adv_sessions=int(row[13] or 0),
    )


@dataclass(frozen=True, slots=True)
class ScreenResult:
    day: date
    lists: list[ScreenList]
    moves_screened: int
    floor_excluded: int
    sanity_floor: str
    gap_excluded: int = 0
    unattributed: int = 0
    #: Gains suppressed as unrecorded reverse splits. Suppressed rather than
    #: shown, because a known-false +2,388% is worse than a visible absence --
    #: and counted rather than dropped, because a silent absence is worse again.
    action_suspect: int = 0
    #: Deep falls with no action on record. Shown, because at that depth a
    #: real crash and a forward split are indistinguishable and the real ones
    #: are more common. See SUSPECT_FALL.
    unexplained_falls: int = 0
    #: Names above the dollar gate but short of the session floor. Kept
    #: visible: they are in the ungated lists, not deleted.
    thin_history: int = 0
    adv_window: int = ADV_WINDOW_SESSIONS
    min_adv_sessions: int = MIN_ADV_SESSIONS


def screen(
    con: duckdb.DuckDBPyConnection,
    *,
    as_of: date | None = None,
    prices: duckdb.DuckDBPyRelation | None = None,
    actions: duckdb.DuckDBPyRelation | None = None,
    top_n: int = TOP_N,
    adjust_dividends: bool = False,
    min_moves: int = MIN_MOVES,
    sanity_floor: str = SANITY_FLOOR,
    adv_window: int = ADV_WINDOW_SESSIONS,
    min_adv_sessions: int = MIN_ADV_SESSIONS,
) -> ScreenResult:
    """Build every top-N list for one trading day.

    ``as_of`` defaults to the newest date present rather than today, so the
    screen works on a stale local copy without pretending it is current — the
    freshness assertion is what decides whether that copy is acceptable.
    """
    rel = moves(
        con, as_of=as_of, prices=prices, actions=actions,
        adjust_dividends=adjust_dividends, sanity_floor=sanity_floor,
        adv_window=adv_window,
    )
    con.register("raw_moves", rel)
    # action_suspect is suppressed here alongside the floor and the gap
    # guard. The list is what gets read every morning, and a move the action
    # table cannot explain is a number we know to be wrong.
    kept = con.sql(
        "select * from raw_moves "
        "where passes_floor and within_gap and not action_suspect"
    )

    observed = assert_fresh(
        "vol_screen", kept, partition="moves", min_rows=min_moves,
        date_column="date", max_staleness_days=10_000,
        expect_cols=("ticker", "date", "pct_move", "tick_move", "band"),
    )
    day = as_of or observed.max_date
    if day is None:
        raise ScreenError("No dated moves to screen.")

    con.register("kept_moves", kept)
    rows = con.execute("select * from kept_moves where date = ?", [day]).fetchall()
    excluded = con.execute(
        "select count(*) from raw_moves where date = ? and not passes_floor", [day]
    ).fetchone()[0]
    gapped = con.execute(
        "select count(*) from raw_moves where date = ? and passes_floor "
        "and not within_gap", [day]
    ).fetchone()[0]
    unattributed = con.execute(
        "select count(*) from raw_moves where date = ? and listing_id is null",
        [day],
    ).fetchone()[0]
    suspect = con.execute(
        "select count(*) from raw_moves where date = ? and passes_floor "
        "and within_gap and action_suspect", [day]
    ).fetchone()[0]
    deep_falls = con.execute(
        "select count(*) from raw_moves where date = ? and passes_floor "
        "and within_gap and unexplained_fall", [day]
    ).fetchone()[0]

    if not rows:
        raise ScreenError(
            f"No moves on {day.isoformat()} survived the {sanity_floor} sanity "
            f"floor ({excluded:,} were below it). Either the date has no prior "
            "close to compare against, or the floor is set above the whole band."
        )

    everything = [_to_move(r) for r in rows]

    lists: list[ScreenList] = []
    for security_type in SECURITY_TYPES:
        for band in BANDS:
            pool = [
                m for m in everything
                if m.security_type == security_type and m.band == band
            ]
            for liquidity in ("all", "liquid"):
                gated = (
                    pool if liquidity == "all"
                    else [m for m in pool if m.liquid(min_adv_sessions)]
                )
                for direction in DIRECTIONS:
                    # Sign first, then order. Sorting alone is not enough:
                    # on a short list "losers" would just be the gainers in
                    # ascending order, which is how a green day quietly
                    # produces a loser list full of winners.
                    signed = [
                        m for m in gated
                        if (m.pct_move > 0 if direction == "gainers" else m.pct_move < 0)
                    ]
                    ranked = sorted(
                        signed,
                        key=lambda m: m.pct_move,
                        reverse=(direction == "gainers"),
                    )
                    lists.append(
                        ScreenList(
                            security_type=security_type,
                            band=band,
                            direction=direction,
                            liquidity=liquidity,
                            rows=ranked[:top_n],
                        )
                    )
    return ScreenResult(
        day=day,
        lists=lists,
        moves_screened=len(everything),
        floor_excluded=int(excluded),
        sanity_floor=sanity_floor,
        gap_excluded=int(gapped),
        unattributed=int(unattributed),
        action_suspect=int(suspect),
        unexplained_falls=int(deep_falls),
        thin_history=sum(
            1 for m in everything
            if m.avg_dollar_volume > LIQUID_MIN_DOLLAR_VOLUME
            and m.adv_sessions < min_adv_sessions
        ),
        adv_window=adv_window,
        min_adv_sessions=min_adv_sessions,
    )


def caveats(result: "ScreenResult") -> list[str]:
    """What the screen dropped, and why. One list, three renderers.

    These lived inline in each renderer, which is exactly how thin_history
    ended up printed by `mr screens` and by neither the digest nor the
    dashboard: a number was added in one place and two copies quietly stayed
    behind. Anything that qualifies the lists belongs here.
    """
    out: list[str] = []
    if result.floor_excluded:
        out.append(
            f"{result.floor_excluded:,} moves below the ${result.sanity_floor} "
            "sanity floor (sub-penny quotes)"
        )
    if result.gap_excluded:
        out.append(
            f"{result.gap_excluded:,} moves spanning a gap of more than "
            f"{MAX_GAP_DAYS} days -- a relisting or a long halt, where the "
            "prior close is not comparable"
        )
    if result.thin_history:
        out.append(
            f"{result.thin_history:,} names cleared the $5M gate on fewer than "
            f"{result.min_adv_sessions} sessions and stayed in the ungated "
            "lists -- a short average is not a liquidity measure"
        )
    if result.unattributed:
        out.append(
            f"{result.unattributed:,} bars carry no listing_id, falling "
            "outside every listing period the vendor knows about"
        )
    if result.action_suspect:
        n = result.action_suspect
        out.append(
            f"{n:,} gain{'' if n == 1 else 's'} suppressed: above "
            f"{SUSPECT_JUMP:.0%} with no corporate action on record, so "
            f"almost certainly an unrecorded reverse split. "
            f"`mr actions-audit` lists {'it' if n == 1 else 'them'}; the "
            "ratio is deliberately not inferred"
        )
    if result.unexplained_falls:
        n = result.unexplained_falls
        out.append(
            f"{n:,} fall{'' if n == 1 else 's'} below {SUSPECT_FALL:.0%} "
            f"carry no corporate action and {'is' if n == 1 else 'are'} "
            "still shown -- at that depth a real collapse and a forward "
            "split look the same, and the real ones are more common"
        )
    return out


def _fmt_money(value: Decimal) -> str:
    v = float(value)
    if v >= 1_000_000_000:
        return f"{v / 1_000_000_000:.1f}B"
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v / 1_000:.0f}K"
    return f"{v:.0f}"


def render(result: ScreenResult, *, show_empty: bool = False) -> Iterator[str]:
    """The lists as text. Empty ones are named, not silently skipped."""
    lists = list(result.lists)
    yield f"volatility screen for {result.day.isoformat()}"
    yield (
        f"{result.moves_screened:,} moves screened, "
        f"{sum(len(l.rows) for l in lists):,} rows across {len(lists)} lists"
    )
    for line in caveats(result):
        yield line

    for sl in lists:
        if not sl.rows and not show_empty:
            continue
        yield ""
        yield f"--- {sl.title} ---"
        if not sl.rows:
            yield "    (empty)"
            continue
        yield (
            f"    {'ticker':<10} {'pct':>9}  {'ticks':>10}  "
            f"{'prev':>11} {'close':>11}  {'adv':>8}  flags"
        )
        for m in sl.rows:
            flags = []
            if m.split_factor != 1:
                flags.append(f"split x{m.split_factor.normalize()}")
            if m.is_ex_div:
                flags.append(f"ex-div {m.div_cash.normalize()}")
            yield (
                f"    {m.ticker:<10} {m.pct_move:>8.2f}% {m.tick_move:>10.1f}  "
                f"{m.adj_prev_close:>11} {m.close:>11}  "
                f"{_fmt_money(m.avg_dollar_volume):>8}  {', '.join(flags)}"
            )


def summary(lists: Iterable[ScreenList]) -> Iterator[str]:
    """One line per list. Shows what is empty without printing 24 headings."""
    yield f"{'list':<44} {'rows':>5}"
    for sl in lists:
        yield f"{sl.title:<44} {len(sl.rows):>5}"
