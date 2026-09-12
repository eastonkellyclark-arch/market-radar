"""Peer sets: the ladder, the floor, and saying so when a set is degraded.

Fixtures are built by hand with the shapes the measurement found in the real
data, because that is what the screen is arranged around -- a dense SIC code full
of pre-revenue filers, a thin code that forces a fallback, and a filer that is
below the floor itself. A fixture of generic companies would test the SQL and not
the decisions.
"""

from __future__ import annotations

import duckdb
import pytest

from marketradar.screens import comps

#: The resolved-fundamentals columns the screen reads. Written out so a schema
#: change in `xbrl/resolve.py` breaks this test rather than silently producing an
#: empty peer set.
COLUMNS = ("cik", "company", "sic", "sic_class", "concept", "value", "status",
           "period_end", "filed", "adsh")


def rows_for(cik: str, company: str, sic: int, *, revenue, assets,
             period_end: str = "2025-12-31", adsh: str | None = None,
             sic_class: str = "operating") -> list[tuple]:
    """Two concept rows for one filer, in the long shape the loader writes."""
    out = []
    for concept, value in (("revenue", revenue), ("assets", assets)):
        out.append((cik, company, sic, sic_class, concept,
                    None if value is None else float(value),
                    "stated" if value is not None else "absent",
                    period_end, period_end, adsh or f"{cik}-{period_end}"))
    return out


def build(con: duckdb.DuckDBPyConnection, rows: list[tuple]) -> None:
    con.execute("""
    create or replace table xb (
        cik varchar, company varchar, sic integer, sic_class varchar,
        concept varchar, value decimal(28,4), status varchar,
        period_end date, filed date, adsh varchar)
    """)
    con.executemany(
        "insert into xb values (?,?,?,?,?,?,?,?,?,?)", rows)


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    return duckdb.connect()


def dense(sic: int, n: int, *, revenue, assets, prefix: str) -> list[tuple]:
    out: list[tuple] = []
    for i in range(n):
        out.extend(rows_for(f"{prefix}{i:03d}", f"{prefix} {i}", sic,
                            revenue=revenue, assets=assets))
    return out


# --- the ladder ---------------------------------------------------------


def test_a_dense_four_digit_code_stays_at_four_digits(con) -> None:
    """The ask, when the ask is available. Recording the depth only matters
    because it is often *not* 4, so the happy path has to be pinned too."""
    rows = dense(3674, 12, revenue=50_000_000, assets=100_000_000, prefix="A")
    build(con, rows)
    result = comps.screen(con)
    served = [r for r in result.rows if r.outcome == comps.SERVED]
    assert len(served) == 12
    assert {r.sic_depth for r in served} == {4}
    assert not any(r.degraded for r in served)
    assert result.depths[4] == 12


def test_a_thin_code_falls_back_and_the_row_says_so(con) -> None:
    """The measured common case: 14.2% of filers live in a 4-digit code with
    fewer than 8 members, so for them the digit is not a choice."""
    rows = dense(3674, 3, revenue=50_000_000, assets=100_000_000, prefix="T")
    # Same 3-digit group (367x), enough to clear the floor together.
    rows += dense(3672, 9, revenue=50_000_000, assets=100_000_000, prefix="U")
    build(con, rows)
    result = comps.screen(con)
    thin = [r for r in result.rows if r.cik.startswith("T")]
    assert all(r.outcome == comps.SERVED for r in thin)
    assert {r.sic_depth for r in thin} == {3}
    assert all(r.depth_fell_back for r in thin)
    assert all("sic_3digit" in r.degraded for r in thin)
    assert all("3-digit" in (r.caveat() or "") for r in thin)


def test_the_ladder_stops_at_two_digits(con) -> None:
    """There is no coarser code to fall back to, so a filer still short at two
    digits is ``too_few_peers`` -- which is a different fact from being below the
    floor itself, and the two were one bucket in the first draft."""
    rows = dense(3674, 2, revenue=50_000_000, assets=100_000_000, prefix="L")
    build(con, rows)
    result = comps.screen(con)
    assert {r.outcome for r in result.rows} == {comps.TOO_FEW_PEERS}
    assert all(r.sic_depth is None for r in result.rows)
    assert result.outcomes[comps.TOO_FEW_PEERS] == 2
    assert result.outcomes[comps.SERVED] == 0


def test_every_column_comes_from_the_depth_that_won(con) -> None:
    """The counts and the ratios must not come from different depths.

    A correlated subquery per column would let ``peers_banded`` be read at
    4-digit while the median came from 2-digit, and the row would be internally
    inconsistent in a way no single number reveals.
    """
    rows = dense(3674, 3, revenue=50_000_000, assets=100_000_000, prefix="C")
    rows += dense(3672, 20, revenue=50_000_000, assets=100_000_000, prefix="D")
    build(con, rows)
    result = comps.screen(con)
    row = next(r for r in result.rows if r.cik.startswith("C"))
    assert row.sic_depth == 3
    # 2 same-code peers + 20 in the 3-digit group = 22, not the 2 a 4-digit read
    # would give and not the 22 a 2-digit read would coincidentally also give.
    assert row.peers_banded == 22
    assert row.peers_material == 22


# --- the size band ------------------------------------------------------


def test_the_band_excludes_a_peer_of_the_wrong_size(con) -> None:
    """Measured: the band is what tightens the set, not the SIC digit. Margin
    IQR is 0.956 with no band and 0.613 inside a 3x one."""
    rows = dense(3674, 9, revenue=50_000_000, assets=100_000_000, prefix="S")
    rows += rows_for("BIG", "Enormous Inc", 3674,
                     revenue=50_000_000_000, assets=100_000_000_000)
    build(con, rows)
    result = comps.screen(con)
    small = next(r for r in result.rows if r.cik == "S000")
    assert small.peers_banded == 8, "the 1000x peer was admitted"
    big = next(r for r in result.rows if r.cik == "BIG")
    assert big.outcome == comps.TOO_FEW_PEERS


def test_the_band_keys_on_assets_not_revenue(con) -> None:
    """Assets is stated on 97.8% of operating filers against revenue's 81.1%, so
    a revenue-keyed band would drop the pre-revenue population silently rather
    than reporting it as ``immaterial``."""
    assert comps.SIZE_CONCEPT == "assets"
    rows = dense(3674, 9, revenue=50_000_000, assets=100_000_000, prefix="B")
    rows += rows_for("NOREV", "Pre Revenue Inc", 3674,
                     revenue=None, assets=100_000_000)
    build(con, rows)
    result = comps.screen(con)
    norev = next(r for r in result.rows if r.cik == "NOREV")
    # Placed and banded -- it has assets -- and excluded by the floor, not by
    # being invisible.
    assert norev.outcome == comps.IMMATERIAL
    assert norev.outcome != comps.UNPLACEABLE


# --- the materiality floor ---------------------------------------------


def test_a_near_zero_denominator_is_kept_out_of_the_set(con) -> None:
    """Why the floor exists, and it is not a similarity floor.

    Raising it to $50M tightens turnover IQR only 0.439 -> 0.346 while dropping
    coverage 82.6% -> 53.9%, so similarity does not justify it. What justifies it
    is that a peer with $200k of revenue and $50M of assets contributes a 250x
    ratio that is a rounding artifact wearing a valuation's units.
    """
    rows = dense(2834, 9, revenue=50_000_000, assets=100_000_000, prefix="M")
    rows += dense(2834, 20, revenue=200_000, assets=100_000_000, prefix="N")
    build(con, rows)
    result = comps.screen(con)
    big = next(r for r in result.rows if r.cik == "M000")
    assert big.peers_banded == 28, "the band should still have selected them"
    assert big.peers_material == 8, "the floor should have removed them"
    assert big.thinned, "losing 20 of 28 peers was not reported"
    # And the ratio is computed over the material set only.
    assert big.turnover_median == pytest.approx(0.5)


def test_a_filer_below_the_floor_is_immaterial_not_unserved(con) -> None:
    """Two different facts. No amount of widening the industry fixes being
    below the floor yourself, and the first draft had them in one bucket."""
    rows = dense(2834, 12, revenue=50_000_000, assets=100_000_000, prefix="P")
    rows += rows_for("TINY", "Tiny Inc", 2834,
                     revenue=100_000, assets=100_000_000)
    build(con, rows)
    result = comps.screen(con)
    tiny = next(r for r in result.rows if r.cik == "TINY")
    assert tiny.outcome == comps.IMMATERIAL
    assert result.outcomes[comps.IMMATERIAL] == 1
    assert result.outcomes[comps.TOO_FEW_PEERS] == 0


def test_no_sic_is_unplaceable_and_says_which(con) -> None:
    rows = dense(3674, 12, revenue=50_000_000, assets=100_000_000, prefix="Q")
    rows += rows_for("NOSIC", "No Sic Inc", None,
                     revenue=50_000_000, assets=100_000_000)
    build(con, rows)
    result = comps.screen(con)
    got = next(r for r in result.rows if r.cik == "NOSIC")
    assert got.outcome == comps.UNPLACEABLE
    assert got.sic_depth is None


# --- saying both --------------------------------------------------------


def test_a_set_degraded_on_both_axes_reports_both(con) -> None:
    """The row the whole honesty apparatus exists for.

    Measured at the $10M floor: 25 of 3,467 served sets fell back to 2-digit
    *and* lost over half their members. Reporting only the worse of the two would
    make those look singly degraded, and a clean median would hide each.
    """
    # All three codes share the 2-digit group 36 and differ at 3 digits, so the
    # ladder has to fall all the way: X is alone at 3674 and alone at 367.
    rows = dense(3674, 1, revenue=50_000_000, assets=100_000_000, prefix="X")
    rows += dense(3612, 9, revenue=50_000_000, assets=100_000_000, prefix="Y")
    rows += dense(3652, 30, revenue=100_000, assets=100_000_000, prefix="Z")
    build(con, rows)
    result = comps.screen(con)
    row = next(r for r in result.rows if r.cik == "X000")
    assert row.outcome == comps.SERVED
    assert row.sic_depth == 2, "the ladder did not widen"
    assert row.depth_fell_back and row.thinned
    assert len(row.degraded) == 2, f"only reported {row.degraded}"
    caveat = row.caveat() or ""
    assert "2-digit" in caveat and "revenue floor" in caveat
    # And the report names the axes separately rather than lumping them.
    text = "\n".join(result.lines())
    assert "industry widened" in text
    assert "thinned" in text
    assert "both" in text


def test_a_clean_set_has_no_caveat(con) -> None:
    rows = dense(3674, 12, revenue=50_000_000, assets=100_000_000, prefix="K")
    build(con, rows)
    result = comps.screen(con)
    row = next(r for r in result.rows if r.cik == "K000")
    assert row.degraded == ()
    assert row.caveat() is None


# --- the similarity diagnostic -----------------------------------------


def test_similarity_is_asset_turnover_not_net_margin(con) -> None:
    """Net margin is unusable where revenue is near zero. SIC 2834 is 793 filers
    -- 12.3% of the universe -- with a within-code margin IQR of 15.2 and a
    *median* of -1.7, because 53% of them report under $1M of revenue. Turnover
    has assets underneath it, and every filer has assets.
    """
    assert comps.SIMILARITY == "asset_turnover"
    rows = dense(3674, 6, revenue=50_000_000, assets=100_000_000, prefix="E")
    rows += dense(3674, 6, revenue=150_000_000, assets=100_000_000, prefix="F")
    build(con, rows)
    result = comps.screen(con)
    row = next(r for r in result.rows if r.cik == "E000")
    # Five peers at 0.5 and six at 1.5: the spread is the honest half of the
    # answer and has to be carried, not just the median.
    assert row.turnover_iqr is not None and row.turnover_iqr > 0
    assert row.turnover_self == pytest.approx(0.5)


def test_the_spread_is_reported_next_to_the_median(con) -> None:
    """A median with no spread is a number that cannot be distrusted."""
    rows = dense(3674, 12, revenue=50_000_000, assets=100_000_000, prefix="G")
    build(con, rows)
    row = next(r for r in comps.screen(con).rows if r.cik == "G000")
    assert row.turnover_median is not None
    assert row.turnover_iqr == pytest.approx(0.0), (
        "an identical set should have zero spread, which is the control")


# --- determinism and the funnel ----------------------------------------


def test_the_same_input_gives_the_same_peer_sets(con) -> None:
    """Idempotent is not enough; deterministic is the rule. A peer set that
    changes between runs over the same partitions would change which companies
    are comparable, which is the `any_value` defect one level up."""
    rows = dense(3674, 12, revenue=50_000_000, assets=100_000_000, prefix="H")
    # Two filings for one filer, so the "latest observation" pick has something
    # to be non-deterministic about.
    rows += rows_for("H000", "H 0 older", 3674, revenue=1_000_000,
                     assets=2_000_000, period_end="2023-12-31", adsh="old")
    build(con, rows)
    first = comps.screen(con)
    second = comps.screen(con)
    assert [(r.cik, r.sic_depth, r.peers_banded, r.peers_material,
             r.turnover_median) for r in first.rows] == \
           [(r.cik, r.sic_depth, r.peers_banded, r.peers_material,
             r.turnover_median) for r in second.rows]
    # And the newest filing is the one used.
    row = next(r for r in first.rows if r.cik == "H000")
    assert float(row.assets) == 100_000_000.0


def test_the_funnel_says_what_each_stage_removed(con) -> None:
    rows = dense(3674, 12, revenue=50_000_000, assets=100_000_000, prefix="J")
    rows += rows_for("NS", "No Sic", None, revenue=5_000_000, assets=9_000_000)
    rows += rows_for("TI", "Tiny", 3674, revenue=1_000, assets=9_000_000)
    build(con, rows)
    result = comps.screen(con)
    names = [s.name for s in result.funnel.stages]
    assert names == ["operating filers", "placeable", "material",
                     "peer set found"]
    by = {s.name: s.remaining for s in result.funnel.stages}
    assert by["operating filers"] == 14
    assert by["placeable"] == 13          # the no-SIC filer is gone
    assert by["material"] == 12           # and the $1k-revenue one
    assert by["peer set found"] == 12
    assert all(s.why for s in result.funnel.stages), "a count with no reason"


def test_the_unchosen_floors_are_reported_beside_the_chosen_one(con) -> None:
    """The default has to be visibly a choice with a cost, the way a concept's
    coverage number sits beside the concept."""
    rows = dense(3674, 12, revenue=50_000_000, assets=100_000_000, prefix="R")
    build(con, rows)
    result = comps.screen(con)
    assert (comps.MATERIAL_REVENUE, comps.MIN_PEERS) in result.alternatives
    assert (50_000_000, 8) in result.alternatives
    assert (1_000_000, 12) in result.alternatives
    text = "\n".join(result.lines())
    assert "coverage at floors that were not chosen" in text


def test_financial_filers_never_enter_the_population(con) -> None:
    """Banks, insurers, brokers and REITs are a different table, not a SIC
    branch -- a bank's top line is interest income. The loader excludes them and
    this asserts the screen does not reintroduce them."""
    rows = dense(3674, 12, revenue=50_000_000, assets=100_000_000, prefix="W")
    rows += rows_for("BANK", "A Bank", 6022, revenue=50_000_000,
                     assets=100_000_000, sic_class="financial")
    build(con, rows)
    result = comps.screen(con)
    assert not any(r.cik == "BANK" for r in result.rows)
