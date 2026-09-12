"""Which XBRL tag carries which concept, and for whom.

A hand-maintained map, because there is no machine answer: the same concept
appears under different tags depending on the filer and the accounting era, and
the only way to know which tags matter is to count them in the filings. Every
number in this module was measured on the SEC Financial Statement Data Sets and
says which quarter it came from, so a later reader can re-measure rather than
trust it.

Three things this module decides, in the order they bite.

**Who is comparable.** A quarter of filers are not operating companies and do
not have an operating company's statements -- a bank's top line is interest
income, a REIT's earnings measure is FFO. They are excluded by SIC and
*counted*, never silently absent, so the table is known to be 74% of the market
by construction rather than by accident. See :func:`classify_sic`.

**Which era.** 78.2% of filers changed their income-statement top-line tag
between 2018 and 2019, when ASC 606 landed. That is one cliff rather than
rolling churn, so the map is era-keyed with exactly one boundary
(:data:`ERA_BOUNDARY`) and v1 populates only the post-606 half. A pre-606
filing resolves to nothing and says so; it does not get run through a map built
from the wrong era.

**Which tag, when a filer reports several.** This is the part that looks like a
detail and is not. Measured on 2024q1, 1,280 of 2,804 operating filers report
both ``NetIncomeLoss`` and ``ProfitLoss``, and **616 of them report different
values** -- because one is attributable to the parent and the other includes
noncontrolling interests. 781 report two equity tags and 608 of those differ.
So the priority order below is not a tiebreak: for a fifth to a half of the
population it *is* the definition of the column.

Which is why two rules hold everywhere downstream:

- the order is by rule, never by whichever row the scan reached first, and
- the resolved tag is carried on every row, so a consumer can see which
  definition it was handed.

That is the same lesson as ``TOT_PARTCP_BOY_CNT`` in the Form 5500 work: when a
source offers a total and a component under similar names, the total is usually
not what you want and it never errors.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final

# --- eras ---------------------------------------------------------------

#: ASC 606 applies to fiscal years *beginning* on or after this date. One
#: boundary, not a rolling map: share of operating filers changing their
#: top-line tag between sampled years was 11-20% everywhere except
#: 2018 -> 2019, where it was 78.2%. ``SalesRevenue*`` went from 48.4% of
#: filers to 2.8% in that single year.
ERA_BOUNDARY: Final[date] = date(2017, 12, 15)

POST_606: Final[str] = "post_606"
PRE_606: Final[str] = "pre_606"


def _minus_months(day: date, months: int) -> date:
    """``day`` shifted back ``months``, clamped to the shorter month's end."""
    m = day.month - months
    year = day.year + (m - 1) // 12
    month = (m - 1) % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def fiscal_start(period_end: date, *, months: int = 12) -> date:
    """The first day of the fiscal period ending on ``period_end``.

    The **day after** the same date a year earlier, not that date itself: a
    fiscal year ending 2018-12-31 began on 2018-01-01, and a map keyed on the
    off-by-one answer would put every December filer on the wrong side of the
    ASC 606 boundary -- which is half the market, and would resolve cleanly
    either way.
    """
    return _minus_months(period_end, months) + timedelta(days=1)


def era_for(period_end: date, *, months: int = 12) -> str:
    """Which era a filing's *fiscal year* falls in.

    The boundary is on the year's **beginning** and the datasets give its end,
    so the start is derived. ``months`` is twelve for an annual filing; a
    transition period is shorter, which is why it is a parameter rather than a
    hardcoded year subtraction.

    A fiscal year ending 2018-11-30 began on 2017-12-01, *before* the boundary,
    and is pre-606; one ending 2018-12-31 began on 2018-01-01 and is post. Half
    a calendar year sits either side of it, which is exactly why the test is on
    the start date and not on ``fy``.
    """
    if fiscal_start(period_end, months=months) >= ERA_BOUNDARY:
        return POST_606
    return PRE_606


# --- who is comparable --------------------------------------------------

OPERATING: Final[str] = "operating"
UNCLASSIFIED: Final[str] = "unclassified"

#: SIC ranges that are **not** operating companies, with the share of 10-K and
#: 10-Q filers each held in 2024q1. Measured here rather than recalled: the
#: totals reproduce the ones recorded in docs/build-spec.md to within 0.2pp,
#: which is how these ranges are known to be the same ones the scope decision
#: was made on.
#:
#: They are a *different table*, not a branch inside this one. Forcing a bank
#: into an operating-company shape produces numbers that are present, plausible
#: and wrong, which is the failure this whole module is arranged to avoid.
NON_OPERATING_SIC: Final[tuple[tuple[int, int, str], ...]] = (
    (6000, 6199, "bank_credit"),      # 9.3%
    (6200, 6299, "broker"),           # 2.9%
    (6300, 6411, "insurance"),        # 2.6%
    (6500, 6599, "real_estate"),      # 1.5%
    (6700, 6799, "holding"),          # 2.4%, and 6798 REITs are inside it
)

#: REITs are called out by their own code even though 6798 falls inside the
#: holding range above, because their earnings measure is FFO rather than net
#: income and the count is worth reading on its own: 4.7% of 2024q1 filers.
REIT_SIC: Final[int] = 6798


def classify_sic(sic: int | str | None) -> str:
    """``operating``, a named non-operating class, or ``unclassified``.

    A missing SIC is **not** operating. 147 of 5,018 2024q1 10-K/10-Q filings
    carry none, and "we do not know what this company is" is a different fact
    from "this is an operating company" -- the same distinction as `absent`
    against `unmapped` below. Folding them in would have put 2.9% of the
    population into the table on no evidence, and nothing downstream could
    have told which rows those were.
    """
    if sic is None or sic == "":
        return UNCLASSIFIED
    try:
        code = int(sic)
    except (TypeError, ValueError):
        return UNCLASSIFIED
    if code == REIT_SIC:
        return "reit"
    for lo, hi, name in NON_OPERATING_SIC:
        if lo <= code <= hi:
            return name
    return OPERATING


# --- concepts -----------------------------------------------------------

#: Does an income-statement tag name look like it carries revenue?
#:
#: This is what separates :data:`ABSENT` from :data:`UNMAPPED`, and it is a
#: heuristic on a tag *name* -- which is normally banned here. It is allowed
#: for exactly this: the answer never feeds a join or a value, only which of
#: two buckets an unresolved filer is counted in. A false positive adds a tag
#: to a review list; it cannot put a wrong number in a column.
REVENUE_LIKE: Final[re.Pattern[str]] = re.compile(
    r"revenue|sales|fees|premium|interestincome", re.IGNORECASE
)

#: Duration in quarters, as the datasets encode it in ``num.qtrs``. ``0`` is a
#: point in time (a balance sheet), ``4`` a full year of flow.
INSTANT: Final[int] = 0
ANNUAL: Final[int] = 4


@dataclass(frozen=True, slots=True)
class Concept:
    """One fundamental, and the tags that carry it in priority order.

    ``tags`` is ordered and the order is a decision. The first tag present on a
    filing wins, and which one that is changes the number for a large minority
    of filers -- see the module docstring. ``definition`` says what the winner
    means, because "net income" is two different numbers and a column name
    cannot carry that.
    """

    name: str
    #: Candidate tags, most-preferred first. Every one is ``us-gaap``.
    tags: tuple[str, ...]
    qtrs: int
    #: What the preferred tag measures, in words, including what it excludes.
    definition: str
    #: Share of operating 10-K FY filers in 2024q1 for whom *any* of ``tags``
    #: resolves at the filing's own period end. Carried here so it can be
    #: rendered beside the number wherever it is consumed.
    #:
    #: **A reference point, not an expectation for every quarter.** Measured
    #: across 2019q1/2020q1/2022q1/2024q1, revenue holds 85.9-89.1% with no
    #: trend, but liabilities runs 74.4% -> 83.2% over the same span: filers
    #: increasingly tag a total liabilities line. So a 2019 quarter resolving
    #: liabilities nine points below this number is not rot, it is 2019. The
    #: rot signal is the per-quarter series, which ``resolve.coverage_matrix``
    #: builds from the partitions themselves.
    coverage_2024q1: float
    #: Why the map looks the way it does, where that is not obvious.
    note: str = ""
    #: Pattern that tells :data:`UNMAPPED` from :data:`ABSENT` for this
    #: concept, applied to the tag the filer presented at the top of its
    #: income statement. ``None`` means there is no such evidence and an
    #: unresolved filing is ``absent``.
    #:
    #: **Only revenue has one**, and the first version of the loader did not
    #: make that distinction -- it applied the income-statement top line to all
    #: six concepts. The result was a liabilities work queue of 445 filings
    #: whose top three "tags to add" were
    #: ``RevenueFromContractWithCustomerExcludingAssessedTax``, ``Revenues``
    #: and ``RevenueFromContractWithCustomerIncludingAssessedTax``: neat,
    #: sorted, plausible and meaningless, because what a filer puts at the top
    #: of its income statement is evidence about revenue and about nothing
    #: else. A balance sheet always has liabilities, so not stating a total for
    #: them is a presentation choice and not a tag to find.
    unmapped_when: re.Pattern[str] | None = None


#: v1: six concepts, post-606, operating companies only.
#:
#: Five are above 97% and revenue is 87.7%. Revenue is deliberately *not*
#: averaged into a complete-row requirement: requiring all six would report the
#: intersection and hide which concept did the excluding. Each resolves
#: independently and carries its own number.
CONCEPTS: Final[dict[str, Concept]] = {
    "revenue": Concept(
        name="revenue",
        tags=(
            # The ASC 606 contract-revenue tags and the generic total. Ordered
            # total-first: where a filer reports both `Revenues` and a contract
            # tag (73 filings in 2024q1, 59 of them with different values),
            # `Revenues` is the broader line and the contract tag is a
            # component of it.
            "Revenues",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
            # The tail. Each is under 1% on its own and they are regulated
            # utilities, lessors and collaboration-funded biotech -- real
            # revenue presented under a specific tag rather than the generic
            # one.
            "RegulatedAndUnregulatedOperatingRevenue",
            "RegulatedOperatingRevenue",
            "RevenueNotFromContractWithCustomer",
            "RevenueFromCollaborativeArrangementExcludingRevenueFromContractWithCustomer",
            "OperatingLeaseLeaseIncome",
            # The pre-606 tags, last. Added 2026-09-10 off the unmapped queue,
            # which is what that queue is for: `SalesRevenueNet` (25 filings)
            # and `SalesRevenueGoodsNet` (19) were the two biggest entries in
            # 2019q1, worth 1.7pp of that quarter on their own.
            #
            # They belong here even though the era boundary exists because
            # these tags collapsed: the boundary describes where the
            # *distribution* moved, not a date after which an old tag became
            # invalid. A post-606 filer still using `SalesRevenueNet` has that
            # as its revenue line. Ranked last so a filer reporting both gets
            # the newer tag, which is the one its peers report.
            "SalesRevenueNet",
            "SalesRevenueGoodsNet",
        ),
        qtrs=ANNUAL,
        definition="total revenue for the fiscal year, consolidated",
        coverage_2024q1=0.879,
        unmapped_when=REVENUE_LIKE,
        note=(
            "The outlier, and the one that matters. The misses are not one "
            "thing: 8.9% of operating filers present no revenue line at all "
            "because they are pre-revenue, which is `absent` and not a gap to "
            "close, and 3.3% use a tag this map does not carry, which is "
            "`unmapped` and is the work queue."
        ),
    ),
    "net_income": Concept(
        name="net_income",
        tags=(
            # Parent-attributable first, deliberately. 1,280 of 2,804 filers
            # report both of the first two and 616 of those differ: ProfitLoss
            # includes noncontrolling interests and NetIncomeLoss does not.
            # Consistency with `equity` below matters more than either choice.
            "NetIncomeLoss",
            "ProfitLoss",
            # After preferred dividends. Different again, and last.
            "NetIncomeLossAvailableToCommonStockholdersBasic",
        ),
        qtrs=ANNUAL,
        definition=(
            "net income attributable to the parent, before preferred "
            "dividends; excludes noncontrolling interests"
        ),
        coverage_2024q1=0.996,
    ),
    "assets": Concept(
        name="assets",
        tags=("Assets",),
        qtrs=INSTANT,
        definition="total assets at the period end",
        coverage_2024q1=0.995,
        note="One tag, 99.5%. The only concept here that needs no choice made.",
    ),
    "liabilities": Concept(
        name="liabilities",
        tags=("Liabilities",),
        qtrs=INSTANT,
        definition="total liabilities at the period end, as stated",
        coverage_2024q1=0.832,
        note=(
            "83.2%, not the 99.9% recorded in the original measurement -- that "
            "number was `LiabilitiesAndStockholdersEquity`, which is the "
            "balance-sheet total and equals assets. The project's own rule, "
            "applied to its own measurement: check what the column counts "
            "before naming it. See DERIVED_LIABILITIES_REJECTED for why the "
            "obvious fix is not taken."
        ),
    ),
    "equity": Concept(
        name="equity",
        tags=(
            # Parent-attributable first, matching net_income. 781 filers report
            # both and 608 differ by the noncontrolling interest.
            "StockholdersEquity",
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        ),
        qtrs=INSTANT,
        definition=(
            "equity attributable to the parent at the period end; excludes "
            "noncontrolling interests and any mezzanine (temporary) equity"
        ),
        coverage_2024q1=0.977,
    ),
    "capex": Concept(
        name="capex",
        tags=(
            # Cash paid for productive fixed assets, most-preferred first.
            # Measured 2026-09-12 on 2024q1's 2,804 operating annual filings:
            # the first tag alone is 74.5% and the ladder reaches 88.7%.
            "PaymentsToAcquirePropertyPlantAndEquipment",          # 74.5%
            "PaymentsToAcquireProductiveAssets",                   # +247
            "PaymentsForCapitalImprovements",                      # +26
            "PaymentsToAcquireOtherPropertyPlantAndEquipment",      # +45
            "PaymentsToAcquireMachineryAndEquipment",               # +29
            # Extractive filers put their capex here and nowhere else, which is
            # why two oil-and-gas tags sit in a general map: for an E&P company
            # this *is* the capital programme, not a special case of one.
            "PaymentsToAcquireOilAndGasProperty",                   # +14
            "PaymentsToAcquireOilAndGasPropertyAndEquipment",       # +10
            "PaymentsToAcquireOtherProductiveAssets",               # +19
            # The tail: 1-4 filers each. Carried because they are unambiguously
            # the same concept and a tag that costs nothing to include should not
            # be a future work-queue entry, not because they move coverage.
            "PaymentsToDevelopSoftware",
            "PaymentsToAcquireBuildings",
            "PaymentsToAcquireLand",
            "PaymentsToAcquireEquipmentOnLease",
        ),
        qtrs=ANNUAL,
        definition=(
            "cash paid during the fiscal year for property, plant and equipment "
            "and other productive fixed assets. Excludes acquisitions of "
            "businesses, purchases of securities, and intangibles -- see "
            "NOT_CAPEX for why each of those is a different concept rather than "
            "a missing tag"
        ),
        coverage_2024q1=0.878,
        note=(
            "Deferred out of v1 at 0.799 and that number was a floor rather than "
            "a ceiling: it came from a thinner candidate list. Measured properly "
            "2026-09-12 it is 0.878, the weakest of the seven and the one that "
            "bounds a DCF, because free cash flow is operating cash flow minus "
            "capex and the other half is 0.996. FCF on the same filer is 87.8% "
            "-- capex is the binding half, essentially alone. "
            "The number is the loader's, not a probe's. A hand-written probe "
            "over num.txt read 88.7% because it counted segment-tagged rows; "
            "the resolver reports those as segment_only, which is the correct "
            "answer and 0.9 points lower. A concept's coverage figure has to be "
            "the one the code reproduces, or the drift test is measuring the "
            "probe."
        ),
    ),
    "operating_cash_flow": Concept(
        name="operating_cash_flow",
        tags=(
            "NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
        ),
        qtrs=ANNUAL,
        definition=(
            "net cash from operating activities for the fiscal year, "
            "including discontinued operations where the filer reports a total"
        ),
        coverage_2024q1=0.996,
    ),
}

#: The quarter every ``coverage_2024q1`` figure above was measured on, and the
#: one ``tests/test_xbrl.py`` re-measures them against.
#:
#: Named here rather than hard-coded in the test because the download cache has to
#: know it too: ``--drop-zips`` deleted this quarter's zip on 2026-09-12 and the
#: drift test went from passing to **skipped**, silently, having just caught a
#: real 0.9-point error in the capex figure. A flag that removes a guard without
#: saying so is the pattern this codebase keeps finding, so :func:`download.prune`
#: now refuses this quarter.
REFERENCE_QUARTER: Final[str] = "2024q1"

#: Concepts measured and deliberately left out of v1, with their 2024q1
#: coverage. Kept here rather than deleted because each comes back when a
#: question needs it, carrying its own number -- not as a speculative column
#: dragging joint coverage down for consumers that never read it.
DEFERRED_CONCEPTS: Final[dict[str, float]] = {
    "shares": 0.951,
    "cash": 0.966,
    "operating_income": 0.913,
}

#: Tags whose names look like capex and which are **not** capex, with the reason.
#:
#: Recorded rather than merely omitted, because the ``unmapped`` work queue keeps
#: surfacing them and a reviewer has to be able to see that each was decided
#: against rather than overlooked. Name-matching every capex-shaped investing tag
#: would read 93.3% against the honest 88.7% -- **4.5 points of the wrong thing**,
#: and the wrong thing is large: the treasury tags have a median of $105M, which
#: would swamp the capex line for any filer holding a securities portfolio.
#:
#: The first entry is the one worth remembering. 26.9% of operating filers report
#: ``CapitalExpendituresIncurredButNotYetPaid``; its name contains
#: "CapitalExpenditures"; it is the *unpaid accrual*. Subtracting it from
#: operating cash flow would deduct money nobody spent. Same rule as
#: ``TOT_PARTCP_BOY_CNT``: read the field definition, not the field name.
NOT_CAPEX: Final[dict[str, str]] = {
    "CapitalExpendituresIncurredButNotYetPaid":
        "the unpaid accrual, not a cash outflow. Reported by 26.9% of filers and "
        "named as if it were the thing itself",
    "PaymentsToAcquireBusinessesNetOfCashAcquired":
        "acquisitions -- the largest entry in the unmapped queue, and it must "
        "stay there. Buying a company is not maintaining an asset base",
    "PaymentsToAcquireBusinessesGross": "acquisitions",
    "PaymentsToAcquireMarketableSecurities": "treasury; median $105M",
    "PaymentsToAcquireAvailableForSaleSecuritiesDebt": "treasury",
    "PaymentsToAcquireInvestments": "treasury",
    "PaymentsToAcquireShortTermInvestments": "treasury",
    "PaymentsToAcquireLongtermInvestments": "treasury",
    "PaymentsToAcquireHeldToMaturitySecurities": "treasury",
    "PaymentsToAcquireEquitySecuritiesFvNi": "treasury",
    "PaymentsToAcquireOtherInvestments": "treasury",
    "PaymentsToAcquireEquityMethodInvestments":
        "an investment, not an asset the business operates",
    "PaymentsToAcquireInterestInSubsidiariesAndAffiliates": "an investment",
    "PaymentsToAcquireInterestInJointVenture": "an investment",
    "PaymentsToAcquireNotesReceivable": "lending",
    "PaymentsToAcquireIntangibleAssets":
        "intangibles. Defensibly investment and definitely not PP&E; it becomes "
        "its own concept with its own coverage if a question needs it",
    "PaymentsToAcquireInProcessResearchAndDevelopment": "acquired IPR&D",
}

#: The pre-606 map is empty on purpose.
#:
#: The older era is three times the distinct top-line tags (419 against 133)
#: for twenty points worse top-5 concentration, so it is most of the work for
#: the worse coverage. v1 is 2019 forward and a pre-606 filing resolves to
#: nothing rather than being run through a map built from the other side of the
#: cliff -- which would resolve, and be wrong, and look identical.
TAGS_BY_ERA: Final[dict[str, dict[str, Concept]]] = {
    POST_606: CONCEPTS,
    PRE_606: {},
}

#: Measured 2024q1 and rejected: ``liabilities`` from
#: ``LiabilitiesAndStockholdersEquity - equity``.
#:
#: It would lift coverage from 83.2% to 99.0% and it is wrong for 5.5% of the
#: filers where both exist -- by more than 5%, and unboundedly: ProKidney Corp
#: states $29.2M and derives $1.52B, a 52x overstatement that would top any
#: leverage screen. 84% of those carry temporary or redeemable equity, and the
#: gap equals the mezzanine amount exactly: redeemable NCI sits between
#: liabilities and equity, in neither tag, so the subtraction quietly files it
#: under debt. Common in SPACs and anything recently de-SPACed.
#:
#: Subtracting mezzanine too would fix the 84% and leave the rest wrong
#: invisibly, which is the worse outcome: a derivation that is usually right is
#: harder to distrust than one that is absent. So liabilities is stated-only at
#: 83.2%, and the 17% is a stated gap rather than a silent error.
DERIVED_LIABILITIES_REJECTED: Final[str] = (
    "LiabilitiesAndStockholdersEquity - equity: 99.0% coverage, wrong by >5% "
    "for 5.5% of filers, 84% of those holding mezzanine equity the "
    "subtraction adds to debt (ProKidney Corp: $29.2M stated, $1.52B derived)"
)


# --- resolution statuses ------------------------------------------------

#: A mapped tag was found, consolidated, at the filing's own period end.
STATED: Final[str] = "stated"

#: The filer presents no line for this concept at all. For revenue that means
#: pre-revenue -- 8.9% of operating filers open their income statement with an
#: expense, mostly biotech and mining. **This is a value, not a gap**: it is
#: the same distinction as `lapsed` against `declining` in the Form 5500
#: series, and a resolver that treats it as a missing tag chases it forever
#: while the coverage number is wrong in both directions.
ABSENT: Final[str] = "absent"

#: A line that looks like this concept exists under a tag the map does not
#: carry. The actionable bucket, and the only one that is a queue: it names the
#: tag, so the next version of the map is a list rather than an investigation.
UNMAPPED: Final[str] = "unmapped"

#: The filing reports this concept only in a currency other than USD. Not a
#: mapping failure and not a gap to close -- a CAD reporter is out of scope for
#: a USD table. Counted apart so it does not inflate the work queue.
NOT_USD: Final[str] = "not_usd"

#: Present only disaggregated -- by segment, product or geography -- with no
#: consolidated total at the period end. The tag is mapped and the value we
#: want was never stated, which is different again from not having the tag.
SEGMENT_ONLY: Final[str] = "segment_only"

#: A mapped tag is there, consolidated and in USD, but never dated to the
#: filing's own period end -- only to the prior-year comparative, or to a
#: transition period of some other length. 27 rows across four filings in
#: 2024q1. Separate from :data:`ABSENT` because the concept is reported; it is
#: this period that has no value for it, which is a question about the filing
#: rather than about the map.
PERIOD_MISMATCH: Final[str] = "period_mismatch"

#: Every status, in the order a coverage report reads them: resolved first,
#: then the one that needs no fixing, then the work queue, then the three that
#: are out of scope for a different reason each.
STATUSES: Final[tuple[str, ...]] = (
    STATED, ABSENT, UNMAPPED, NOT_USD, SEGMENT_ONLY, PERIOD_MISMATCH,
)



def concept(name: str, era: str = POST_606) -> Concept | None:
    """The concept's map for an era, or None where that era has none."""
    return TAGS_BY_ERA.get(era, {}).get(name)


def tag_priority(name: str, era: str = POST_606) -> dict[str, int]:
    """``{tag: rank}``, rank 0 being most preferred.

    Handed to SQL as a rank rather than resolved with an arbitrary-row pick:
    those choose without saying which, so three loads of identical input can
    disagree. ``min_by`` on this rank is a rule rather than a coin flip.

    (The banned-function invariant scans every string literal in this package,
    docstrings included, so the functions in question are not named here with
    their parentheses -- the note explaining the ban must not be the thing that
    trips it. They are listed in tests/test_repo_invariants.py.)
    """
    got = concept(name, era)
    return {tag: i for i, tag in enumerate(got.tags)} if got else {}
