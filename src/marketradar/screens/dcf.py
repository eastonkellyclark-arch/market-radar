"""A discounted cash flow on normalised XBRL, with every substitution on the row.

**The headline is not the value, it is the substitution list.** A DCF is a stack of
inputs and most of them, for most filers, are not the thing the method asks for.
Free cash flow needs capex that 24% of filers do not report. WACC needs a beta that
needs a ticker, and the CIK->ticker link is current-only. The equity risk premium
has no free source at all. So a number produced from a peer-set beta, a fallen-back
comp depth, an absent capex line and a stored ERP constant is **four substitutions
deep**, and the one thing it must not do is sort alongside a clean one.

That is what :attr:`Valuation.substitutions` is for, and why it is a list rather
than a flag.

Measured 2026-09-12 over 6,431 operating filers, latest annual observation each:

    operating cash flow  98.6%
    capex                75.9%   <- binds FCF essentially alone
    FCF (both)           75.9%
    + revenue            70.5%
    + assets + equity    68.9%
    beta (CIK->ticker->prices)  46.6%

**Absent capex is marked, never zeroed.** 22.7% of filers present no capex line,
and `unmapped` is 0% -- there is no map work left, the line genuinely is not there.
But "no line" and "zero" are different facts: a filer that buried capex in an
aggregated investing total reports no capex line either, and treating that as zero
overstates free cash flow by exactly the buried amount. So such a filer gets a
valuation carrying :data:`ABSENT_CAPEX`, and `Valuation.clean` is False. Same rule
as a nil XBRL tag, which is evidence and not a zero.

**Coverage is reported same-quarter-of-year, never pooled.** Capex has a 20-point
q1-against-q2 spread -- q1 runs 84-91% and q2 runs 62-69% -- because q1 carries the
December fiscal year ends and q2-q4 are retailers and odd-year-end filers. A pooled
DCF coverage number is a weighted average of four different populations and moves
when the calendar does. See :func:`coverage_by_quarter`.

**Terminal value is perpetuity growth, and that was forced rather than chosen.**
An exit multiple needs a market multiple; our comps carry no prices, because XBRL
has no market cap and yfinance must not supply one for anything historical. So the
exit method is not something this module can compute at all -- it is exposed as an
override for a caller who supplies a multiple, and the default says why. Growth is
capped at the risk-free rate: a perpetuity growing faster than the discount rate
has infinite value, and one growing faster than long-run nominal GDP eventually
eats the economy.

No market cap anywhere in here. This produces an enterprise value. Comparing it to
a market cap is a separate, local, hand-run step -- that is what yfinance is for
and it is never reachable from a GitHub Action.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Final

import duckdb

from marketradar.screens import funnel as funnel_mod

log = logging.getLogger(__name__)

SCREEN: Final[str] = "dcf"

# --- the substitutions --------------------------------------------------
#
# Each names an input that is not the thing the method asks for. They are counted
# and carried, never collapsed into a single "quality" score: a peer-set beta and
# an absent capex line are wrong in different directions and a reader has to know
# which applies.

#: Beta came from the filer's peer set rather than its own returns. 53.4% of
#: filers have no usable CIK->ticker->price chain, so this is the common case and
#: not an edge one.
PEER_BETA: Final[str] = "peer_beta"

#: The peer set behind that beta fell past 4-digit SIC. Inherited from
#: ``screens/comps.py`` rather than re-derived, so the two surfaces cannot
#: disagree about how good a peer set was.
COMP_DEPTH: Final[str] = "comp_depth_fallback"

#: The filer reports no capex line, so free cash flow is operating cash flow with
#: nothing subtracted. **Not a zero** -- see the module docstring.
ABSENT_CAPEX: Final[str] = "absent_capex"

#: The equity risk premium is a stored constant. On every row by construction,
#: because there is no free source and none can be computed from what we hold.
ERP_CONSTANT: Final[str] = "erp_constant"

#: No beta at all, own or peer: the discount rate falls back to a flat equity
#: cost. The weakest row this module will emit, and it says so.
NO_BETA: Final[str] = "no_beta"

#: Near-term FCF growth is a flat constant, not a filer-specific forecast.
#:
#: **Added after the 20-name check, which is what it was for.** Against rough
#: market caps, the mature cohort lands near parity -- AbbVie 1.75x, Chevron 1.25x,
#: Mastercard 1.08x, J&J 0.96x -- while heavy reinvestors come out absurd: Amazon
#: 0.03x, Tesla 0.07x, AMD 0.09x, NVIDIA 0.22x. The arithmetic is right in every
#: case and the *input* is wrong for most of them, because one growth rate cannot
#: describe both Johnson & Johnson and NVIDIA.
#:
#: It belongs on the row for exactly the reason the ERP does: an assumption applied
#: uniformly because nothing better was available is a substitution, and leaving it
#: off the list made 1,584 rows look cleaner than they are.
GROWTH_CONSTANT: Final[str] = "growth_constant"

#: The filer's own history says the growth constant is badly wrong for it.
#:
#: **Not a rate -- a warning.** Measured 2026-09-12, a filer-specific growth rate
#: fitted from our own 30 quarters loses a held-out horse race against the flat
#: 3%, on every variant tried (see :data:`GROWTH_FITTING_REJECTED`). Past growth
#: does not predict future growth here, so this module does not pretend to know
#: NVIDIA's rate.
#:
#: What the history *can* say is that 3% is not it. A company that compounded
#: revenue at 40% for six years may or may not continue, but the constant is
#: certainly the wrong centre for it, and a valuation resting on that constant
#: should say so where it is least likely to hold.
GROWTH_MISMATCH: Final[str] = "growth_mismatch"

SUBSTITUTIONS: Final[tuple[str, ...]] = (
    PEER_BETA, COMP_DEPTH, ABSENT_CAPEX, ERP_CONSTANT, GROWTH_CONSTANT,
    GROWTH_MISMATCH, NO_BETA,
)

#: How far a filer's historical growth may sit from the constant before the row is
#: flagged. Set at the p75 of the measured distribution: median full-history
#: revenue growth is +6.0% and p75 is +17.0%, so this catches the upper quartile
#: and the shrinking tail without firing on ordinary companies.
GROWTH_MISMATCH_BAND: Final[float] = 0.10

#: Why the fitted rate is not used, recorded so it is not re-derived.
#:
#: Measured 2026-09-12 over the 30 loaded quarters. A rate fitted on the first half
#: of each filer's annual history, scored against the growth actually realised in
#: the second half, beside a flat 3% on the same filers and years:
#:
#:     revenue, CAGR endpoints    n=2,753  corr +0.010  fitted 0.138  const 0.089
#:     revenue, log-linear        n=2,606  corr -0.035  fitted 0.129  const 0.084
#:     free cash flow, CAGR       n=1,585  corr +0.079  fitted 0.365  const 0.227
#:     free cash flow, log-linear n=1,236  corr -0.011  fitted 0.311  const 0.199
#:
#: **The constant wins all four, and the correlation between a filer's past and
#: future growth is indistinguishable from zero** -- it is negative for both
#: log-linear fits. A fitted rate would be 50% worse on held-out data while
#: looking filer-specific, and the substitution list would stop warning about it.
#: That is strictly worse than an honest constant.
GROWTH_FITTING_REJECTED: Final[str] = (
    "A per-filer growth rate fitted from our own history loses to the flat "
    "constant out of sample, on revenue and on free cash flow, by CAGR and by "
    "log-linear fit. Correlation between first-half and second-half growth runs "
    "-0.035 to +0.079. Rejected 2026-09-12; see docs/build-spec.md."
)

#: What each substitution does to the answer, in the direction it does it. Printed
#: beside the count, because a substitution with no stated direction is a caveat
#: and caveats are the thing that gets forgotten.
SUBSTITUTION_WHY: Final[dict[str, str]] = {
    PEER_BETA: "beta is the peer set's median, not this filer's own returns -- "
               "it understates idiosyncratic risk for an unusual company",
    COMP_DEPTH: "the peer set behind that beta is a widened industry, so the "
                "beta is of a broader group than the label suggests",
    ABSENT_CAPEX: "no capex line, so FCF is operating cash flow undiminished. "
                  "**Overstates** FCF by whatever capex was folded into an "
                  "aggregated investing total",
    ERP_CONSTANT: "the equity risk premium is a dated constant, not a measured "
                  "or implied figure. On every row; there is no free source",
    NO_BETA: "no beta, own or peer. The discount rate is a flat equity cost and "
             "carries no company-specific risk at all",
    GROWTH_CONSTANT: "near-term growth is a flat constant, not a filer-specific "
                     "forecast. **Understates** any company growing faster than "
                     "it -- measured at 0.03x of market cap for Amazon against "
                     "0.96x for Johnson & Johnson",
    GROWTH_MISMATCH: "this filer's own history is more than 10 points from the "
                     "growth constant, so the constant is unlikely to be the "
                     "right centre for it. A warning, not a rate: a fitted rate "
                     "loses to the constant out of sample",
}

# --- WACC inputs --------------------------------------------------------

#: Equity risk premium, as a stored constant with its source and as-of date.
#:
#: **A decision, not a default.** There is no free ERP API, and none can be
#: computed from what this system holds: FRED gives ``DGS10`` but no long-run
#: equity total-return series, so a historical premium is not derivable here. The
#: options were a constant or a required input, and a required input means a DCF
#: that produces nothing until somebody types a number -- which in practice means
#: somebody types 5.0 and it is recorded nowhere.
#:
#: So: a constant, dated, attributed, carried on every row as
#: :data:`ERP_CONSTANT`, and overridable per call. The date matters as much as the
#: value; an ERP from a different rate environment is a different assumption.
ERP: Final[float] = 0.0433
ERP_AS_OF: Final[date] = date(2026, 9, 1)
ERP_SOURCE: Final[str] = (
    "Damodaran implied equity risk premium for the S&P 500, monthly series, "
    "as of 2026-09-01. A published figure cited with attribution, not a "
    "redistributed dataset"
)

#: Risk-free rate series. Treasury, public domain, and local-only per the FRED
#: rule -- read from ``macro_series`` and never published.
RISK_FREE_SERIES: Final[str] = "DGS10"

#: Used when ``macro_series`` has no DGS10 observation. Marked, not silent.
RISK_FREE_FALLBACK: Final[float] = 0.0483

#: Cost of debt, as a spread over the risk-free rate. ICE BofA investment-grade
#: and high-yield spreads are in ``macro_series``, so this is a floor rather than
#: a guess -- but it is not issuer-specific, which a real credit spread would be.
DEBT_SPREAD: Final[float] = 0.0200

#: Marginal tax rate for the after-tax cost of debt. The US federal statutory
#: rate; state and foreign mix is filer-specific and not modelled.
TAX_RATE: Final[float] = 0.21

#: Equity cost used when there is no beta at all. Not a beta of 1.0 dressed up:
#: a flat number makes the absence visible in the output, where beta=1.0 would
#: look like a measurement.
FLAT_EQUITY_COST: Final[float] = 0.0950

#: Explicit forecast horizon, in years. Ten is convention; the terminal value
#: dominates either way and the honest response to that is to report the split.
HORIZON: Final[int] = 10

#: Terminal growth, capped at the risk-free rate. A perpetuity growing at or above
#: the discount rate has infinite value, and one growing faster than long-run
#: nominal GDP eventually becomes the economy.
TERMINAL_GROWTH: Final[float] = 0.0250

#: Near-term FCF growth when nothing better is known. Deliberately low: the
#: alternative is extrapolating one year of revenue growth for ten years, which
#: is a forecast dressed as an input.
DEFAULT_GROWTH: Final[float] = 0.0300

#: A WACC below this is not a discount rate, it is an arithmetic accident -- and
#: it makes the terminal value explode. Rejected rather than clamped.
MIN_WACC: Final[float] = 0.0400

#: Growth rates the valuation is flexed at, stored with every row.
#:
#: **Stored rather than recomputed by a consumer.** The deck used to derive these
#: four numbers itself, which made it the only surface that computed rather than
#: rendered -- and therefore the only one that could disagree with the panel about
#: the same filer. A deck is the artifact that leaves the room, so it reads.
#:
#: Four points rather than a curve: the flat constant, zero, and two steps above
#: it. Enough to show what the assumption costs without implying a distribution
#: nobody measured.
FLEX_GROWTH: Final[tuple[float, ...]] = (0.00, 0.03, 0.06, 0.10)

#: A beta outside this range is a failed regression, not a risky company.
#:
#: **Measured 2026-09-12 and it was not hypothetical.** A first pass over 5,500
#: CIKs returned a minimum beta of **-5,886**: an illiquid ticker whose weekly
#: series is mostly flat with one enormous move, regressed against a benchmark
#: whose variance over the same weeks is tiny. The slope is arithmetically real
#: and means nothing, and fed into CAPM it produces a negative cost of equity --
#: which would then pass the WACC floor from the wrong side.
#:
#: Rejected rather than clamped, for the same reason a missing split is never
#: inferred from a price jump: a clamped -5,886 becomes -3.0 and looks like a
#: measurement. Returning None sends the filer to the peer-set fallback, which is
#: marked on the row.
BETA_BOUNDS: Final[tuple[float, float]] = (-3.0, 5.0)

# --- why a filer gets no valuation -------------------------------------

VALUED: Final[str] = "valued"
NO_CASH_FLOW: Final[str] = "no_cash_flow"
NEGATIVE_FCF: Final[str] = "negative_fcf"
NO_DISCOUNT_RATE: Final[str] = "no_discount_rate"

OUTCOMES: Final[tuple[str, ...]] = (
    VALUED, NO_CASH_FLOW, NEGATIVE_FCF, NO_DISCOUNT_RATE,
)

OUTCOME_WHY: Final[dict[str, str]] = {
    VALUED: "an enterprise value, with its substitutions listed",
    NO_CASH_FLOW: "no operating cash flow on the latest annual filing. Nothing "
                  "to discount; not a modelling choice",
    NEGATIVE_FCF: "free cash flow is negative, so a growing perpetuity of it is "
                  "a negative number with a confident shape. Excluded rather "
                  "than reported, because the method does not apply",
    NO_DISCOUNT_RATE: "the WACC came out below the floor, which makes the "
                      "terminal value explode. Rejected rather than clamped",
}


def cik_key(cik: Any) -> str:
    """A CIK in one representation, so two sources can be joined on it.

    The XBRL partitions carry it unpadded and Postgres carries it zero-padded to
    ten characters. Both are the same identifier and neither string equals the
    other, so every cross-source lookup in this module goes through here. A
    mismatch here does not raise -- it produces a believable wrong answer, which
    is the whole reason it is a function rather than a convention.
    """
    return str(cik).strip().lstrip("0").rjust(10, "0") if str(cik).strip() else ""


@dataclass(frozen=True, slots=True)
class Inputs:
    """Every input to one valuation, and where each came from.

    ``source`` is per field rather than per row, because a row is a mixture: the
    cash flows are as-filed XBRL, the beta may be the filer's own or its peers',
    and the ERP is a constant from a paper. One provenance string for the lot
    would describe none of them.
    """

    cik: str
    company: str
    sic: int | None
    period_end: date | None
    #: ``{field: where it came from}``, e.g. ``{"beta": "peer set, 3-digit SIC"}``
    source: dict[str, str] = field(default_factory=dict)

    operating_cash_flow: float | None = None
    capex: float | None = None
    revenue: float | None = None
    assets: float | None = None
    equity: float | None = None
    liabilities: float | None = None

    beta: float | None = None
    risk_free: float | None = None
    erp: float = ERP
    growth: float = DEFAULT_GROWTH
    terminal_growth: float = TERMINAL_GROWTH

    @property
    def free_cash_flow(self) -> float | None:
        """Operating cash flow minus capex, or OCF alone where capex is absent.

        The second case is the marked one. It is not "capex = 0": it is "we do not
        know what capex was", and the difference is recorded as
        :data:`ABSENT_CAPEX` rather than buried in the arithmetic.
        """
        if self.operating_cash_flow is None:
            return None
        if self.capex is None:
            return self.operating_cash_flow
        return self.operating_cash_flow - abs(self.capex)


@dataclass(frozen=True, slots=True)
class Valuation:
    """One enterprise value, and everything that makes it less than one."""

    inputs: Inputs
    outcome: str
    #: Which of :data:`SUBSTITUTIONS` applied. Never collapsed to a count in
    #: storage: the kinds matter and they are wrong in different directions.
    substitutions: tuple[str, ...] = ()

    wacc: float | None = None
    enterprise_value: float | None = None
    #: ``{growth rate: enterprise value}`` at :data:`FLEX_GROWTH`. Computed here,
    #: with the valuation, so every consumer renders the same numbers -- see the
    #: note on that constant.
    flex: dict[float, float] = field(default_factory=dict)
    #: Present value of the explicit forecast, and of the terminal value. Split
    #: because the terminal value is usually most of the answer, and a reader who
    #: cannot see that share cannot judge the sensitivity to one growth number.
    pv_forecast: float | None = None
    pv_terminal: float | None = None

    @property
    def terminal_share(self) -> float | None:
        if not self.enterprise_value or self.pv_terminal is None:
            return None
        return self.pv_terminal / self.enterprise_value

    @property
    def clean(self) -> bool:
        """No substitutions at all -- including the ERP constant.

        Which makes it **unreachable**, and deliberately so rather than by
        oversight: the ERP is a stored constant on every row because there is no
        free source, so a literally clean DCF cannot exist here. Reporting that
        honestly is the point. :attr:`clean_but_erp` is the number worth reading.
        """
        return not self.substitutions

    #: The substitutions that are on every row because no alternative exists, so
    #: "clean apart from the unavoidable" is a number that can be read.
    UNAVOIDABLE = (ERP_CONSTANT, GROWTH_CONSTANT)
    #: Deliberately **not** in UNAVOIDABLE: GROWTH_MISMATCH is filer-specific
    #: evidence that the constant is wrong here, which is the opposite of an
    #: unavoidable assumption everyone shares.

    @property
    def clean_but_constants(self) -> bool:
        """Own beta, 4-digit comp depth, a real capex line. The best available.

        Two constants are excluded because neither has a free source: the ERP, and
        the near-term growth rate. Both are still *on the row* -- a consumer
        filtering for the best available rows should see that they are resting on
        assumptions, and the 20-name check showed exactly how much the growth one
        costs.
        """
        return set(self.substitutions) <= set(self.UNAVOIDABLE)

    @property
    def depth(self) -> int:
        """How many substitutions deep. A sort key, never a quality score."""
        return len(self.substitutions)


@dataclass(frozen=True, slots=True)
class Result:
    rows: list[Valuation]
    funnel: funnel_mod.Funnel
    outcomes: dict[str, int]
    #: ``{substitution: count}`` over valued rows.
    substitutions: dict[str, int]
    #: ``{n: count}`` -- how many rows are n substitutions deep.
    depths: dict[int, int]
    #: ``{(quarter_of_year, fiscal_year): (valued, population)}``. Never pooled:
    #: capex's q1-vs-q2 spread is 20 points and a pooled figure is a calendar
    #: artifact. See :func:`coverage_by_quarter`.
    by_quarter: dict[tuple[str, int], tuple[int, int]] = field(
        default_factory=dict)

    def lines(self) -> list[str]:
        out = list(self.funnel.lines())
        valued = [r for r in self.rows if r.outcome == VALUED]
        out.append("")
        out.append("why a filer has no valuation")
        total = sum(self.outcomes.values()) or 1
        for name in OUTCOMES:
            got = self.outcomes.get(name, 0)
            out.append(f"  {name:<18}{got:>6,}  {got / total * 100:5.1f}%  "
                       f"{OUTCOME_WHY[name][:58]}")
        out.append("")
        out.append("substitutions, over valued rows")
        for name in SUBSTITUTIONS:
            got = self.substitutions.get(name, 0)
            if not got and name == NO_BETA:
                continue
            share = got / max(1, len(valued)) * 100
            out.append(f"  {name:<20}{got:>6,}  {share:5.1f}%")
        out.append("")
        out.append("how many substitutions deep")
        for n in sorted(self.depths):
            got = self.depths[n]
            out.append(f"  {n} substitution{'s' if n != 1 else ' '}  "
                       f"{got:>6,}  {got / max(1, len(valued)) * 100:5.1f}%")
        zero = self.depths.get(0, 0)
        best = sum(1 for r in valued if r.clean_but_constants)
        out.append("")
        out.append(f"  {zero:,} rows have zero substitutions")
        if not zero:
            out.append("    -- structural, not a bug. Two constants sit on every "
                       "row because neither has a free source: the equity risk")
            out.append("       premium and the near-term growth rate. A literally "
                       "clean DCF cannot exist here, and saying so is the point.")
        out.append(f"  {best:,} rows are clean apart from the two unavoidable "
                   f"constants ({best / max(1, len(valued)) * 100:.1f}% of "
                   "valued): own beta, 4-digit comp depth, a real capex line")
        out.append("    -- the ERP and the near-term growth rate, neither of "
                   "which has a free source. Both stay on the row: the 20-name")
        out.append("       check put Amazon at 0.03x of market cap and Johnson & "
                   "Johnson at 0.96x on the same growth constant.")
        if self.by_quarter:
            out.append("")
            out.append("coverage by quarter-of-year, never pooled")
            out.append("  capex runs 84-91% in q1 and 62-69% in q2, so a pooled")
            out.append("  figure is a weighted average of four populations")
            years = sorted({y for _, y in self.by_quarter})
            out.append("        " + "".join(f"{y:>8}" for y in years))
            for q in ("q1", "q2", "q3", "q4"):
                cells = []
                for year in years:
                    got = self.by_quarter.get((q, year))
                    cells.append("       -" if not got or not got[1]
                                 else f"{got[0] / got[1] * 100:7.1f}%")
                if any(c.strip() != "-" for c in cells):
                    out.append(f"    {q}  " + "".join(cells))
        return out


def wacc(
    *, beta: float | None, risk_free: float, erp: float,
    debt: float | None, equity_value: float | None,
    tax_rate: float = TAX_RATE, debt_spread: float = DEBT_SPREAD,
) -> tuple[float, tuple[str, ...]]:
    """``(wacc, substitutions)`` from CAPM plus an after-tax cost of debt.

    Capital structure is book, not market: we have no market cap, so the weights
    come from ``equity`` and ``liabilities`` as filed. That is a real limitation
    and not a substitution in the sense this module tracks -- it applies to every
    row equally and is stated once, here, rather than counted 4,000 times.
    """
    subs: list[str] = [ERP_CONSTANT]
    if beta is None:
        cost_equity = FLAT_EQUITY_COST
        subs.append(NO_BETA)
    else:
        cost_equity = risk_free + beta * erp
    cost_debt = (risk_free + debt_spread) * (1 - tax_rate)
    total = (debt or 0.0) + (equity_value or 0.0)
    if total <= 0:
        return cost_equity, tuple(subs)
    w_debt = (debt or 0.0) / total
    return (cost_equity * (1 - w_debt) + cost_debt * w_debt), tuple(subs)


def enterprise_value(
    fcf: float, rate: float, *, growth: float, terminal_growth: float,
    horizon: int = HORIZON,
) -> tuple[float, float, float]:
    """``(ev, pv_forecast, pv_terminal)`` -- Gordon growth after the horizon.

    ``terminal_growth`` is capped below ``rate`` by the caller; this function
    refuses rather than clamps if it is not, because a terminal value computed
    through a near-zero denominator is a very large number with no error bar.
    """
    if terminal_growth >= rate:
        raise ValueError(
            f"terminal growth {terminal_growth:.3f} is not below the discount "
            f"rate {rate:.3f}; the perpetuity is infinite"
        )
    pv_forecast = 0.0
    cash = fcf
    for year in range(1, horizon + 1):
        cash = cash * (1 + growth)
        pv_forecast += cash / ((1 + rate) ** year)
    terminal = cash * (1 + terminal_growth) / (rate - terminal_growth)
    pv_terminal = terminal / ((1 + rate) ** horizon)
    return pv_forecast + pv_terminal, pv_forecast, pv_terminal


def _latest_sql(fundamentals: str) -> str:
    """One row per filer: latest annual observation of each concept, widened.

    ``row_number`` over an explicit ordering, never ``any_value``: a valuation
    that changes between two runs over the same partitions is the ``build_sponsors``
    defect with a dollar sign in front of it.
    """
    return f"""
    with ranked as (
        select cik, company, sic, concept, value, status, period_end,
               row_number() over (
                   partition by cik, concept
                   order by period_end desc, filed desc, adsh desc) as rn
        from {fundamentals}
        where sic_class = 'operating'
    )
    select cik,
           max_by(company, cik)      as company,
           max_by(sic, cik)          as sic,
           max(period_end)           as period_end,
           max(case when concept = 'operating_cash_flow' and status = 'stated'
                    then value end)  as operating_cash_flow,
           max(case when concept = 'capex' and status = 'stated'
                    then value end)  as capex,
           max(case when concept = 'revenue' and status = 'stated'
                    then value end)  as revenue,
           max(case when concept = 'assets' and status = 'stated'
                    then value end)  as assets,
           max(case when concept = 'equity' and status = 'stated'
                    then value end)  as equity,
           max(case when concept = 'liabilities' and status = 'stated'
                    then value end)  as liabilities
    from ranked where rn = 1
    group by cik
    """


def screen(
    con: duckdb.DuckDBPyConnection,
    *,
    fundamentals: str = "xb",
    betas: dict[str, float] | None = None,
    peer_betas: dict[str, tuple[float, int]] | None = None,
    growths: dict[str, float] | None = None,
    historical_growth: dict[str, float] | None = None,
    risk_free: float | None = None,
    erp: float = ERP,
    growth: float = DEFAULT_GROWTH,
    terminal_growth: float = TERMINAL_GROWTH,
    horizon: int = HORIZON,
) -> Result:
    """A valuation per operating filer, each carrying its substitutions.

    ``betas`` maps CIK to a beta computed from that filer's own returns;
    ``peer_betas`` maps CIK to ``(beta, sic_depth)`` from its peer set. Both are
    passed in rather than computed here, because beta needs prices and prices are
    vendor data under a different licence from everything else in this module --
    keeping the join at the caller keeps this file free of that boundary.
    """
    # Normalised on entry, both sides, because a CIK has two representations in
    # this codebase and they do not compare: the XBRL partitions carry it
    # unpadded ('7332') and Postgres carries it zero-padded to ten ('0000007332').
    # The first run of this passed 5,499 real betas and matched **none** of them,
    # and nothing errored -- every row simply read `no_beta`, which is a perfectly
    # plausible answer. That is the identifier rule's quieter cousin: the right
    # identifier in the wrong representation fails exactly like a name join,
    # silently and with a believable result.
    betas = {cik_key(k): v for k, v in (betas or {}).items()}
    peer_betas = {cik_key(k): v for k, v in (peer_betas or {}).items()}
    growths = ({cik_key(k): v for k, v in growths.items()}
               if growths is not None else None)
    historical_growth = {cik_key(k): v
                         for k, v in (historical_growth or {}).items()}
    rf = risk_free if risk_free is not None else RISK_FREE_FALLBACK
    cap = min(terminal_growth, rf)

    rows = con.execute(_latest_sql(fundamentals)).fetchall()
    population = len(rows)

    out: list[Valuation] = []
    outcomes = {name: 0 for name in OUTCOMES}
    subs_count = {name: 0 for name in SUBSTITUTIONS}
    depths: dict[int, int] = {}
    for (cik, company, sic, period_end, ocf, capex, revenue, assets, equity,
         liabilities) in rows:
        source: dict[str, str] = {
            "cash_flows": "XBRL, as filed, latest annual period",
            "risk_free": f"{RISK_FREE_SERIES} from macro_series"
                         if risk_free is not None else
                         f"{RISK_FREE_SERIES} unavailable; fallback constant",
            "erp": ERP_SOURCE,
            "capital_structure": "book equity and liabilities as filed -- no "
                                 "market cap exists in XBRL",
        }
        subs: list[str] = []
        if growths is None or cik_key(cik) not in growths:
            subs.append(GROWTH_CONSTANT)
            source["growth"] = (f"flat {growth:.1%} constant, not a forecast for "
                                "this filer")
            past = historical_growth.get(cik_key(cik))
            if past is not None and abs(past - growth) > GROWTH_MISMATCH_BAND:
                # The history cannot say what the rate *is* -- that was measured
                # and rejected -- but it can say the constant is not it.
                subs.append(GROWTH_MISMATCH)
                source["growth"] += (
                    f"; this filer's own history compounded at {past:+.1%}, "
                    f"more than {GROWTH_MISMATCH_BAND:.0%} away")
        beta: float | None = None
        key = cik_key(cik)
        if key in betas:
            beta = betas[key]
            source["beta"] = "the filer's own returns against the benchmark"
        elif key in peer_betas:
            beta, depth = peer_betas[key]
            subs.append(PEER_BETA)
            source["beta"] = f"peer-set median, {depth}-digit SIC"
            if depth < 4:
                subs.append(COMP_DEPTH)
        else:
            source["beta"] = "none available: no ticker, or no price history"

        if ocf is not None and capex is None:
            # Marked, never zeroed. See the module docstring.
            subs.append(ABSENT_CAPEX)
            source["capex"] = ("no capex line on the filing. FCF is operating "
                               "cash flow undiminished, which overstates it by "
                               "any capex folded into an aggregated total")
        elif capex is not None:
            source["capex"] = "XBRL, as filed"

        inputs = Inputs(
            cik=cik, company=company, sic=sic, period_end=period_end,
            source=source,
            operating_cash_flow=None if ocf is None else float(ocf),
            capex=None if capex is None else float(capex),
            revenue=None if revenue is None else float(revenue),
            assets=None if assets is None else float(assets),
            equity=None if equity is None else float(equity),
            liabilities=None if liabilities is None else float(liabilities),
            beta=beta, risk_free=rf, erp=erp,
            growth=(growths or {}).get(cik_key(cik), growth),
            terminal_growth=cap,
        )

        fcf = inputs.free_cash_flow
        rate, rate_subs = wacc(
            beta=beta, risk_free=rf, erp=erp,
            debt=inputs.liabilities, equity_value=inputs.equity,
        )
        subs.extend(rate_subs)
        ordered = tuple(s for s in SUBSTITUTIONS if s in set(subs))

        flex: dict[float, float] = {}
        if fcf is None:
            outcome, ev, pv_f, pv_t = NO_CASH_FLOW, None, None, None
        elif fcf <= 0:
            outcome, ev, pv_f, pv_t = NEGATIVE_FCF, None, None, None
        elif rate < MIN_WACC or cap >= rate:
            outcome, ev, pv_f, pv_t = NO_DISCOUNT_RATE, None, None, None
        else:
            ev, pv_f, pv_t = enterprise_value(
                fcf, rate, growth=inputs.growth, terminal_growth=cap,
                horizon=horizon)
            outcome = VALUED
            for alt in FLEX_GROWTH:
                try:
                    flex[alt] = enterprise_value(
                        fcf, rate, growth=alt, terminal_growth=cap,
                        horizon=horizon)[0]
                except ValueError:
                    # A flex point that cannot be computed is omitted rather
                    # than clamped: a renderer showing four rows where one is a
                    # substitute would be the midpoint mistake again.
                    continue
        outcomes[outcome] += 1
        if outcome == VALUED:
            for name in ordered:
                subs_count[name] += 1
            depths[len(ordered)] = depths.get(len(ordered), 0) + 1
        out.append(Valuation(
            inputs=inputs, outcome=outcome,
            substitutions=ordered if outcome == VALUED else (),
            wacc=rate if outcome == VALUED else None,
            enterprise_value=ev, pv_forecast=pv_f, pv_terminal=pv_t,
            flex=flex,
        ))

    has_ocf = sum(1 for r in out if r.inputs.operating_cash_flow is not None)
    positive = sum(1 for r in out
                   if (r.inputs.free_cash_flow or 0) > 0)
    stages = [
        ("operating filers", population,
         "one latest annual observation each, financials excluded at load"),
        ("operating cash flow", has_ocf,
         "98.6% of filers; the half of FCF that is not the constraint"),
        ("positive free cash flow", positive,
         "OCF minus capex, or OCF alone where no capex line exists -- marked, "
         "never zeroed"),
        ("valued", outcomes[VALUED],
         f"a WACC at or above {MIN_WACC:.1%} and a terminal growth below it"),
    ]
    result = Result(
        rows=out, funnel=funnel_mod.build(SCREEN, *stages), outcomes=outcomes,
        substitutions=subs_count, depths=depths,
        by_quarter=coverage_by_quarter(out),
    )
    for line in result.lines():
        log.debug("%s", line)
    return result


def coverage_by_quarter(
    rows: list[Valuation],
) -> dict[tuple[str, int], tuple[int, int]]:
    """``{(q, fiscal year): (valued, population)}`` -- never pooled.

    **Capex has a 20-point q1-against-q2 spread**, measured across 30 quarters:
    q1 runs 84-91% and q2 runs 62-69%, because q1 carries the December fiscal
    year ends and q2-q4 are retailers and other odd-year-end filers at a fifth
    the volume. A pooled coverage figure is a weighted average of four different
    populations, and it moves when the calendar does rather than when the data
    does. The same mistake the XBRL span metric made by reading 2019q1 against
    2026q2 and reporting revenue falling 8.1pp.
    """
    out: dict[tuple[str, int], list[int]] = {}
    for row in rows:
        period = row.inputs.period_end
        if period is None:
            continue
        key = (f"q{(period.month - 1) // 3 + 1}", period.year)
        cell = out.setdefault(key, [0, 0])
        cell[1] += 1
        if row.outcome == VALUED:
            cell[0] += 1
    return {k: (v[0], v[1]) for k, v in out.items()}


def read_risk_free(con: duckdb.DuckDBPyConnection, *, alias: str) -> float | None:
    """The newest DGS10 observation, as a decimal. None if absent.

    FRED is local-only and has no publish path -- Treasury data is public domain
    but the ICE series in the same table are not, and splitting one API call
    across two destinations by licence buys nothing. So this reads Postgres and
    the caller records where the number came from.
    """
    try:
        row = con.execute(
            f"select value from {alias}.macro_series "
            f"where series_id = ? order by obs_date desc limit 1",
            [RISK_FREE_SERIES],
        ).fetchone()
    except Exception as exc:            # no Postgres attached, or no table
        log.warning("risk-free rate unavailable: %s", exc)
        return None
    if not row or row[0] is None:
        return None
    return float(row[0]) / 100.0


def peer_betas(
    con: duckdb.DuckDBPyConnection,
    own: dict[str, float],
    *,
    fundamentals: str = "xb",
) -> dict[str, tuple[float, int]]:
    """``{cik: (median peer beta, sic_depth)}`` for filers with no own beta.

    Composition, not a second implementation: :func:`comps.peer_median` runs the
    same SIC-depth ladder the peer-set screen does, so the two surfaces cannot
    disagree about how good a peer group was. The depth comes back with the number
    because the row has to record it -- a beta from a widened industry is of a
    broader group than its label implies, and that is :data:`COMP_DEPTH`.
    """
    from marketradar.screens import comps

    medians = comps.peer_median(con, own, fundamentals=fundamentals)
    return {cik: (median, depth) for cik, (median, depth, _n) in medians.items()}


def beta_from_returns(
    ticker: list[float], benchmark: list[float],
) -> float | None:
    """Ordinary least squares slope of ticker returns on benchmark returns.

    Returns None rather than a number in three cases, and all three are the same
    principle: a figure that cannot mean anything must not be indistinguishable
    from one that does.

    * fewer than 52 weekly observations -- a beta from twelve weeks is noise;
    * a benchmark with no variance -- the slope is undefined;
    * a slope outside :data:`BETA_BOUNDS` -- a failed regression. Measured: an
      illiquid ticker produced **-5,886**, which in CAPM is a negative cost of
      equity that would pass the WACC floor from the wrong side.

    None sends the filer to the peer-set fallback, which is marked on the row.
    Clamping instead would turn -5,886 into -3.0 and make it look measured.
    """
    n = min(len(ticker), len(benchmark))
    if n < 52:
        return None
    xs, ys = benchmark[:n], ticker[:n]
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    var = sum((x - mx) ** 2 for x in xs)
    if var <= 0 or math.isclose(var, 0.0):
        return None
    beta = cov / var
    lo, hi = BETA_BOUNDS
    if not (lo <= beta <= hi) or not math.isfinite(beta):
        return None
    return beta
