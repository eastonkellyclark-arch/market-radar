"""Decks: the provenance is on the page, and there is no way to render one without.

**A deck is the easiest place in this system for the discipline to get laundered.**
Every other surface carries its caveats structurally -- a peer set prints its SIC
depth, a valuation prints its substitution list, an XBRL concept prints its own
coverage. A slide is a rectangle with a big number on it, and the default behaviour
of a slide is to look authoritative.

So these tests are mostly about what cannot be omitted rather than about layout.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest

from marketradar import decks

pptx = pytest.importorskip(
    "pptx",
    reason=("python-pptx is in the `decks` dependency group, which is "
            "deliberately not installed by default -- it pulls lxml and pillow "
            "and no data job needs them. `uv sync --group decks` to run these."),
)

REPO = Path(__file__).resolve().parents[1]


def subject(**over) -> decks.Subject:
    base = dict(
        cik="0000104169", company="WALMART INC.", ticker="WMT", sic=5331,
        first_period=date(2019, 1, 31), last_period=date(2026, 1, 31),
        still_filing=True,
        fundamentals={
            "revenue": {"value": 6.8e11, "status": "stated",
                        "tag": "RevenueFromContractWithCustomerExcludingAssessedTax",
                        "coverage": 0.878},
            "operating_cash_flow": {"value": 3.6e10, "status": "stated",
                                    "tag": "NetCashProvidedByUsedInOperatingActivities",
                                    "coverage": 0.996},
            "capex": {"value": 2.3e10, "status": "stated",
                      "tag": "PaymentsToAcquirePropertyPlantAndEquipment",
                      "coverage": 0.878},
        },
        peers={"sic_depth": 4, "peers_banded": 14, "peers_material": 14,
               "turnover_median": 2.41, "turnover_iqr": 0.62},
        valuation={"enterprise_value": 3.53e11, "wacc": 0.070,
                   "terminal_share": 0.66, "beta": 0.62,
                   "free_cash_flow": 1.3e10,
                   "substitutions": ["erp_constant", "growth_constant"]},
        insiders=[], deals=[], prices=[(date(2026, 9, 10), 104.5)],
    )
    base.update(over)
    return decks.Subject(**base)


def slide_text(path: Path) -> list[str]:
    """All the text on each slide, one joined string per slide."""
    from pptx import Presentation

    prs = Presentation(str(path))
    out = []
    for slide in prs.slides:
        parts = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                parts.append(shape.text_frame.text)
        out.append("\n".join(parts))
    return out


# --- the footer cannot be omitted ---------------------------------------


def test_every_page_carries_the_provenance_footer(tmp_path) -> None:
    """**The structural requirement.** Not an appendix, not a cover disclaimer:
    the page a reader screenshots is the page they send on, so the caveats are on
    every one of them.
    """
    heavy = subject(valuation={
        "enterprise_value": 1.2e9, "wacc": 0.11, "terminal_share": 0.45,
        "beta": 1.3, "free_cash_flow": 9e7,
        "substitutions": ["peer_beta", "comp_depth_fallback", "absent_capex",
                          "erp_constant", "growth_constant",
                          "growth_mismatch"]},
        peers={"sic_depth": 2, "peers_banded": 24, "peers_material": 11,
               "turnover_median": 1.1, "turnover_iqr": 2.8})
    dest = decks.build(heavy, tmp_path / "d.pptx")
    pages = slide_text(dest)
    assert len(pages) == len(decks.PAGES) == 10
    for i, text in enumerate(pages):
        assert "inputs carry:" in text, (
            f"page {i + 1} ({decks.PAGES[i][0]}) has no provenance footer")


def test_the_footer_names_every_substitution(tmp_path) -> None:
    heavy = subject(valuation={
        "enterprise_value": 1.2e9, "wacc": 0.11, "terminal_share": 0.45,
        "beta": 1.3, "free_cash_flow": 9e7,
        "substitutions": ["peer_beta", "absent_capex", "erp_constant",
                          "growth_constant"]})
    pages = slide_text(decks.build(heavy, tmp_path / "d.pptx"))
    footer = pages[0]
    assert "beta is the peer set's median" in footer
    assert "no capex line" in footer
    assert "equity risk premium is a dated constant" in footer
    assert "flat 3%" in footer


def test_add_page_is_the_only_way_to_make_a_slide() -> None:
    """A renderer calling `add_slide` directly could forget the footer, and a
    forgotten footer is exactly how a deck becomes more confident than its inputs.
    So every page function goes through `add_page`.
    """
    source = (REPO / "src" / "marketradar" / "decks.py").read_text(
        encoding="utf-8")
    # One call site, inside add_page itself.
    assert source.count("slides.add_slide(") == 1
    body = source.split("def add_page(", 1)[1]
    assert "slides.add_slide(" in body.split("def ", 1)[0], (
        "add_slide moved out of add_page; a page could now skip the footer")
    for name, _what in decks.PAGES:
        fn = source.split(f"def _page_{name}(", 1)[1].split("\ndef ", 1)[0]
        assert "add_page(" in fn, f"_page_{name} does not go through add_page"


def test_a_clean_subject_still_says_what_it_rests_on(tmp_path) -> None:
    """No valuation has zero substitutions, so "clean" means clean apart from two
    constants -- and every page names which two rather than saying nothing.

    Note what this pins: the footer's no-substitutions branch is unreachable for a
    real valuation, because the equity risk premium and the growth rate are on
    every one. A clean subject takes the ordinary branch and lists them, which is
    the honest rendering -- "clean" must not print as an absence of caveats.
    """
    clean = subject()
    assert clean.clean_but_constants_like, "fixture is not the clean case"
    pages = slide_text(decks.build(clean, tmp_path / "d.pptx"))
    for i, text in enumerate(pages):
        assert "inputs carry:" in text, f"page {i + 1} lost its footer"
        assert "equity risk premium is a dated constant" in text
        assert "flat 3%" in text
    # And the cover says it positively rather than leaving the reader to infer it.
    assert "clean apart from two constants" in pages[0]


# --- the worst case, rendered ------------------------------------------


def test_the_cover_leads_with_the_worst_input_not_the_number(tmp_path) -> None:
    """A cover showing only the number is the page that gets screenshotted. The
    order is by what most changes the answer: an absent capex line beats a peer
    beta because it moves free cash flow rather than the discount rate."""
    heavy = subject(valuation={
        "enterprise_value": 1.2e9, "wacc": 0.11, "terminal_share": 0.45,
        "beta": 1.3, "free_cash_flow": 9e7,
        "substitutions": ["peer_beta", "comp_depth_fallback", "absent_capex",
                          "erp_constant", "growth_constant",
                          "growth_mismatch"]})
    cover = slide_text(decks.build(heavy, tmp_path / "d.pptx"))[0]
    assert "weakest input: no capex line" in cover
    assert heavy.weakest == decks.SUBSTITUTION_WORDS["absent_capex"]

    # Without the capex problem, the next worst is the missing beta.
    no_beta = subject(valuation={
        "enterprise_value": 1.2e9, "wacc": 0.095, "terminal_share": 0.5,
        "beta": None, "free_cash_flow": 9e7,
        "substitutions": ["no_beta", "erp_constant", "growth_constant"]})
    assert no_beta.weakest == decks.SUBSTITUTION_WORDS["no_beta"]


def test_absent_capex_is_called_an_upper_bound_on_its_own_page(tmp_path) -> None:
    """22.7% of operating filers present no capex line and `unmapped` is 0% -- the
    line is genuinely not there. The page must not print a free cash flow that
    reads as measured."""
    absent = subject(fundamentals={
        "operating_cash_flow": {"value": 3.6e10, "status": "stated",
                                "tag": "NetCashProvidedByUsedInOperatingActivities",
                                "coverage": 0.996},
        "capex": {"value": None, "status": "absent", "tag": None,
                  "coverage": 0.878}},
        valuation={"enterprise_value": 4e11, "wacc": 0.07,
                   "terminal_share": 0.66, "beta": 0.62,
                   "free_cash_flow": 3.6e10,
                   "substitutions": ["absent_capex", "erp_constant",
                                     "growth_constant"]})
    pages = slide_text(decks.build(absent, tmp_path / "d.pptx"))
    cash = next(t for t in pages if "Free cash flow" in t)
    assert "upper bound" in cash
    assert "not zero capex" in cash
    assert "OVERSTATED" in cash


def test_a_widened_peer_set_says_so_on_the_peer_page(tmp_path) -> None:
    wide = subject(peers={"sic_depth": 2, "peers_banded": 24,
                          "peers_material": 11, "turnover_median": 1.1,
                          "turnover_iqr": 2.8})
    pages = slide_text(decks.build(wide, tmp_path / "d.pptx"))
    peers = next(t for t in pages if "Peer set" in t)
    assert "2-digit" in peers
    assert "fallbacks" in peers
    # And the spread is beside the median, so the median can be distrusted.
    assert "2.80" in peers or "2.8" in peers


def test_no_prices_is_explained_rather_than_blank(tmp_path) -> None:
    """A delisted company has no price history, which is what an acquisition looks
    like from inside a survivor-only universe."""
    gone = subject(prices=[], still_filing=False)
    pages = slide_text(decks.build(gone, tmp_path / "d.pptx"))
    prices = next(t for t in pages if "Price history" in t)
    assert "delisted" in prices or "survivor-only" in prices
    assert "stopped filing" in pages[0]


# --- what must never be on a slide -------------------------------------


def test_no_market_capitalisation_anywhere(tmp_path) -> None:
    """**The most authoritative-looking wrong thing this project could produce.**

    The deck shows an enterprise value from reported cash flows. Putting a market
    cap beside it would invite the comparison that the 20-name check showed is
    meaningless for growth companies -- Amazon at 0.03x -- and a slide is the
    worst place to discover that.
    """
    pages = slide_text(decks.build(subject(), tmp_path / "d.pptx"))
    joined = "\n".join(pages).lower()
    assert "market cap" not in joined or "no market capitalisation" in joined
    assert "enterprise value" in joined
    source = (REPO / "src" / "marketradar" / "decks.py").read_text(
        encoding="utf-8")
    assert "market_cap" not in source, (
        "the deck renderer gained a market-cap field")


def test_the_deck_module_is_unreachable_from_a_workflow() -> None:
    """Same rule as yfinance: local-only, and enforced rather than remembered.

    python-pptx pulls lxml and pillow, the `decks` group is not installed by
    default, and nothing a GitHub Action runs may import it -- otherwise the
    nightly jobs acquire a dependency nobody decided to add.
    """
    workflows = sorted((REPO / ".github" / "workflows").glob("*.yml"))
    assert workflows, "no workflows found; this check would pass vacuously"
    commands: list[str] = []
    for path in workflows:
        text = path.read_text(encoding="utf-8")
        commands.extend(re.findall(r"uv run mr ([a-z0-9-]+)", text))
    assert commands, "no `uv run mr` commands found; check the regex"
    assert "decks" not in commands, (
        f"a workflow runs `mr decks`: {sorted(set(commands))}")

    # `tests.yml` is the one workflow allowed to install the group, and it must:
    # 15 tests in this file are the only thing standing between a deck and a slide
    # with no provenance on it, and a guard that silently skips on the runner is
    # not a guard. Same resolution as setup-node for the DOM suite.
    #
    # Every *data* workflow must not, because that is where the dependency would
    # become a runtime cost nobody decided to pay.
    for path in workflows:
        text = path.read_text(encoding="utf-8")
        if path.name == "tests.yml":
            assert "--group decks" in text, (
                "tests.yml does not install the decks group, so every test in "
                "this file skips in CI")
            continue
        assert "--group decks" not in text, (
            f"{path.name} is a data workflow and installs the decks group")


def test_no_src_module_outside_decks_imports_pptx() -> None:
    """A stray import would pull lxml and pillow into every job that touches the
    package, which is the dependency decision being made by accident."""
    offenders = []
    for path in (REPO / "src").rglob("*.py"):
        if path.name == "decks.py":
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"^\s*(import pptx|from pptx)", text, re.MULTILINE):
            offenders.append(str(path.relative_to(REPO)))
    assert not offenders, f"pptx imported outside decks.py: {offenders}"


def test_pptx_is_imported_lazily_with_a_refusal_that_helps() -> None:
    """Importing the module must not require the group: the dashboard and the CLI
    import marketradar broadly, and a hard import would make python-pptx a core
    dependency by the back door."""
    source = (REPO / "src" / "marketradar" / "decks.py").read_text(
        encoding="utf-8")
    head = source.split("def _pptx(", 1)[0]
    assert "from pptx" not in head and "import pptx" not in head
    assert "uv sync --group decks" in source


# --- the pages themselves ----------------------------------------------


def test_ten_pages_in_a_declared_order() -> None:
    assert len(decks.PAGES) == 10
    assert [n for n, _ in decks.PAGES][:2] == ["cover", "identity"]
    assert all(what for _n, what in decks.PAGES), "a page with no stated purpose"


def test_every_concept_shows_its_own_coverage(tmp_path) -> None:
    """Seven concepts individually clear 98% and all seven on one filer is 58.7%,
    so they are never averaged and each carries its own number."""
    pages = slide_text(decks.build(subject(), tmp_path / "d.pptx"))
    fund = next(t for t in pages if "Fundamentals" in t)
    assert "99.6%" in fund          # operating cash flow
    assert "87.8%" in fund          # capex and revenue
    assert "never averaged" in fund


def test_the_sensitivity_page_shows_what_the_constant_costs(tmp_path) -> None:
    pages = slide_text(decks.build(subject(), tmp_path / "d.pptx"))
    sens = next(t for t in pages if "Sensitivity" in t)
    assert "base case" in sens
    assert "does not predict future growth" in sens
