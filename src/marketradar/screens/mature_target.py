"""Old private companies whose headcount has stopped growing.

The deal-sourcing thesis this screen encodes is deliberately narrow: a
business that has existed for decades, employs enough people to be worth
acquiring, is not listed, and is not adding staff. Age without a growth story
is the shape that produces a seller -- succession, fatigue, a founder in their
sixties -- and Form 5500 is the only free source that carries all four facts
about a private employer.

**Every input here is a bound, not a measurement, and the screen says so.**

*Age is a floor.* ``oldest_plan_eff`` is the effective date of the oldest
plan the sponsor still files. A company founded in 1971 that started its
401(k) in 1985 reads as 1985, and one that terminated its original plan and
started a new one in 2019 reads as 2019. So the age column is "at least this
old", never "this old", and the screen can only ever *understate* age. That
direction is the safe one for a screen looking for old companies -- it hides
targets, it does not invent them -- but a rank order built on it is a rank
order of plan history, not of incorporation dates. State SoS or UCC filings
would give the real number; that is bulk state data this project has not
touched, and until it does this is the proxy.

*Headcount is a range, and the obvious column is not headcount at all.*
``TOT_PARTCP_BOY_CNT`` is *total participants*, which counts retirees and
separated ex-employees who still hold a balance alongside current staff --
78% of the main form's participants are active, and for an old institution
it is far worse: Boca Raton Regional Hospital reports 934 participants and
388 active, J M Smith 890 and 362. A trend on the total measures a pension
plan paying people out, which is exactly what an old employer does whether
or not it is shrinking, and the first version of this screen ranked on it
and surfaced hospitals, universities and charities doing precisely that.

So the screen reads ``TOT_ACT_PARTCP_BOY_CNT`` -- employees still accruing.
Present on 95.2% of main-form filings and 99.9% of short-form ones; a plan
that does not report it is left out of the comparison rather than counted as
zero. Even then a sponsor with three plans counts the same employees in each,
so the sum across plans is an upper bound and the largest single plan a lower
one, and the screen filters on the lower bound.

*A flat trend is not the same as a lapse.* This is the distinction
:func:`marketradar.sources.form5500.build_trend` exists to preserve, and the
screen depends on it entirely. A sponsor absent from a later plan year may
have terminated the plan, been acquired, changed EIN, or simply not filed
yet -- filings lag the plan year by about eighteen months. None of those is a
headcount decline, and a screen that read them as one would rank its own
blind spot at the top of the list. So candidates must be ``status='filing'``,
and the trend is measured only between years the sponsor actually filed.

**There is no composite score, and there was.** The first version ranked on
a weighted sum of age, size and decline. Every candidate in the top forty
scored between 0.992 and 0.999, because each component saturated: any
business past fifty years maxes the age term, anything near the participant
ceiling maxes the size term, and any decline past 25% maxes the third. What
looked like a ranking was a sort by headcount wearing three decimal places.

Worse, one of those components should never have been there. Size is already
a *filter* -- the band is 20 to 1,000 -- and adding it again as 30% of the
score says a 900-person business is a better target than a 200-person one,
which is not this screen's thesis and is not anything the data supports.

So the list is ordered by the one input with an unambiguous direction: the
age floor, oldest first. ``sort`` re-orders it by decline or by size for the
cases where those are what you want to read. The components are all on the
row either way, which is what a score was supposed to provide and did not.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any, Final, Iterable

import duckdb

from marketradar.sources import form5500

log = logging.getLogger(__name__)

#: Minimum age, in years, of the oldest plan the sponsor still files. Twenty
#: years is deliberately conservative given the floor problem above: at this
#: distance a plan-start date is old enough that the company is old whichever
#: way the floor is wrong.
MIN_AGE_YEARS: Final[int] = 20

#: Headcount band, read against the sponsor's largest single plan -- the
#: lower bound of the range, so a target that clears the floor on that
#: reading clears it on any reading. The floor is where a business is large
#: enough to have a management layer to sell; the ceiling is where it stops
#: being a private-market target and starts being an auction.
MIN_PARTICIPANTS: Final[int] = 20
MAX_PARTICIPANTS: Final[int] = 1_000

#: Trends that count as "not growing". Growth is excluded rather than ranked
#: last: a business adding staff is a different thesis, not a worse version
#: of this one.
TARGET_TRENDS: Final[tuple[str, ...]] = (
    form5500.TREND_FLAT, form5500.TREND_DECLINING,
)

#: How the list can be ordered. ``age`` is the default because it is the only
#: input whose direction is not a judgement call: older is unambiguously more
#: interesting for a succession thesis. Whether a steeper decline is better
#: (more motivated seller) or worse (a business with a problem) is a question
#: about the thesis rather than about the data, so it is a choice here and
#: not a weight buried in a number.
SORTS: Final[dict[str, str]] = {
    "age": "age_years desc, active_last asc, ein",
    "decline": "pct_change asc nulls last, age_years desc, ein",
    "size": "active_last desc, age_years desc, ein",
    "smallest": "active_last asc, age_years desc, ein",
}


@dataclass(frozen=True, slots=True)
class Target:
    """One candidate. Every bound is carried, never collapsed to one number."""

    ein: str
    sponsor_name: str
    naics: str | None
    city: str | None
    state: str | None
    #: Effective date of the oldest plan still filed. A floor on entity age.
    oldest_plan_eff: date | None
    age_years: float
    #: Lower and upper bounds on headcount, in that order: the sponsor's
    #: largest single plan in its newest filed year, and the sum across its
    #: plans that year, which counts shared employees more than once.
    participants_last: int
    participants_sum: int
    #: Employees still accruing, and the sum of those across plans. The two
    #: columns above also count retirees and separated ex-employees holding a
    #: balance -- 22% of the main form's participants, and not headcount.
    active_last: int
    active_sum: int
    #: The two comparable totals the trend is measured between: the plans
    #: this sponsor filed in *both* its first and last complete year, summed
    #: at each end. Not the same thing as the columns above -- those describe
    #: the newest year, these describe a like-for-like pair.
    matched_first: int
    matched_last: int
    #: Plans present at one end and not the other. Reported rather than
    #: folded into the trend: a plan opening or closing is a fact about the
    #: filing, not about headcount.
    common_plans: int
    plans_added: int
    plans_dropped: int
    trend: str
    pct_change: float | None
    first_year: int
    last_year: int
    years_filed: int
    plans: int
    is_multiemployer: bool
    #: ``plan_type`` (sponsors a 403(b), so a 501(c)(3) or public school --
    #: structural and certain), ``naics`` (in a nonprofit-dense sector, a
    #: guess), ``both``, or ``None``. Never a silent exclusion: the screen
    #: sets these aside by default and reports how many, by basis.
    nonprofit_basis: str | None

    @property
    def age_note(self) -> str:
        """How the age reads in a sentence, with its direction of error."""
        if self.oldest_plan_eff is None:
            return "no usable plan date"
        return (f"at least {self.age_years:.0f}y "
                f"(oldest plan {self.oldest_plan_eff.isoformat()})")

    @property
    def plan_note(self) -> str:
        """What changed in the filing set, if anything.

        A candidate whose plan set churned is a weaker reading than one whose
        plans are identical at both ends, even though the trend itself is
        measured only over the plans they share.
        """
        if not (self.plans_added or self.plans_dropped):
            return f"{self.common_plans} plan(s), unchanged"
        bits = []
        if self.plans_added:
            bits.append(f"+{self.plans_added} new")
        if self.plans_dropped:
            bits.append(f"-{self.plans_dropped} gone")
        return (f"{self.common_plans} compared, " + ", ".join(bits)
                + " (not in the trend)")

    @property
    def headcount_note(self) -> str:
        if self.active_last == self.active_sum:
            return f"{self.active_last:,}"
        return f"{self.active_last:,}–{self.active_sum:,}"


def candidates(
    con: duckdb.DuckDBPyConnection,
    *,
    trend: str = "f5500_trend",
    as_of: date | None = None,
    min_age_years: int = MIN_AGE_YEARS,
    min_participants: int = MIN_PARTICIPANTS,
    max_participants: int = MAX_PARTICIPANTS,
    trends: Iterable[str] = TARGET_TRENDS,
    include_dfe: bool = False,
    include_multiemployer: bool = False,
    nonprofits: str = "exclude",
    sort: str = "age",
    limit: int | None = None,
) -> list[Target]:
    """Rank private sponsors that are old, sized, still filing, and not growing.

    ``trend`` is the table :func:`form5500.build_trend` writes. ``as_of``
    defaults to today and exists so a test can pin the age arithmetic.

    Four filters, and each one drops a population for a stated reason:

    - **not resolved to an SEC filer**, and no name match either. A sponsor
      whose name matched while its EIN did not is an open question in the
      review queue, and a question is not a private company -- counting it as
      one is the 44.2%-precision mistake wearing a different hat.
    - **not a nonprofit**, by default. A college, a church or a museum fits
      every other filter here -- old, private, flat headcount -- and cannot
      be bought, so it is noise in a deal-sourcing list. ``nonprofits`` takes
      ``exclude`` (default), ``only`` to inspect what is being set aside, or
      ``include``. Two tiers of evidence and the row says which: a 403(b) is
      structural (only a 501(c)(3) or public school may sponsor one) while
      the NAICS set is a sector guess that will also catch a for-profit
      hospital group or a family nursing home.
    - **not a DFE, and not a multiemployer plan.** Both are trustees rather
      than employers, and both take the top of the list if left in. A master
      trust is old and enormous and flat; a Taft-Hartley board of trustees is
      old and enormous and declining, because it covers an entire trade in a
      region and the building trades have been shrinking for decades. Five of
      the first twelve candidates this screen ever produced were boards of
      trustees for tile layers, masons and carpenters.
    - **still filing.** A lapsed sponsor may not exist any more, and the one
      thing this screen must not do is confuse that with a decline.
    - **age and headcount inside the bands**, both read against their
      conservative bound.
    """
    as_of = as_of or date.today()
    if sort not in SORTS:
        raise ValueError(f"unknown sort {sort!r}; try one of {sorted(SORTS)}")
    trend_list = ", ".join(f"'{t}'" for t in trends)
    dfe_clause = "" if include_dfe else "and not is_dfe"
    me_clause = "" if include_multiemployer else "and not is_multiemployer"
    if nonprofits not in ("exclude", "include", "only"):
        raise ValueError(f"nonprofits must be exclude/include/only, "
                         f"got {nonprofits!r}")
    np_clause = {
        "exclude": "and nonprofit_basis is null",
        "only": "and nonprofit_basis is not null",
        "include": "",
    }[nonprofits]
    limit_clause = f"limit {int(limit)}" if limit else ""

    rows = con.execute(f"""
        with scored as (
            select
                ein, sponsor_name, naics, city, state, oldest_plan_eff,
                date_diff('day', oldest_plan_eff, DATE '{as_of.isoformat()}')
                    / 365.25                              as age_years,
                participants_last, participants_sum,
                active_last, active_sum, is_multiemployer, nonprofit_basis,
                matched_first, matched_last,
                common_plans, plans_added, plans_dropped,
                trend, pct_change, first_year, last_year, years_filed, plans
            from {trend}
            where not by_ein
              and not name_matched
              {dfe_clause}
              {me_clause}
              {np_clause}
              and status = '{form5500.STATUS_FILING}'
              and trend in ({trend_list})
              and oldest_plan_eff is not null
              and active_last between {int(min_participants)}
                                  and {int(max_participants)}
        )
        select * from scored
        where age_years >= {int(min_age_years)}
        order by {SORTS[sort]}
        {limit_clause}
    """).fetchall()

    cols = [d[0] for d in con.description]
    out = [Target(**{
        k: v for k, v in zip(cols, r)
    }) for r in rows]
    log.info("%d mature-target candidates (age >= %dy, %d-%d participants, "
             "trend in %s)", len(out), min_age_years, min_participants,
             max_participants, tuple(trends))
    return out


def summarise(targets: list[Target]) -> dict[str, Any]:
    """Counts a reader needs before trusting the list.

    Reports the *population* the screen ran against as well as what it
    returned, because a list of nine from a file of 800,000 usually means a
    band is wrong rather than that nine companies qualify.
    """
    by_trend: dict[str, int] = {}
    by_state: dict[str, int] = {}
    for t in targets:
        by_trend[t.trend] = by_trend.get(t.trend, 0) + 1
        by_state[t.state or "??"] = by_state.get(t.state or "??", 0) + 1
    ages = sorted(t.age_years for t in targets)
    return {
        "candidates": len(targets),
        "by_trend": by_trend,
        "top_states": sorted(by_state.items(), key=lambda kv: -kv[1])[:10],
        "median_age": ages[len(ages) // 2] if ages else None,
        # min(), not max(): the oldest plan is the earliest date.
        "oldest": min((t.oldest_plan_eff for t in targets
                       if t.oldest_plan_eff), default=None),
    }


def funnel(
    con: duckdb.DuckDBPyConnection, *, trend: str = "f5500_trend",
    **kw: Any,
) -> Any:
    """The population by stage, as the shared :mod:`funnel` type.

    This screen is the reason that module exists: its multiemployer filter
    removed 800,287 sponsors instead of 4,502 and the surviving list looked
    *more* plausible than the correct one. The stage count was the only
    thing that said otherwise.
    """
    from marketradar.screens import funnel as funnel_mod

    pop = population(con, trend=trend)
    return funnel_mod.build(
        "mature targets",
        ("sponsors", pop["sponsors"], "every EIN across the loaded years"),
        ("private", pop["private"],
         "no SEC match by EIN, and no name match either"),
        ("not a pooled vehicle", pop["not_dfe"], "DFEs are trustees"),
        ("an employer", pop["an_employer"],
         "not a multiemployer board of trustees"),
        ("for-profit", pop["for_profit"],
         "nonprofits set aside; --nonprofits only to read them"),
        ("still filing", pop["still_filing"],
         "a lapse is a question, never a decline"),
        ("has a plan date", pop["has_plan_date"], "the age floor needs one"),
        ("trend measurable", pop["trend_known"],
         "two complete filed years with a plan in common"),
    )


def population(
    con: duckdb.DuckDBPyConnection, *, trend: str = "f5500_trend"
) -> dict[str, int]:
    """How many sponsors each filter removed, in the order applied.

    A screen that returns a short list is either selective or broken, and
    these counts are the difference. They are also how a change to a band
    gets argued about with a number rather than a feeling.
    """
    q = f"select count(*) from {trend} where "
    private = "not by_ein and not name_matched"
    steps = {
        "sponsors": "true",
        "private": "not by_ein and not name_matched",
        "not_dfe": "not by_ein and not name_matched and not is_dfe",
        "an_employer": ("not by_ein and not name_matched and not is_dfe "
                        "and not is_multiemployer"),
        "for_profit": ("not by_ein and not name_matched and not is_dfe "
                       "and not is_multiemployer "
                       "and nonprofit_basis is null"),
        "still_filing": ("not by_ein and not name_matched and not is_dfe "
                         "and not is_multiemployer "
                         "and nonprofit_basis is null "
                         f"and status = '{form5500.STATUS_FILING}'"),
        "has_plan_date": ("not by_ein and not name_matched and not is_dfe "
                          "and not is_multiemployer "
                          "and nonprofit_basis is null "
                          f"and status = '{form5500.STATUS_FILING}' "
                          "and oldest_plan_eff is not null"),
        "trend_known": ("not by_ein and not name_matched and not is_dfe "
                        "and not is_multiemployer "
                        "and nonprofit_basis is null "
                        f"and status = '{form5500.STATUS_FILING}' "
                        "and oldest_plan_eff is not null "
                        f"and trend <> '{form5500.TREND_UNKNOWN}'"),
    }
    out = {k: int(con.execute(q + v).fetchone()[0]) for k, v in steps.items()}
    # What the nonprofit filter set aside, split by how good the evidence is.
    # A count that is only ever subtracted is a count nobody can check.
    for basis, label in ((form5500.NONPROFIT_PLAN, "set_aside_403b"),
                         (form5500.NONPROFIT_BOTH, "set_aside_both"),
                         (form5500.NONPROFIT_NAICS_ONLY, "set_aside_naics")):
        out[label] = int(con.execute(
            f"{q}{private} and not is_dfe and not is_multiemployer "
            f"and nonprofit_basis = '{basis}'").fetchone()[0])
    return out
