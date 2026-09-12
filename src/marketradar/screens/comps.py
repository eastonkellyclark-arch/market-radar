"""Peer sets from XBRL: same industry, same size, and honest about both.

A comp set is an assertion that these companies are alike enough for one's ratio
to say something about another's. Everything here is arranged around the fact that
the assertion is usually weaker than it looks, and that the weakness is invisible
in the output -- a median over eleven peers and a median over eleven *unrelated*
peers are both just a number.

**Three things were measured before any of this was written.** 2026-09-12, over
6,288 operating filers with a SIC and assets, one latest annual observation each.

1. **The SIC digit buys nothing measurable, and the size band does the work.**
   On the 2,441 filers that clear eight peers at *all three* depths -- the only
   population where the numbers compare -- median within-set asset-turnover IQR is
   0.432 at 4-digit, 0.438 at 3-digit, 0.492 at 2-digit. Net margin runs the other
   way, 0.613 / 0.591 / 0.538. Neither is a cliff. What does move the number is
   the band: margin IQR is 0.956 with no band and 0.613 inside a 3x one.

   The first version of that measurement said 2-digit sets were dramatically
   *tighter*, because only 2,598 filers qualify at 4-digit against 4,544 at
   2-digit and the 4-digit survivors cluster in the dense codes. Same mistake as
   the XBRL span metric: different populations, one number.

2. **So the depth is a ladder, not a setting** -- narrowest that clears the floor,
   4 then 3 then 2 -- and :attr:`PeerSet.sic_depth` rides on every row, exactly
   as the resolved tag rides on every fundamentals row. The evidence says the
   narrowing is not measurably better; it is still the defensible default, and
   recording it is what lets a consumer disagree.

3. **Net margin is unusable where revenue is near zero.** SIC 2834,
   pharmaceutical preparations, is 793 filers -- 12.3% of the universe -- and 53%
   of them report under $1M of revenue. Its within-code margin IQR is 15.2 with a
   *median* of -1.7. Those are pre-revenue biotechs, and a ratio with a near-zero
   denominator is arithmetic rather than a comparable. :data:`SIMILARITY` is asset
   turnover, which has assets underneath it and every filer has assets.

**The materiality floor is a correctness floor, not a similarity floor**, and the
measurement is what separates those. Raising it from nothing to $50M tightens
turnover IQR only from 0.439 to 0.346 while dropping universe coverage from 82.6%
to 53.9% -- a bad trade bought on similarity alone. What it actually prevents is a
peer with $200k of revenue and $50M of assets contributing a 250x multiple that is
a rounding artifact with a valuation's units. So the default is deliberately low
(:data:`MATERIAL_REVENUE`, $1M), which is nearly free -- 0.439 to 0.434 -- and the
higher floors are reported rather than imposed.

**And a degraded set says so on both axes.** A set that fell back to 2-digit *and*
lost half its members to the materiality floor is twice removed from what the
caller asked for, and a clean median hides both. Measured at the $10M floor: 100
of 3,467 served filers lose more than half their banded peers, and 25 are degraded
on both axes at once. Rare -- which is the point. The honesty machinery costs
almost nothing and the 25 rows are precisely the ones a reader would be misled by.

No prices anywhere in here. This produces peer *sets* and fundamental ratios; a
market multiple needs a market cap, which XBRL does not carry and yfinance must
not supply for anything historical.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Final

import duckdb

from marketradar.screens import funnel as funnel_mod

log = logging.getLogger(__name__)

SCREEN: Final[str] = "comps"

#: SIC depths tried in order, narrowest first. Four digits is "pharmaceutical
#: preparations", two is "chemicals and allied products".
SIC_DEPTHS: Final[tuple[int, ...]] = (4, 3, 2)

#: Multiplicative half-width of the size band: 3.0 admits peers from a third the
#: size to three times it. Keyed on **assets**, not revenue -- assets is stated on
#: 97.8% of operating filers against revenue's 81.1%, and a revenue-keyed band
#: would silently drop the pre-revenue population rather than reporting it.
SIZE_BAND: Final[float] = 3.0

#: Concept the size band is measured on. Not configurable by accident: the band
#: is the thing doing the similarity work, so what it keys on is a decision.
SIZE_CONCEPT: Final[str] = "assets"

#: Peers needed before a median means anything. Eight rather than three because a
#: median over three peers is two numbers and a tiebreak; eight costs 13 points of
#: coverage against three and that cost is flat across every materiality floor
#: measured (94.6% -> 85.7% at no floor, 95.8% -> 86.8% at $1M, 96.2% -> 86.8% at
#: $10M), so the floor and the count are close to independent.
MIN_PEERS: Final[int] = 8

#: Revenue a peer must report to count toward a revenue-denominated comparison.
#: A correctness floor -- see the module docstring on why it is not a similarity
#: floor and why it is set low.
MATERIAL_REVENUE: Final[int] = 1_000_000

#: The ratio used to describe how alike a set is. **Asset turnover, not net
#: margin.** Margin's denominator is the thing half the biggest SIC code does not
#: have.
SIMILARITY: Final[str] = "asset_turnover"

#: A set that lost more than this share of its banded peers to the materiality
#: floor is flagged. Half: the point where the set the caller is shown is mostly
#: not the set the band selected.
THINNED_SHARE: Final[float] = 0.5

# --- why a filer has no peer set ---------------------------------------

#: It has a peer set at :data:`MIN_PEERS` or better.
SERVED: Final[str] = "served"

#: No SIC, or no size figure, so it cannot even be placed in a group. Distinct
#: from having been placed and found alone.
UNPLACEABLE: Final[str] = "unplaceable"

#: The filer itself does not clear the materiality floor. Its peers are not the
#: problem and raising the depth will not help.
IMMATERIAL: Final[str] = "immaterial"

#: Placed, material, and still short of :data:`MIN_PEERS` at two digits. The end
#: of the ladder: there is no coarser industry code to fall back to.
TOO_FEW_PEERS: Final[str] = "too_few_peers"

OUTCOMES: Final[tuple[str, ...]] = (
    SERVED, UNPLACEABLE, IMMATERIAL, TOO_FEW_PEERS,
)


@dataclass(frozen=True, slots=True)
class PeerSet:
    """One filer's peers, with everything needed to distrust the median."""

    cik: str
    company: str
    sic: int | None
    #: Which SIC depth the ladder settled on. 4 is the ask; 3 and 2 are
    #: fallbacks, and a consumer that treats them alike is choosing to.
    sic_depth: int | None
    #: Peers inside the SIC group and the size band, before materiality.
    peers_banded: int
    #: Of those, the ones clearing :data:`MATERIAL_REVENUE`. This is the set the
    #: ratios below are computed over.
    peers_material: int
    outcome: str

    #: Median of the peer set's asset turnover, and its interquartile range. The
    #: IQR is the honest half: it says how alike the set it just averaged is.
    turnover_median: float | None = None
    turnover_iqr: float | None = None
    #: The filer's own turnover, for comparison against its peers'.
    turnover_self: float | None = None

    revenue: Any = None
    assets: Any = None

    @property
    def depth_fell_back(self) -> bool:
        return self.sic_depth is not None and self.sic_depth < SIC_DEPTHS[0]

    @property
    def thinned(self) -> bool:
        """Did the materiality floor take most of the band's selection?"""
        if not self.peers_banded:
            return False
        return self.peers_material < self.peers_banded * (1 - THINNED_SHARE)

    @property
    def degraded(self) -> tuple[str, ...]:
        """Every axis this set is compromised on, not the worst one.

        Both, because they are different compromises and a reader needs to know
        which applies: a fallback means the industry is broader than asked for, a
        thinned set means the members are fewer than the band selected. Reporting
        only one would make the 25 doubly-degraded sets look singly degraded.
        """
        out = []
        if self.depth_fell_back:
            out.append(f"sic_{self.sic_depth}digit")
        if self.thinned:
            out.append("thinned_by_materiality")
        return tuple(out)

    @property
    def usable(self) -> bool:
        return self.outcome == SERVED and self.turnover_median is not None

    def caveat(self) -> str | None:
        """One line a panel can render beside the median, or None."""
        if not self.degraded:
            return None
        parts = []
        if self.depth_fell_back:
            parts.append(f"industry widened to {self.sic_depth}-digit SIC")
        if self.thinned:
            parts.append(f"{self.peers_banded - self.peers_material} of "
                         f"{self.peers_banded} peers below the "
                         f"${MATERIAL_REVENUE / 1e6:g}M revenue floor")
        return "; ".join(parts)


@dataclass(frozen=True, slots=True)
class Result:
    rows: list[PeerSet]
    funnel: funnel_mod.Funnel
    #: ``{outcome: count}`` over every candidate, so what was dropped is visible
    #: rather than inferred from a shorter list.
    outcomes: dict[str, int]
    #: ``{depth: count}`` over served sets. The shape of the ladder's own
    #: answer, which is the thing the measurement said to record.
    depths: dict[int, int]
    #: Coverage at the floors that were *not* chosen, so the default is
    #: visibly a choice. ``{(material, min_peers): served}``.
    alternatives: dict[tuple[int, int], int] = field(default_factory=dict)

    def lines(self) -> list[str]:
        out = list(self.funnel.lines())
        out.append("")
        out.append("why a filer has no peer set")
        total = sum(self.outcomes.values()) or 1
        for name in OUTCOMES:
            got = self.outcomes.get(name, 0)
            out.append(f"  {name:<16} {got:>6,}  {got / total * 100:5.1f}%")
        out.append("")
        out.append(f"SIC depth the ladder settled on (narrowest clearing "
                   f"{MIN_PEERS} peers)")
        for depth in SIC_DEPTHS:
            got = self.depths.get(depth, 0)
            served = sum(self.depths.values()) or 1
            mark = "  <- asked for" if depth == SIC_DEPTHS[0] else ""
            out.append(f"  {depth}-digit        {got:>6,}  "
                       f"{got / served * 100:5.1f}%{mark}")
        served_rows = [r for r in self.rows if r.outcome == SERVED]
        widened = [r for r in served_rows if r.depth_fell_back]
        thinned = [r for r in served_rows if r.thinned]
        both = [r for r in served_rows if len(r.degraded) > 1]
        out.append("")
        out.append("how the served sets are degraded, per axis")
        out.append(f"  industry widened   {len(widened):>6,}  the ladder fell "
                   "past 4-digit SIC")
        out.append(f"  thinned            {len(thinned):>6,}  over "
                   f"{THINNED_SHARE:.0%} of the banded peers are below the "
                   "revenue floor")
        out.append(f"  both               {len(both):>6,}  twice removed from "
                   "what was asked for")
        if both:
            out.append("  the `both` row is the one a clean median hides: a "
                       "widened industry and a thinned set are different")
            out.append("  compromises, and reporting only the worse one makes "
                       "these look singly degraded")
        clean = len(served_rows) - len(widened) - len(thinned) + len(both)
        out.append(f"  clean              {clean:>6,}  4-digit SIC, set intact")
        if self.alternatives:
            out.append("")
            out.append("coverage at floors that were not chosen")
            for (material, peers), got in sorted(self.alternatives.items()):
                label = "none" if not material else f"${material / 1e6:g}M"
                out.append(f"  revenue >= {label:<6} and >= {peers:>2} peers: "
                           f"{got:>6,} served")
        return out


def _latest_sql(fundamentals: str) -> str:
    """One row per filer: its most recent annual figures, widened.

    ``row_number`` over an explicit ordering rather than ``any_value`` over the
    group. The determinism rule applies here exactly as it does in a loader: the
    peer set has to be the same on two runs over the same partitions, and "the
    filing that happened to come back first" is a coin flip that would change
    which companies are peers.
    """
    return f"""
    with ranked as (
        select cik, company, sic, concept, value, status,
               row_number() over (
                   partition by cik, concept
                   order by period_end desc, filed desc, adsh desc) as rn
        from {fundamentals}
        where sic_class = 'operating'
    )
    select cik,
           max_by(company, cik) as company,
           max_by(sic, cik)     as sic,
           max(case when concept = 'revenue' and status = 'stated'
                    then value end) as revenue,
           max(case when concept = 'assets'  and status = 'stated'
                    then value end) as assets
    from ranked where rn = 1
    group by cik
    """


def _peer_table(con: duckdb.DuckDBPyConnection, depth: int, *,
                material: int, band: float) -> str:
    """Peers per filer at one SIC depth, banded and split by materiality.

    Both counts come back from one pass: the banded set is what the industry and
    size rules selected, the material set is what survives the correctness floor,
    and the gap between them is the thing :attr:`PeerSet.thinned` reports.
    """
    div = 10 ** (SIC_DEPTHS[0] - depth)
    name = f"_comps_peers_{depth}"
    con.execute(f"drop table if exists {name}")
    con.execute(f"""
    create temp table {name} as
    select a.cik,
           count(*)                                              as peers_banded,
           count(*) filter (where p.revenue > {material})          as peers_material,
           median(p.turnover) filter (where p.revenue > {material})
                                                                  as turnover_median,
           (quantile_cont(p.turnover, 0.75)
              filter (where p.revenue > {material}))
             - (quantile_cont(p.turnover, 0.25)
              filter (where p.revenue > {material}))               as turnover_iqr
    from _comps_base a
    join _comps_base p
      on p.sic // {div} = a.sic // {div}
     and p.cik <> a.cik
     and p.{SIZE_CONCEPT} between a.{SIZE_CONCEPT} / {band}
                              and a.{SIZE_CONCEPT} * {band}
    group by a.cik
    """)
    return name


def screen(
    con: duckdb.DuckDBPyConnection,
    *,
    fundamentals: str = "xb",
    material_revenue: int = MATERIAL_REVENUE,
    min_peers: int = MIN_PEERS,
    band: float = SIZE_BAND,
    alternatives: bool = True,
) -> Result:
    """Peer sets for every operating filer, with the ladder's answer recorded.

    ``fundamentals`` is a view or table with the resolved XBRL schema. Money stays
    in SQL as ``DECIMAL`` throughout; the only floats are the turnover ratios,
    which are ratios and never money.
    """
    con.execute("drop table if exists _comps_base")
    con.execute(f"""
    create temp table _comps_base as
    select cik, company, sic, revenue, {SIZE_CONCEPT},
           case when {SIZE_CONCEPT} > 0 and revenue is not null
                then cast(revenue as double) / cast({SIZE_CONCEPT} as double)
           end as turnover
    from ({_latest_sql(fundamentals)})
    """)

    candidates = int(con.execute(
        "select count(*) from _comps_base").fetchone()[0])
    placeable = int(con.execute(
        f"select count(*) from _comps_base "
        f"where sic is not null and {SIZE_CONCEPT} > 0").fetchone()[0])
    material = int(con.execute(
        f"select count(*) from _comps_base where sic is not null "
        f"and {SIZE_CONCEPT} > 0 and revenue > {material_revenue}").fetchone()[0])

    for depth in SIC_DEPTHS:
        _peer_table(con, depth, material=material_revenue, band=band)

    joins = "\n".join(
        f"left join _comps_peers_{d} p{d} on p{d}.cik = b.cik"
        for d in SIC_DEPTHS)
    # The ladder, as a CASE per column rather than a correlated subquery: the
    # first depth clearing the floor wins, and every column is taken from that
    # same depth so the counts and the ratios cannot come from different sets.
    def pick(column: str) -> str:
        arms = " ".join(
            f"when coalesce(p{d}.peers_material, 0) >= {min_peers} "
            f"then p{d}.{column}"
            for d in SIC_DEPTHS)
        return f"case {arms} end"

    depth_arms = " ".join(
        f"when coalesce(p{d}.peers_material, 0) >= {min_peers} then {d}"
        for d in SIC_DEPTHS)

    rows = con.execute(f"""
    select b.cik, b.company, b.sic,
           case {depth_arms} end                     as sic_depth,
           {pick('peers_banded')}                    as peers_banded,
           {pick('peers_material')}                  as peers_material,
           {pick('turnover_median')}                 as turnover_median,
           {pick('turnover_iqr')}                    as turnover_iqr,
           b.turnover                                as turnover_self,
           b.revenue, b.{SIZE_CONCEPT}
    from _comps_base b
    {joins}
    order by b.cik
    """).fetchall()

    out: list[PeerSet] = []
    outcomes: dict[str, int] = {name: 0 for name in OUTCOMES}
    depths: dict[int, int] = {d: 0 for d in SIC_DEPTHS}
    for (cik, company, sic, depth, banded, mat, t_med, t_iqr, t_self,
         revenue, size) in rows:
        if sic is None or not size or size <= 0:
            outcome = UNPLACEABLE
        elif revenue is None or revenue <= material_revenue:
            # The filer itself is below the floor. Said separately from
            # `too_few_peers` because no amount of widening the industry fixes
            # it, and the two would otherwise be one indistinguishable bucket.
            outcome = IMMATERIAL
        elif depth is None:
            outcome = TOO_FEW_PEERS
        else:
            outcome = SERVED
            depths[int(depth)] += 1
        outcomes[outcome] += 1
        out.append(PeerSet(
            cik=cik, company=company, sic=sic,
            sic_depth=int(depth) if depth is not None else None,
            peers_banded=int(banded or 0), peers_material=int(mat or 0),
            outcome=outcome,
            turnover_median=float(t_med) if t_med is not None else None,
            turnover_iqr=float(t_iqr) if t_iqr is not None else None,
            turnover_self=float(t_self) if t_self is not None else None,
            revenue=revenue, assets=size,
        ))

    served = outcomes[SERVED]
    stages: list[tuple[str, int, str]] = [
        ("operating filers", candidates,
         "one latest annual observation each, financials excluded at load"),
        ("placeable", placeable,
         f"a SIC to group on and {SIZE_CONCEPT} to band on"),
        ("material", material,
         f"revenue over ${material_revenue / 1e6:g}M -- a correctness floor, "
         "so a near-zero denominator cannot become a multiple"),
        ("peer set found", served,
         f"at least {min_peers} material peers inside a {band:g}x "
         f"{SIZE_CONCEPT} band, at 4-, 3- or 2-digit SIC"),
    ]

    alts: dict[tuple[int, int], int] = {}
    if alternatives:
        alts = _alternatives(con, band=band, chosen=(material_revenue,
                                                     min_peers))

    result = Result(rows=out, funnel=funnel_mod.build(SCREEN, *stages),
                    outcomes=outcomes, depths=depths, alternatives=alts)
    for line in result.lines():
        log.debug("%s", line)
    return result


#: Floors reported alongside the chosen one. Not a tuning knob: the point is that
#: the default is visibly a choice with a cost, the way a concept's coverage
#: number sits beside the concept.
ALTERNATIVE_FLOORS: Final[tuple[tuple[int, int], ...]] = (
    (0, 8), (1_000_000, 8), (10_000_000, 8), (50_000_000, 8),
    (1_000_000, 3), (1_000_000, 12),
)


def _alternatives(con: duckdb.DuckDBPyConnection, *, band: float,
                  chosen: tuple[int, int]) -> dict[tuple[int, int], int]:
    """How many filers each unchosen floor would serve.

    Cheap -- it reuses ``_comps_base`` and counts, rather than building the sets
    again -- and it is the difference between a default and an unexamined
    constant.
    """
    out: dict[tuple[int, int], int] = {}
    for material, peers in sorted({*ALTERNATIVE_FLOORS, chosen}):
        arms = []
        for depth in SIC_DEPTHS:
            div = 10 ** (SIC_DEPTHS[0] - depth)
            arms.append(f"""
            select a.cik from _comps_base a join _comps_base p
              on p.sic // {div} = a.sic // {div} and p.cik <> a.cik
             and p.{SIZE_CONCEPT} between a.{SIZE_CONCEPT} / {band}
                                      and a.{SIZE_CONCEPT} * {band}
            where a.sic is not null and a.{SIZE_CONCEPT} > 0
              and a.revenue > {material} and p.revenue > {material}
            group by a.cik having count(*) >= {peers}
            """)
        got = int(con.execute(
            "select count(*) from (" + " union ".join(arms) + ")"
        ).fetchone()[0])
        out[(material, peers)] = got
    return out
