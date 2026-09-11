"""What a target sold for, over what it last reported.

The multiple needs two numbers that come from different places: the price, from
the 8-K, and the target's financials, from its own XBRL filings. Joining those
is the whole problem, and it has exactly one honest key.

**The filer must be the target.** An 8-K names its parties by defined term --
"the Buyer", "Parent", "Merger Sub" -- so the target's *name* is the string a
regular expression pulled out of a legal sentence, and matching that to a filer
is the 44.2%-precision mistake the Form 5500 work measured. What is exact is the
filer's own CIK. So this screen reads filings where the filer is *not* the
acquirer, and then confirms from the filing record that the filer was the thing
sold. The prose is used only where it is reliable -- a clear "we agreed to
acquire" excludes a filing -- and never to assert who the target was.

**Read the build-spec section before trusting any number this produces.** The
population is survivor-biased by a factor of thirty, measured 2026-09-10: of
2,160 filers whose 10-K history ends before 2024, **1.7% appear in the deals
table at all**, against 50.7% of the 4,271 still filing. An acquisition target is
by definition a company that stopped filing, so the deals population is missing
almost exactly the rows this screen exists to find -- none of Activision, VMware,
Twitter, Seagen, Slack, Xilinx, Arena or Horizon is in it, though all eight sit
in the XBRL partitions with clean pre-deal histories.

So this screen is correct and its output is ten rows. That is not a bug in the
screen and the funnel says where it went. The constraint is **not** target
financials, which is what the build order assumed: those are fully available and
unbiased. It is the deal population.

**And the filer being on the selling side does not make it the target.**
Measured 2026-09-10 over all 30 loaded quarters, and this is the finding the
screen is arranged around: of 2,112 filings with a stated value and a pre-deal
annual report, **1,778 -- 84% -- filed another 10-K afterwards.** They sold a
division, not themselves. Dividing a
division's price by the whole parent's revenue produces a number that is
present, plausible, and far too small, and nothing about it looks wrong: the
multiple is just low, and low multiples are what a screen for cheap deals is
supposed to find. It would have ranked its own errors first.

So the target's identity is *confirmed from the filing record*, not from prose:
a company acquired whole stops filing. That is the same delisting that makes the
forward-return study survivor-biased, read from the other side -- the absence
that breaks one study is the confirmation for this one.

**"Has not filed yet" is not "stopped filing."** Third time this shape has come
up, after Form 5500's ``pending_years`` against ``gap_years`` and the XBRL nil
tag. A 10-K lands months after its fiscal year, and the loaded partitions end
somewhere, so a recent deal has no "after" to look at. Those are
:data:`TOO_RECENT` and are counted apart from both other answers, because
folding them into "acquired whole" would admit exactly the cases that have not
had time to contradict it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Final

import duckdb

from marketradar.screens import funnel as funnel_mod

log = logging.getLogger(__name__)

SCREEN: Final[str] = "deal_multiples"

#: The one role that disqualifies a filing. A filer that clearly said it was
#: buying is not the target, and that much the prose gets right; every other
#: role is admitted and the filing record decides.
#:
#: Measured across roles, "stopped filing" fires at about 1% everywhere --
#: ``party`` 6 of 579, ``seller`` 5 of 505, ``not_stated`` 0 of 228 -- which is
#: the survivorship problem above rather than a property of the roles.
ACQUIRER_ROLE: Final[str] = "acquirer"

#: De-SPACs have no operating acquirer, no target financials and no computable
#: multiple; securitizations are not acquisitions at all. Both are excluded by
#: the classification the deals loader already made, not re-derived here.
OPERATING_TYPE: Final[str] = "operating"

#: Confirmed: the target filed no annual report after the deal, and enough
#: filing history was loaded afterwards for that to mean something.
ACQUIRED_WHOLE: Final[str] = "acquired_whole"

#: The filer kept filing, so whatever was sold was not the filer. 84% of the
#: population with a pre-deal report. Excluded from every multiple and counted.
KEPT_FILING: Final[str] = "kept_filing"

#: Not enough post-deal coverage to tell. Not a signal in either direction.
TOO_RECENT: Final[str] = "too_recent"

IDENTITIES: Final[tuple[str, ...]] = (ACQUIRED_WHOLE, KEPT_FILING, TOO_RECENT)

#: How much loaded filing history must sit after a deal before an absence of
#: filings is read as "acquired".
#:
#: A 10-K for a fiscal year ending in December is typically filed in the
#: following February or March, and an acquired company's last one can be nearly
#: a year before the deal closes. 18 months is the same lag the Form 5500 work
#: settled on for a different source and for the same reason: below it, absence
#: and lateness are indistinguishable.
CONFIRM_LAG: Final[timedelta] = timedelta(days=548)

#: Concepts this screen divides by. Revenue leads deliberately.
#:
#: Net income is carried and is the weaker denominator -- capital structure and
#: taxes move it between two otherwise comparable targets, and it goes negative,
#: where a multiple stops meaning anything. Operating income would be the
#: standard M&A denominator and is **not available**: it was measured at 91.3%
#: and deferred out of the tag map's v1, so adding it is a map edit and a
#: re-resolve of the range. Named here rather than silently missing.
DENOMINATORS: Final[tuple[str, ...]] = ("revenue", "net_income")


@dataclass(frozen=True, slots=True)
class Multiple:
    """One deal, its target's last reported figures, and the ratio."""

    accession: str
    cik: str
    company: str
    filed_date: date
    value_usd: Any
    consideration: str
    items: str
    #: ``acquired_whole`` | ``kept_filing`` | ``too_recent``
    identity: str
    #: Fiscal period end of the annual report the figures come from, which is
    #: always *before* the deal: point-in-time, which is the whole reason these
    #: come from the Financial Statement Data Sets rather than companyfacts.
    period_end: date | None
    revenue: Any = None
    net_income: Any = None
    #: Which tag supplied each figure. Carried because "net income" is two
    #: different numbers depending on the tag -- see xbrl/tag_map.py.
    tags: dict[str, str] = field(default_factory=dict)
    value_to_revenue: float | None = None
    value_to_net_income: float | None = None

    @property
    def usable(self) -> bool:
        return self.identity == ACQUIRED_WHOLE and self.revenue is not None


@dataclass(frozen=True, slots=True)
class Result:
    rows: list[Multiple]
    funnel: funnel_mod.Funnel
    #: ``{identity: count}`` over the population that had a pre-deal report, so
    #: the 84% is visible rather than inferred from a shrinking list.
    identities: dict[str, int]
    #: The newest period end in the loaded XBRL, which is what bounds
    #: :data:`TOO_RECENT`. Reported because the screen's answer moves when more
    #: quarters are loaded, and a reader has to know where the edge is.
    coverage_to: date | None

    def lines(self) -> list[str]:
        out = list(self.funnel.lines())
        out.append("")
        out.append("target identity, among deals with a pre-deal annual report")
        for name in IDENTITIES:
            out.append(f"  {name:<16} {self.identities.get(name, 0):>6,}")
        if self.coverage_to:
            out.append(f"  filing history loaded to {self.coverage_to}; a deal "
                       f"within {CONFIRM_LAG.days} days of that cannot be "
                       "confirmed either way")
        return out


def screen(
    con: duckdb.DuckDBPyConnection,
    *,
    deals: str = "deals",
    fundamentals: str = "xb",
    today: date | None = None,
) -> Result:
    """Multiples for every deal whose target can be identified exactly.

    ``deals`` and ``fundamentals`` are relation names the caller has already
    put in scope, so this function does no I/O and no manifest resolution --
    the same shape as the volatility screen, and what lets it be tested on a
    handful of rows.
    """
    con.execute(f"""
        create or replace view dm_deals as
        select accession, cik, company, filed_date, value_usd, consideration,
               items, deal_type, filer_role
        from {deals}
    """)
    con.execute(f"""
        create or replace view dm_facts as
        select lpad(cast(cik as varchar), 10, '0') as cik10,
               period_end, concept, value, tag, status
        from {fundamentals}
    """)

    def count(where: str) -> int:
        return int(con.execute(
            f"select count(*) from dm_deals where {where}").fetchone()[0])

    base = f"filer_role <> '{ACQUIRER_ROLE}'"
    priced = f"{base} and value_usd is not null"
    operating = f"{priced} and deal_type = '{OPERATING_TYPE}'"
    stages: list[tuple[str, int, str]] = [
        ("deal candidates", count("true"), "every 8-K the deals loader stored"),
        ("filer may be the target", count(base),
         "a filing that says it is the buyer is out; the rest are decided by "
         "the filing record, not by the prose"),
        ("value stated", count(priced),
         "no price, no multiple; the 8-K states one or it does not"),
        ("operating deal", count(operating),
         "de-SPACs have no target financials and securitizations are not deals"),
    ]

    coverage_to = con.execute(
        "select max(period_end) from dm_facts").fetchone()[0]

    # The target's last annual report *before* the deal, and whether anything
    # came after. Keyed on the filer's own CIK, zero-padded on both sides
    # because EDGAR writes it both ways and a string compare would miss half.
    con.execute(f"""
        create or replace table dm_matched as
        with d as (
            select *, lpad(cast(cik as varchar), 10, '0') as cik10
            from dm_deals where {operating}
        ),
        spans as (
            select d.accession,
                   max(case when f.period_end < d.filed_date
                            then f.period_end end)            as pre_end,
                   max(f.period_end)                          as last_end
            from d join dm_facts f on f.cik10 = d.cik10
            group by d.accession
        )
        select d.*, s.pre_end, s.last_end
        from d left join spans s using (accession)
    """)
    stages.append((
        "target appears in XBRL", int(con.execute(
            "select count(*) from dm_matched where last_end is not null"
        ).fetchone()[0]),
        "the loaded range starts at 2019q1; an older deal cannot match",
    ))
    stages.append((
        "has a pre-deal annual report", int(con.execute(
            "select count(*) from dm_matched where pre_end is not null"
        ).fetchone()[0]),
        "point-in-time: what was knowable when the deal was announced",
    ))

    # Identity, from the filing record rather than from prose.
    con.execute(f"""
        create or replace table dm_identity as
        select m.*,
            case
                when m.pre_end is null then NULL
                when m.last_end > m.pre_end then '{KEPT_FILING}'
                when DATE '{(today or date.today()).isoformat()}'
                     - m.filed_date < {CONFIRM_LAG.days}
                  or DATE '{(coverage_to or date(1900, 1, 1)).isoformat()}'
                     - m.filed_date < {CONFIRM_LAG.days}
                    then '{TOO_RECENT}'
                else '{ACQUIRED_WHOLE}'
            end as identity
        from dm_matched m
    """)
    identities = {
        row[0]: int(row[1]) for row in con.execute(
            "select identity, count(*) from dm_identity "
            "where identity is not null group by 1"
        ).fetchall()
    }
    stages.append((
        "target identity confirmed", identities.get(ACQUIRED_WHOLE, 0),
        f"filed nothing after the deal. {identities.get(KEPT_FILING, 0):,} kept "
        "filing, so they sold a division and their own revenue is the wrong "
        f"denominator; {identities.get(TOO_RECENT, 0):,} are too recent to tell. "
        "This stage collapsing is the survivorship bias in the deal "
        "population, not a filter that is too strict -- see the module "
        "docstring",
    ))

    # The figures, pivoted off the long table -- one column per concept asked
    # for, and a concept nobody asked for costs nothing.
    picks = ", ".join(
        f"max(case when concept = '{c}' and status = 'stated' then value end) "
        f"as {c}, "
        f"max(case when concept = '{c}' and status = 'stated' then tag end) "
        f"as {c}_tag"
        for c in DENOMINATORS
    )
    con.execute(f"""
        create or replace table dm_rows as
        with figures as (
            select i.accession, {picks}
            from dm_identity i
            join dm_facts f
              on f.cik10 = i.cik10 and f.period_end = i.pre_end
            group by i.accession
        )
        select i.*, {", ".join(f"g.{c}, g.{c}_tag" for c in DENOMINATORS)}
        from dm_identity i left join figures g using (accession)
        where i.identity = '{ACQUIRED_WHOLE}'
    """)
    stages.append((
        "revenue resolved on it", int(con.execute(
            "select count(*) from dm_rows where revenue is not null"
        ).fetchone()[0]),
        "revenue is 85.9-91.2% of operating filers, so this is the binding one",
    ))

    rows: list[Multiple] = []
    for row in con.execute(f"""
        select accession, cik, company, filed_date, value_usd, consideration,
               items, identity, pre_end,
               {", ".join(f"{c}, {c}_tag" for c in DENOMINATORS)}
        from dm_rows
        where revenue is not null
        order by filed_date desc, accession
    """).fetchall():
        (accession, cik, company, filed, value, consideration, items,
         identity, pre_end, revenue, revenue_tag, net_income,
         net_income_tag) = row
        rows.append(Multiple(
            accession=accession, cik=str(cik), company=company,
            filed_date=filed, value_usd=value,
            consideration=consideration or "not_stated", items=items or "",
            identity=identity, period_end=pre_end,
            revenue=revenue, net_income=net_income,
            tags={k: v for k, v in (("revenue", revenue_tag),
                                    ("net_income", net_income_tag)) if v},
            value_to_revenue=_ratio(value, revenue),
            value_to_net_income=_ratio(value, net_income),
        ))

    return Result(
        rows=rows,
        funnel=funnel_mod.build(SCREEN, *stages),
        identities=identities,
        coverage_to=coverage_to,
    )


def _ratio(value: Any, denominator: Any) -> float | None:
    """``value / denominator``, or None where the ratio means nothing.

    A non-positive denominator is not a small multiple, it is not a multiple:
    a loss-making target at a positive price gives a negative ratio that sorts
    below every real one, and a zero-revenue target gives infinity. Both are
    returned as absent rather than as numbers, which is the same rule as the
    nil XBRL tag -- no value is not a value of zero.
    """
    if value is None or denominator is None:
        return None
    try:
        den = float(denominator)
        if den <= 0:
            return None
        return float(value) / den
    except (TypeError, ValueError, ZeroDivisionError):
        return None
