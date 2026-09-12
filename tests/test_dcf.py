"""The DCF: substitutions on the row, absent capex marked, coverage by quarter.

The arithmetic is the easy part and is tested directly. Everything else here exists
because the 20-filer check found it: a growth constant that was not on the row, a
beta of -5,886 that passed CAPM, and a market cap off by 1000x from a segment row.
"""

from __future__ import annotations

from datetime import date

import duckdb
import pytest

from marketradar.screens import dcf

COLUMNS = ("cik", "company", "sic", "sic_class", "concept", "value", "status",
           "period_end", "filed", "adsh")


def rows_for(cik: str, company: str, *, ocf, capex, revenue=500_000_000,
             assets=1_000_000_000, equity=600_000_000,
             liabilities=400_000_000, sic: int = 3674,
             period_end: str = "2025-12-31") -> list[tuple]:
    """One filer's seven concept rows, in the long shape the loader writes."""
    out = []
    for concept, value in (("operating_cash_flow", ocf), ("capex", capex),
                           ("revenue", revenue), ("assets", assets),
                           ("equity", equity), ("liabilities", liabilities)):
        out.append((cik, company, sic, "operating", concept,
                    None if value is None else float(value),
                    "stated" if value is not None else "absent",
                    period_end, period_end, f"{cik}-{period_end}"))
    return out


def build(con: duckdb.DuckDBPyConnection, rows: list[tuple]) -> None:
    con.execute("""
    create or replace table xb (
        cik varchar, company varchar, sic integer, sic_class varchar,
        concept varchar, value decimal(28,4), status varchar,
        period_end date, filed date, adsh varchar)
    """)
    con.executemany("insert into xb values (?,?,?,?,?,?,?,?,?,?)", rows)


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    return duckdb.connect()


# --- absent capex is marked, never zero --------------------------------


def test_absent_capex_is_marked_and_the_row_is_not_clean(con) -> None:
    """**The rule, and it is not cosmetic.** 22.7% of operating filers present no
    capex line and `unmapped` is 0%, so there is no map work left -- the line
    genuinely is not there. But "no line" and "zero" are different facts: a filer
    that folded capex into an aggregated investing total reports no capex line
    either, and treating that as zero overstates free cash flow by exactly the
    buried amount.

    So the valuation still happens -- refusing would discard a quarter of the
    universe -- and it carries ``absent_capex``, which is what stops it sorting
    beside a filer whose capex is known to be nil.
    """
    build(con, rows_for("1", "No Capex Line Inc", ocf=100_000_000, capex=None))
    result = dcf.screen(con)
    row = result.rows[0]
    assert row.outcome == dcf.VALUED
    assert dcf.ABSENT_CAPEX in row.substitutions
    assert not row.clean
    assert not row.clean_but_constants, (
        "an absent capex line is not one of the unavoidable constants")
    # FCF is OCF undiminished, and the source says so rather than implying a zero.
    assert row.inputs.free_cash_flow == 100_000_000
    assert "no capex line" in row.inputs.source["capex"]
    assert "overstates" in row.inputs.source["capex"]


def test_a_real_capex_line_is_subtracted_and_not_marked(con) -> None:
    build(con, rows_for("1", "Clean Inc", ocf=100_000_000, capex=30_000_000))
    row = dcf.screen(con).rows[0]
    assert row.inputs.free_cash_flow == 70_000_000
    assert dcf.ABSENT_CAPEX not in row.substitutions


def test_capex_sign_is_not_trusted(con) -> None:
    """XBRL reports payments as positive, but a filer that signs it the other way
    must not have its capex *added* to cash flow."""
    build(con, rows_for("1", "Negative Sign Inc", ocf=100_000_000,
                        capex=-30_000_000))
    row = dcf.screen(con).rows[0]
    assert row.inputs.free_cash_flow == 70_000_000


# --- the substitution list ---------------------------------------------


def test_no_row_has_zero_substitutions_and_that_is_structural(con) -> None:
    """Two constants sit on every row because neither has a free source: the
    equity risk premium and the near-term growth rate.

    Asserted rather than quietly arranged: a reader filtering for clean rows has
    to find out from the data that there are none, and the honest version of
    "how many are clean" is :attr:`clean_but_constants`.
    """
    build(con, rows_for("1", "Best Case Inc", ocf=100_000_000,
                        capex=30_000_000))
    result = dcf.screen(con, betas={"1": 1.1})
    row = result.rows[0]
    assert row.outcome == dcf.VALUED
    assert not row.clean
    assert set(row.substitutions) == {dcf.ERP_CONSTANT, dcf.GROWTH_CONSTANT}
    assert row.clean_but_constants
    assert result.depths.get(0, 0) == 0
    text = "\n".join(result.lines())
    assert "0 rows have zero substitutions" in text
    assert "structural, not a bug" in text


def test_the_growth_constant_is_on_the_row(con) -> None:
    """**Added after the 20-name check, which is what the check was for.**

    Against rough market caps the mature cohort lands near parity -- AbbVie 1.75x,
    Chevron 1.25x, Mastercard 1.08x, J&J 0.96x -- while heavy reinvestors come out
    absurd: Amazon 0.03x, Tesla 0.07x, AMD 0.09x, NVIDIA 0.22x. Every one of those
    is arithmetically correct; one growth rate simply cannot describe both J&J and
    NVIDIA. Leaving it off the substitution list made 1,584 rows look cleaner than
    they were.
    """
    build(con, rows_for("1", "Grower Inc", ocf=100_000_000, capex=10_000_000))
    row = dcf.screen(con, betas={"1": 1.0}).rows[0]
    assert dcf.GROWTH_CONSTANT in row.substitutions
    assert "not a forecast for this filer" in row.inputs.source["growth"]

    # And a caller who supplies one per filer is not charged for it.
    row = dcf.screen(con, betas={"1": 1.0}, growths={"1": 0.12}).rows[0]
    assert dcf.GROWTH_CONSTANT not in row.substitutions
    assert row.inputs.growth == 0.12


def test_a_peer_beta_is_marked_and_so_is_its_depth(con) -> None:
    """Two substitutions, not one. A peer beta says the risk is the group's; a
    fallen-back depth says the group is broader than the label implies."""
    build(con, rows_for("1", "No Ticker Inc", ocf=100_000_000,
                        capex=10_000_000))
    row = dcf.screen(con, peer_betas={"1": (0.9, 2)}).rows[0]
    assert dcf.PEER_BETA in row.substitutions
    assert dcf.COMP_DEPTH in row.substitutions
    assert "2-digit" in row.inputs.source["beta"]

    # A 4-digit peer set is still a peer beta, but not a depth fallback.
    row = dcf.screen(con, peer_betas={"1": (0.9, 4)}).rows[0]
    assert dcf.PEER_BETA in row.substitutions
    assert dcf.COMP_DEPTH not in row.substitutions


def test_own_beta_beats_a_peer_beta(con) -> None:
    build(con, rows_for("1", "Both Inc", ocf=100_000_000, capex=10_000_000))
    row = dcf.screen(con, betas={"1": 1.4},
                     peer_betas={"1": (0.8, 2)}).rows[0]
    assert row.inputs.beta == 1.4
    assert dcf.PEER_BETA not in row.substitutions
    assert dcf.COMP_DEPTH not in row.substitutions
    assert "own returns" in row.inputs.source["beta"]


def test_no_beta_at_all_is_the_weakest_row_and_says_so(con) -> None:
    build(con, rows_for("1", "Dark Inc", ocf=100_000_000, capex=10_000_000))
    row = dcf.screen(con).rows[0]
    assert dcf.NO_BETA in row.substitutions
    assert row.inputs.beta is None
    assert "none available" in row.inputs.source["beta"]


def test_substitutions_are_a_list_not_a_score(con) -> None:
    """A peer beta and an absent capex line are wrong in *different directions*,
    so collapsing them into one quality number would describe neither."""
    build(con, rows_for("1", "Deep Inc", ocf=100_000_000, capex=None))
    row = dcf.screen(con, peer_betas={"1": (0.9, 2)}).rows[0]
    assert row.depth == 5
    assert set(row.substitutions) == {
        dcf.PEER_BETA, dcf.COMP_DEPTH, dcf.ABSENT_CAPEX, dcf.ERP_CONSTANT,
        dcf.GROWTH_CONSTANT}
    # Every substitution states the direction it pushes the answer.
    for name in row.substitutions:
        assert dcf.SUBSTITUTION_WHY[name]


def test_the_row_order_follows_the_declared_order(con) -> None:
    """So two rows with the same substitutions compare equal as tuples, and a
    consumer can group on the field without normalising it first."""
    build(con, rows_for("1", "A", ocf=100_000_000, capex=None))
    row = dcf.screen(con, peer_betas={"1": (1.0, 2)}).rows[0]
    assert list(row.substitutions) == [
        s for s in dcf.SUBSTITUTIONS if s in set(row.substitutions)]


# --- the CIK representation trap ---------------------------------------


def test_a_padded_cik_still_finds_its_beta(con) -> None:
    """**The quiet cousin of the identifier rule.** The XBRL partitions carry a
    CIK unpadded and Postgres carries it zero-padded to ten, and the two strings
    do not compare. The first run of this module passed 5,499 real betas and
    matched none of them -- nothing errored, every row simply read ``no_beta``,
    which is a perfectly plausible answer.

    Right identifier, wrong representation, fails exactly like a name join.
    """
    build(con, rows_for("7332", "Unpadded Inc", ocf=100_000_000,
                        capex=10_000_000))
    row = dcf.screen(con, betas={"0000007332": 1.3}).rows[0]
    assert row.inputs.beta == 1.3, "a zero-padded key missed an unpadded CIK"
    assert dcf.NO_BETA not in row.substitutions
    assert dcf.cik_key("7332") == dcf.cik_key("0000007332")


# --- the arithmetic ----------------------------------------------------


def test_the_terminal_value_share_is_reported(con) -> None:
    """It is usually most of the answer, and a reader who cannot see that share
    cannot judge the sensitivity to one growth number."""
    build(con, rows_for("1", "Mature Inc", ocf=100_000_000, capex=20_000_000))
    row = dcf.screen(con, betas={"1": 0.8}).rows[0]
    assert row.pv_forecast and row.pv_terminal
    assert row.enterprise_value == pytest.approx(
        row.pv_forecast + row.pv_terminal)
    assert 0.3 < (row.terminal_share or 0) < 0.95


def test_terminal_growth_at_or_above_the_discount_rate_is_refused() -> None:
    """A perpetuity growing at the discount rate is infinite. Refused rather than
    clamped, for the same reason a missing split is never inferred from a price
    jump: a clamped number looks like a measurement."""
    with pytest.raises(ValueError, match="infinite"):
        dcf.enterprise_value(100.0, 0.05, growth=0.03, terminal_growth=0.05)
    with pytest.raises(ValueError, match="infinite"):
        dcf.enterprise_value(100.0, 0.05, growth=0.03, terminal_growth=0.08)


def test_a_wacc_below_the_floor_is_rejected_not_clamped(con) -> None:
    build(con, rows_for("1", "Thin Inc", ocf=100_000_000, capex=10_000_000))
    result = dcf.screen(con, betas={"1": -2.9}, risk_free=0.01, erp=0.0433)
    row = result.rows[0]
    assert row.outcome == dcf.NO_DISCOUNT_RATE
    assert row.enterprise_value is None


def test_negative_free_cash_flow_is_excluded_with_a_reason(con) -> None:
    """52% of the universe has negative operating cash flow -- 3,299 of 6,344
    filers, dominated by companies with no revenue or under $100M. A growing
    perpetuity of a negative number is a confident-looking negative value, and the
    method simply does not apply."""
    build(con, rows_for("1", "Burner Inc", ocf=-50_000_000, capex=10_000_000))
    result = dcf.screen(con)
    assert result.rows[0].outcome == dcf.NEGATIVE_FCF
    assert result.rows[0].enterprise_value is None
    assert "does not apply" in dcf.OUTCOME_WHY[dcf.NEGATIVE_FCF]


def test_no_operating_cash_flow_is_distinct_from_negative(con) -> None:
    """One is a fact about the filing, the other about the business."""
    build(con, rows_for("1", "Silent Inc", ocf=None, capex=10_000_000))
    assert dcf.screen(con).rows[0].outcome == dcf.NO_CASH_FLOW


# --- beta ---------------------------------------------------------------


def test_an_implausible_beta_is_rejected_rather_than_clamped() -> None:
    """**Measured, not hypothetical.** A first pass over 5,500 CIKs returned a
    minimum beta of -5,886: an illiquid ticker, mostly flat with one enormous
    week, against a benchmark with tiny variance over the same weeks. In CAPM that
    is a negative cost of equity, which would then pass the WACC floor from the
    wrong side.

    Clamping would turn it into -3.0 and make it look measured. None sends the
    filer to the peer-set fallback, which is marked on the row.
    """
    flat = [0.001] * 60
    spike = [0.0] * 59 + [5.0]
    assert dcf.beta_from_returns(spike, flat) is None
    lo, hi = dcf.BETA_BOUNDS
    assert lo < 0 < hi


def test_a_short_series_has_no_beta() -> None:
    assert dcf.beta_from_returns([0.01] * 20, [0.01] * 20) is None


def test_a_flat_benchmark_has_no_beta() -> None:
    assert dcf.beta_from_returns([0.01, -0.02] * 40, [0.0] * 80) is None


def test_a_real_beta_comes_out_of_real_returns() -> None:
    bench = [0.01 * (1 if i % 2 else -1) for i in range(80)]
    stock = [2.0 * r for r in bench]
    got = dcf.beta_from_returns(stock, bench)
    assert got is not None and got == pytest.approx(2.0)


# --- coverage by quarter ------------------------------------------------


def test_coverage_is_reported_by_quarter_of_year_never_pooled(con) -> None:
    """**Capex has a 20-point q1-against-q2 spread** -- q1 runs 84-91% and q2 runs
    62-69% -- because q1 carries the December fiscal year ends and q2-q4 are
    retailers and other odd-year-end filers at a fifth the volume. A pooled
    coverage figure is a weighted average of four populations and moves when the
    calendar does, which is the mistake the XBRL span metric made.
    """
    rows = rows_for("1", "Dec Year End", ocf=100_000_000, capex=10_000_000,
                    period_end="2025-12-31")
    rows += rows_for("2", "Jun Year End", ocf=100_000_000, capex=None,
                     period_end="2025-06-30")
    rows += rows_for("3", "Jun Burner", ocf=-10_000_000, capex=None,
                     period_end="2025-06-30")
    build(con, rows)
    result = dcf.screen(con)
    assert result.by_quarter[("q4", 2025)] == (1, 1)
    assert result.by_quarter[("q2", 2025)] == (1, 2)
    text = "\n".join(result.lines())
    assert "never pooled" in text
    assert "q2" in text


def test_the_funnel_says_what_each_stage_removed(con) -> None:
    rows = rows_for("1", "Valued", ocf=100_000_000, capex=10_000_000)
    rows += rows_for("2", "Burner", ocf=-5_000_000, capex=1_000_000)
    rows += rows_for("3", "Silent", ocf=None, capex=1_000_000)
    build(con, rows)
    result = dcf.screen(con)
    names = [s.name for s in result.funnel.stages]
    assert names == ["operating filers", "operating cash flow",
                     "positive free cash flow", "valued"]
    by = {s.name: s.remaining for s in result.funnel.stages}
    assert by["operating filers"] == 3
    assert by["operating cash flow"] == 2
    assert by["positive free cash flow"] == 1
    assert all(s.why for s in result.funnel.stages)


# --- the recorded decisions --------------------------------------------


def test_the_erp_is_a_dated_sourced_constant(con) -> None:
    """A required input means a DCF that produces nothing until somebody types a
    number, which in practice means somebody types 5.0 and it is recorded nowhere.
    So: a constant, with its date and its attribution on the row."""
    assert 0.0 < dcf.ERP < 0.12
    assert dcf.ERP_AS_OF.year >= 2026
    assert "Damodaran" in dcf.ERP_SOURCE
    assert "2026-09-01" in dcf.ERP_SOURCE
    build(con, rows_for("1", "Any Inc", ocf=100_000_000, capex=10_000_000))
    row = dcf.screen(con).rows[0]
    assert row.inputs.source["erp"] == dcf.ERP_SOURCE


def test_terminal_growth_is_capped_at_the_risk_free_rate(con) -> None:
    """A perpetuity growing faster than long-run nominal GDP eventually becomes
    the economy, and rf is the available proxy for that ceiling."""
    build(con, rows_for("1", "Any Inc", ocf=100_000_000, capex=10_000_000))
    row = dcf.screen(con, betas={"1": 1.0}, risk_free=0.012,
                     terminal_growth=0.05).rows[0]
    assert row.inputs.terminal_growth <= 0.012


def test_the_capital_structure_is_book_and_the_row_says_so(con) -> None:
    """No market cap exists in XBRL, so the WACC weights are book. Stated once on
    every row rather than as a caveat somebody has to remember."""
    build(con, rows_for("1", "Any Inc", ocf=100_000_000, capex=10_000_000))
    row = dcf.screen(con).rows[0]
    assert "no market cap exists" in row.inputs.source["capital_structure"]


def test_a_financial_filer_never_enters_the_population(con) -> None:
    rows = rows_for("1", "Operating", ocf=100_000_000, capex=10_000_000)
    rows += [(r[0], r[1], 6022, "financial", *r[4:])
             for r in rows_for("2", "A Bank", ocf=100_000_000, capex=1_000_000)]
    build(con, rows)
    assert [r.inputs.cik for r in dcf.screen(con).rows] == ["1"]


# --- the growth decision: measured, and the constant won ----------------


def test_a_fitted_growth_rate_is_rejected_and_the_reason_is_recorded() -> None:
    """**The measurement said keep the constant, so the constant stayed.**

    Out of sample over 30 quarters -- a rate fitted on the first half of each
    filer's annual history, scored against the growth actually realised in the
    second half, beside a flat 3% on the same filers and years:

        revenue, CAGR endpoints    n=2,753  corr +0.010  fitted 0.138  const 0.089
        revenue, log-linear        n=2,606  corr -0.035  fitted 0.129  const 0.084
        free cash flow, CAGR       n=1,585  corr +0.079  fitted 0.365  const 0.227
        free cash flow, log-linear n=1,236  corr -0.011  fitted 0.311  const 0.199

    The constant wins all four and the past-to-future correlation is
    indistinguishable from zero -- negative for both log-linear fits. A fitted rate
    would be ~50% worse on held-out data *while looking filer-specific*, and the
    substitution list would stop warning about it. That is strictly worse than an
    honest constant, which is why this is a recorded rejection and not a TODO.
    """
    assert "loses to the flat" in dcf.GROWTH_FITTING_REJECTED
    assert "-0.035" in dcf.GROWTH_FITTING_REJECTED
    assert dcf.DEFAULT_GROWTH == 0.03


def test_the_history_flags_the_constant_rather_than_replacing_it(con) -> None:
    """What the history *can* support: not the rate, only that 3% is not it.

    Scored on the 20 large caps: the flag catches **7 of the 8** names trading
    above 3.3x the DCF, with one false alarm in the 12 already in range. It misses
    Texas Instruments at 0.12x, whose own history is flat while the market prices a
    recovery -- a backward-looking flag cannot see a forward-looking re-rating, and
    that limit is the point of flagging rather than fitting.
    """
    build(con, rows_for("1", "Fast Grower Inc", ocf=100_000_000,
                        capex=20_000_000))
    row = dcf.screen(con, betas={"1": 1.0},
                     historical_growth={"1": 0.40}).rows[0]
    assert dcf.GROWTH_MISMATCH in row.substitutions
    assert dcf.GROWTH_CONSTANT in row.substitutions, (
        "the constant is still what was used; the flag does not replace it")
    assert row.inputs.growth == dcf.DEFAULT_GROWTH
    assert "compounded at +40.0%" in row.inputs.source["growth"]

    # An ordinary company is not flagged.
    row = dcf.screen(con, betas={"1": 1.0},
                     historical_growth={"1": 0.05}).rows[0]
    assert dcf.GROWTH_MISMATCH not in row.substitutions

    # Shrinking counts too: the band is two-sided.
    row = dcf.screen(con, betas={"1": 1.0},
                     historical_growth={"1": -0.20}).rows[0]
    assert dcf.GROWTH_MISMATCH in row.substitutions


def test_the_mismatch_flag_is_not_one_of_the_unavoidable_constants(con) -> None:
    """It is filer-specific *evidence*, which is the opposite of an assumption
    everybody shares. Folding it into ``clean_but_constants`` would hide exactly
    the rows where the constant is least likely to hold."""
    build(con, rows_for("1", "Fast Inc", ocf=100_000_000, capex=20_000_000))
    row = dcf.screen(con, betas={"1": 1.0},
                     historical_growth={"1": 0.40}).rows[0]
    assert not row.clean_but_constants
    assert dcf.GROWTH_MISMATCH not in dcf.Valuation.UNAVOIDABLE


def test_a_caller_supplied_growth_is_neither_constant_nor_mismatch(con) -> None:
    """An explicit forecast is a real input, so it is charged for neither."""
    build(con, rows_for("1", "Forecast Inc", ocf=100_000_000,
                        capex=20_000_000))
    row = dcf.screen(con, betas={"1": 1.0}, growths={"1": 0.11},
                     historical_growth={"1": 0.40}).rows[0]
    assert dcf.GROWTH_CONSTANT not in row.substitutions
    assert dcf.GROWTH_MISMATCH not in row.substitutions
    assert row.inputs.growth == 0.11
