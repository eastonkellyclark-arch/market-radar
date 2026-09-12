"""A ten-page deck, with the provenance of every number on the page it is on.

**A deck is the easiest place in this system for the discipline to get laundered.**
Every other surface carries its caveats structurally: a peer set prints the SIC
depth it settled for, a valuation prints its substitution list, an XBRL concept
prints its own coverage. A slide is a rectangle with a big number on it, and the
default behaviour of a slide is to look authoritative.

So the caveats are not an appendix here. :func:`provenance_footer` runs on *every*
page from a single code path, and a page built on a peer-set beta and a fallen-back
comp depth says so in the footer of that page. There is deliberately no way to
render a page without it -- :func:`add_page` is the only way to make a slide and it
calls the footer itself.

What it pulls, and from where:

    identity        companies + the point-in-time filer universe
    fundamentals    xbrl_fundamentals, with each concept's own coverage
    comps           screens/comps -- peer set, SIC depth, turnover spread
    valuation       screens/dcf -- enterprise value and its substitutions
    insiders        Form 4 clusters
    deals           the 8-K deal history for this CIK
    prices          our own Tiingo history, split-adjusted

Nothing here fetches. A deck is rendered from rows that already exist, so a page
that cannot be drawn says which input was missing rather than going blank -- the
same contract the dashboard panels have.

**No market cap anywhere.** The valuation page shows an enterprise value, and
comparing it to a market cap is a local hand-run step. A slide putting the two side
by side would be the most authoritative-looking wrong thing this project could
produce.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Final

log = logging.getLogger(__name__)

#: Ten pages, in the order a reader needs them: who, then what it reports, then
#: what it is worth and why that is uncertain, then what has happened around it.
PAGES: Final[tuple[tuple[str, str], ...]] = (
    ("cover", "Company, ticker, SIC, filing window"),
    ("identity", "What the filer universe knows, including whether it still files"),
    ("fundamentals", "The seven concepts, each with its own coverage"),
    ("cash_flow", "Operating cash flow, capex and free cash flow"),
    ("peers", "The peer set, the SIC depth it settled for, and its spread"),
    ("valuation", "Enterprise value, WACC, and every substitution behind it"),
    ("sensitivity", "What the answer does when the two constants move"),
    ("insiders", "Form 4 clusters -- officers, directors and 10% holders"),
    ("deals", "8-K deal history, by form type"),
    ("prices", "Split-adjusted price history and the unexplained-move count"),
)

#: Slide size. 13.333 x 7.5 inches is 16:9, which is what a screen is.
SLIDE_W: Final[float] = 13.333
SLIDE_H: Final[float] = 7.5

#: Ink. Muted rather than branded: this is a working document and a deck that
#: looks like a pitch invites being read like one.
INK: Final[str] = "1A1A1A"
MUTED: Final[str] = "6B6B6B"
WARN: Final[str] = "B45309"
GOOD: Final[str] = "15803D"

#: Every substitution, in the words a reader needs on the page rather than the
#: identifier a column needs. Shared with the dashboard panel's legend by
#: intention, not by import: the two surfaces must agree and the wording is short
#: enough that a shared constant would couple a slide renderer to an HTML one.
SUBSTITUTION_WORDS: Final[dict[str, str]] = {
    "erp_constant": "equity risk premium is a dated constant (no free source)",
    "growth_constant": "near-term growth is a flat 3%, not a forecast",
    "growth_mismatch": "this filer's own history is >10pts from that 3%",
    "peer_beta": "beta is the peer set's median, not this company's returns",
    "comp_depth_fallback": "that peer set is a widened industry",
    "absent_capex": "no capex line -- free cash flow is overstated",
    "no_beta": "no beta at all; a flat equity cost",
}


class DeckError(RuntimeError):
    """A deck could not be built."""


@dataclass(frozen=True, slots=True)
class Subject:
    """Everything one deck is rendered from. Assembled by the caller.

    A dataclass rather than a live connection because a deck must be reproducible:
    given the same Subject, the same file. A renderer that queried as it drew
    would make the output depend on when the slide was laid out.
    """

    cik: str
    company: str
    ticker: str | None = None
    sic: int | None = None
    first_period: date | None = None
    last_period: date | None = None
    still_filing: bool | None = None

    #: ``{concept: {"value":…, "status":…, "tag":…, "coverage":…}}``
    fundamentals: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: From screens/comps: sic_depth, peers_banded, peers_material, turnover.
    peers: dict[str, Any] = field(default_factory=dict)
    #: From screens/dcf: enterprise_value, wacc, terminal_share, substitutions.
    valuation: dict[str, Any] = field(default_factory=dict)
    insiders: list[dict[str, Any]] = field(default_factory=list)
    deals: list[dict[str, Any]] = field(default_factory=list)
    #: ``[(date, adjusted close)]`` -- already adjusted by the caller, because
    #: percent moves must always use adjusted prices and a renderer is the wrong
    #: place to be deciding that.
    prices: list[tuple[date, float]] = field(default_factory=list)
    unexplained_moves: int = 0

    @property
    def substitutions(self) -> list[str]:
        return list(self.valuation.get("substitutions") or [])

    @property
    def provenance(self) -> list[str]:
        """The lines every page carries. Built once, from the data.

        Derived rather than declared, for the same reason the dashboard's panel
        states are: a caveat written by hand in one place and contradicted by the
        data in another is the drift this codebase keeps finding.
        """
        out: list[str] = []
        depth = self.peers.get("sic_depth")
        if depth is not None and int(depth) < 4:
            out.append(f"peer set widened to {int(depth)}-digit SIC")
        banded = int(self.peers.get("peers_banded") or 0)
        material = int(self.peers.get("peers_material") or 0)
        if banded and material < banded:
            out.append(f"{material} of {banded} peers clear the revenue floor")
        for name in self.substitutions:
            out.append(SUBSTITUTION_WORDS.get(name, name))
        if self.unexplained_moves:
            out.append(f"{self.unexplained_moves} unexplained price moves on "
                       "record for this ticker")
        if self.still_filing is False:
            out.append("this filer has stopped filing -- no prices exist after "
                       "its delisting")
        return out

    @property
    def clean_but_constants_like(self) -> bool:
        """No substitution beyond the two that are on every valuation.

        Mirrors ``dcf.Valuation.clean_but_constants`` for a Subject, which carries
        the substitution list rather than the Valuation. Named separately rather
        than imported so a deck can be rendered from stored rows with no screens
        module in the process.
        """
        return set(self.substitutions) <= {"erp_constant", "growth_constant"}

    @property
    def weakest(self) -> str:
        """The single most important caveat, for the cover.

        One line, because a cover with eight caveats is a cover nobody reads. The
        order is by what most changes the number: no beta at all beats a peer
        beta, and an absent capex line beats either because it moves free cash
        flow rather than the discount rate.
        """
        for name in ("absent_capex", "no_beta", "growth_mismatch", "peer_beta",
                     "comp_depth_fallback"):
            if name in self.substitutions:
                return SUBSTITUTION_WORDS[name]
        if self.valuation.get("enterprise_value") is None:
            return "no valuation -- see the valuation page for which input failed"
        return "clean apart from two constants with no free source"


def _pptx():
    """Imported lazily with a refusal that says how to install it.

    The decks group is not in `dev` on purpose: python-pptx pulls lxml and pillow,
    and no data job needs them.
    """
    try:
        from pptx import Presentation
        from pptx.dml.color import RGBColor
        from pptx.util import Emu, Inches, Pt
    except ImportError as exc:   # pragma: no cover - exercised by the skip
        raise DeckError(
            "python-pptx is not installed. It is in the `decks` dependency "
            "group, which is deliberately not installed by default: "
            "`uv sync --group decks`."
        ) from exc
    return Presentation, RGBColor, Inches, Pt, Emu


def build(subject: Subject, dest: Path) -> Path:
    """Render one deck. Ten pages, every one carrying its provenance."""
    Presentation, RGBColor, Inches, Pt, _Emu = _pptx()
    prs = Presentation()
    prs.slide_width = Inches(SLIDE_W)
    prs.slide_height = Inches(SLIDE_H)
    ctx = _Ctx(prs=prs, RGBColor=RGBColor, Inches=Inches, Pt=Pt,
               subject=subject)

    for name, _what in PAGES:
        renderer = globals().get(f"_page_{name}")
        if renderer is None:      # pragma: no cover - PAGES is the source
            raise DeckError(f"no renderer for page {name!r}")
        renderer(ctx)

    dest.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(dest))
    log.info("wrote %s (%d pages)", dest, len(PAGES))
    return dest


@dataclass
class _Ctx:
    """The pptx handles plus the subject, so page functions take one argument."""

    prs: Any
    RGBColor: Any
    Inches: Any
    Pt: Any
    subject: Subject
    pages: int = 0


def add_page(ctx: _Ctx, title: str, subtitle: str = "") -> Any:
    """A blank slide with a title and **the provenance footer already on it**.

    The only way to make a slide in this module, which is the point: a page
    cannot be rendered without its caveats because there is no code path that
    produces one. A renderer that called `add_slide` directly could forget, and a
    forgotten footer is exactly how a deck becomes more confident than its inputs.
    """
    ctx.pages += 1
    slide = ctx.prs.slides.add_slide(ctx.prs.slide_layouts[6])   # blank
    _text(ctx, slide, title, 0.6, 0.4, SLIDE_W - 1.2, 0.6, size=26, bold=True)
    if subtitle:
        _text(ctx, slide, subtitle, 0.6, 1.0, SLIDE_W - 1.2, 0.4, size=12,
              colour=MUTED)
    provenance_footer(ctx, slide)
    return slide


def provenance_footer(ctx: _Ctx, slide: Any) -> None:
    """Every caveat that applies to this subject, at the foot of this page.

    Not an appendix and not a cover disclaimer. A reader looking at the valuation
    page has to see, on that page, that the beta came from peers -- because the
    page they screenshot is the page they send on.
    """
    lines = ctx.subject.provenance
    if not lines:
        _text(ctx, slide, "inputs: clean apart from two constants with no free "
                          "source (equity risk premium, near-term growth)",
              0.6, SLIDE_H - 0.75, SLIDE_W - 1.2, 0.5, size=9, colour=GOOD)
        return
    body = "inputs carry: " + "; ".join(lines)
    _text(ctx, slide, body, 0.6, SLIDE_H - 0.95, SLIDE_W - 1.2, 0.7, size=9,
          colour=WARN)


def _text(ctx: _Ctx, slide: Any, text: str, left: float, top: float,
          width: float, height: float, *, size: int = 12, bold: bool = False,
          colour: str = INK) -> Any:
    box = slide.shapes.add_textbox(ctx.Inches(left), ctx.Inches(top),
                                   ctx.Inches(width), ctx.Inches(height))
    frame = box.text_frame
    frame.word_wrap = True
    para = frame.paragraphs[0]
    run = para.add_run()
    run.text = text
    run.font.size = ctx.Pt(size)
    run.font.bold = bold
    run.font.color.rgb = ctx.RGBColor.from_string(colour)
    return box


def _rows(ctx: _Ctx, slide: Any, rows: list[tuple[str, ...]], *,
          top: float = 1.6, size: int = 11,
          widths: tuple[float, ...] = ()) -> None:
    """A plain aligned table. No pptx table object: a textbox grid is smaller,
    renders identically everywhere, and cannot inherit a theme."""
    if not rows:
        return
    cols = max(len(r) for r in rows)
    widths = widths or tuple([(SLIDE_W - 1.2) / cols] * cols)
    for i, row in enumerate(rows):
        left = 0.6
        for j in range(cols):
            cell = row[j] if j < len(row) else ""
            _text(ctx, slide, str(cell), left, top + i * 0.32, widths[j], 0.3,
                  size=size, bold=(i == 0),
                  colour=MUTED if i == 0 else INK)
            left += widths[j]


def _money(value: Any) -> str:
    if value in (None, ""):
        return "--"
    try:
        n = float(value)
    except (TypeError, ValueError):
        return str(value)
    for cut, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= cut:
            return f"${n / cut:,.2f}{suffix}"
    return f"${n:,.0f}"


# --- the ten pages ------------------------------------------------------


def _page_cover(ctx: _Ctx) -> None:
    s = ctx.subject
    slide = add_page(ctx, s.company, f"CIK {s.cik}"
                     + (f"  ·  {s.ticker}" if s.ticker else "")
                     + (f"  ·  SIC {s.sic}" if s.sic else ""))
    ev = s.valuation.get("enterprise_value")
    _text(ctx, slide, _money(ev) if ev else "no valuation",
          0.6, 2.4, 6.0, 1.2, size=44, bold=True)
    _text(ctx, slide, "enterprise value, discounted free cash flow",
          0.6, 3.6, 6.0, 0.4, size=11, colour=MUTED)
    # The cover carries the single worst caveat, in words, next to the number.
    # A cover that shows only the number is the page that gets screenshotted.
    _text(ctx, slide, f"weakest input: {s.weakest}", 0.6, 4.2, SLIDE_W - 1.2,
          0.5, size=13, colour=WARN if s.substitutions else GOOD)
    _text(ctx, slide,
          "Not a market valuation. This is an enterprise value from reported "
          "cash flows; no market capitalisation appears anywhere in this deck.",
          0.6, 5.0, SLIDE_W - 1.2, 0.6, size=10, colour=MUTED)


def _page_identity(ctx: _Ctx) -> None:
    s = ctx.subject
    slide = add_page(ctx, "Identity", "From the point-in-time filer universe, "
                                     "keyed on CIK")
    status = ("still filing" if s.still_filing
              else "stopped filing" if s.still_filing is False else "unknown")
    _rows(ctx, slide, [
        ("field", "value", "why it is this and not something else"),
        ("CIK", s.cik, "the identifier SEC assigns and never reuses"),
        ("company", s.company, "the name on its most recent filing"),
        ("ticker", s.ticker or "--",
         "current listing only; a recycled symbol is a different company"),
        ("SIC", str(s.sic or "--"), "self-reported on the filing"),
        ("filing window",
         f"{s.first_period or '?'} to {s.last_period or '?'}",
         "from the sub tables of every loaded quarter"),
        ("status", status,
         "stopped filing is what an acquisition looks like from here"),
    ], widths=(2.2, 3.4, 6.5))


def _page_fundamentals(ctx: _Ctx) -> None:
    s = ctx.subject
    slide = add_page(ctx, "Fundamentals",
                     "Each concept carries its own coverage. They are never "
                     "averaged: seven concepts individually clear 98% and all "
                     "seven on one filer is 58.7%.")
    rows: list[tuple[str, ...]] = [("concept", "value", "tag", "status",
                                   "coverage")]
    for name in ("revenue", "net_income", "assets", "liabilities", "equity",
                 "capex", "operating_cash_flow"):
        got = s.fundamentals.get(name) or {}
        cov = got.get("coverage")
        rows.append((
            name,
            _money(got.get("value")) if got.get("status") == "stated" else "--",
            str(got.get("tag") or "--")[:38],
            str(got.get("status") or "not read"),
            f"{float(cov) * 100:.1f}%" if cov is not None else "--",
        ))
    _rows(ctx, slide, rows, widths=(2.4, 1.8, 4.4, 2.0, 1.4))


def _page_cash_flow(ctx: _Ctx) -> None:
    s = ctx.subject
    slide = add_page(ctx, "Free cash flow", "Operating cash flow minus capex")
    ocf = (s.fundamentals.get("operating_cash_flow") or {}).get("value")
    capex = (s.fundamentals.get("capex") or {})
    absent = capex.get("status") != "stated"
    fcf = None
    if ocf is not None:
        fcf = float(ocf) - (0.0 if absent else abs(float(capex["value"])))
    _rows(ctx, slide, [
        ("line", "value", "note"),
        ("operating cash flow", _money(ocf), "as filed, 98.6% of filers"),
        ("capex", "--" if absent else _money(capex.get("value")),
         "no capex line on this filing" if absent
         else "as filed, 87.8% of filers"),
        ("free cash flow", _money(fcf),
         "operating cash flow undiminished -- OVERSTATED by any capex folded "
         "into an aggregated investing total" if absent
         else "operating cash flow minus capex"),
    ], widths=(3.0, 2.4, 6.7))
    if absent:
        _text(ctx, slide,
              "Absent capex is not zero capex. 22.7% of operating filers "
              "present no capex line, and `unmapped` is 0% -- the line is "
              "genuinely not there. This free cash flow is an upper bound.",
              0.6, 3.4, SLIDE_W - 1.2, 0.8, size=12, colour=WARN)


def _page_peers(ctx: _Ctx) -> None:
    s = ctx.subject
    p = s.peers
    depth = p.get("sic_depth")
    slide = add_page(ctx, "Peer set",
                     "Same industry, same size. The depth is the answer, not a "
                     "setting.")
    _rows(ctx, slide, [
        ("field", "value", "note"),
        ("SIC depth", f"{depth}-digit" if depth else "no peer set",
         "4-digit was asked for; 3 and 2 are fallbacks"),
        ("peers in band", str(p.get("peers_banded") or "--"),
         "same SIC group, within 3x on assets"),
        ("peers above the floor", str(p.get("peers_material") or "--"),
         "clearing $1M revenue -- a correctness floor, not a similarity one"),
        ("median asset turnover",
         f"{float(p['turnover_median']):.2f}" if p.get("turnover_median")
         else "--", "revenue over assets, across the peer set"),
        ("spread (IQR)",
         f"{float(p['turnover_iqr']):.2f}" if p.get("turnover_iqr") else "--",
         "how alike the set it just averaged is"),
    ], widths=(3.2, 2.2, 6.7))
    _text(ctx, slide,
          "Measured: the extra SIC digit buys nothing on similarity -- "
          "within-set turnover spread is 0.43 at 4-digit and 0.49 at 2-digit on "
          "the filers that clear eight peers at every depth. What tightens a set "
          "is the size band, not the industry code.",
          0.6, 3.6, SLIDE_W - 1.2, 0.8, size=11, colour=MUTED)


def _page_valuation(ctx: _Ctx) -> None:
    s = ctx.subject
    v = s.valuation
    slide = add_page(ctx, "Valuation",
                     "Discounted free cash flow, perpetuity terminal value")
    _rows(ctx, slide, [
        ("field", "value", "note"),
        ("enterprise value", _money(v.get("enterprise_value")), ""),
        ("WACC", f"{float(v['wacc']) * 100:.1f}%" if v.get("wacc") else "--",
         "CAPM plus an after-tax cost of debt; book capital structure"),
        ("beta", f"{float(v['beta']):.2f}" if v.get("beta") is not None
         else "none", "weekly returns against SPY, split-adjusted"),
        ("terminal share",
         f"{float(v['terminal_share']) * 100:.0f}%"
         if v.get("terminal_share") else "--",
         "how much of the answer rests on one growth number"),
        ("substitutions", str(len(s.substitutions)),
         "listed below; none of them is zero"),
    ], widths=(3.0, 2.2, 6.9))
    rows: list[tuple[str, ...]] = [("substitution", "what it does to this number")]
    for name in s.substitutions:
        rows.append((name, SUBSTITUTION_WORDS.get(name, name)))
    if len(rows) > 1:
        _rows(ctx, slide, rows, top=3.7, size=10, widths=(3.4, 8.5))


def _page_sensitivity(ctx: _Ctx) -> None:
    """The stored flex, rendered. **This page does not compute.**

    It used to call ``dcf.enterprise_value`` itself, which made it the only page in
    the deck that derived a number rather than rendering one -- and so the only one
    that could disagree with the dashboard about the same filer. A deck is the
    artifact that leaves the room, so the four points are computed once beside the
    valuation, stored on the row, and read here.
    """
    s = ctx.subject
    v = s.valuation
    slide = add_page(ctx, "Sensitivity",
                     "What the answer does when the growth constant moves. It is "
                     "an assumption, so this is the honest range.")
    ev = v.get("enterprise_value")
    flex = v.get("flex") or {}
    if ev is None:
        _text(ctx, slide, "No valuation, so nothing to flex.", 0.6, 2.0,
              8.0, 0.5, size=14, colour=MUTED)
        return
    if not flex:
        # Said rather than drawn empty: a missing flex means the row was written
        # by an older screen, which is a fact about the row and not about the
        # company.
        _text(ctx, slide,
              "This valuation carries no stored sensitivity. Re-run `mr dcf` -- "
              "the deck renders the flex rather than deriving it, so that this "
              "page and the dashboard cannot disagree.",
              0.6, 2.0, SLIDE_W - 1.2, 0.8, size=13, colour=WARN)
        return
    base = float(ev)
    base_rate = v.get("growth")
    rows: list[tuple[str, ...]] = [("near-term growth", "enterprise value",
                                   "vs the base case")]
    for key in sorted(flex, key=float):
        growth, got = float(key), float(flex[key])
        is_base = base_rate is not None and abs(growth - float(base_rate)) < 1e-9
        rows.append((f"{growth * 100:.0f}%", _money(got),
                     "base case" if is_base else f"{got / base:.2f}x"))
    _rows(ctx, slide, rows, widths=(3.0, 3.0, 6.1))
    _text(ctx, slide,
          "A growth rate fitted from this company's own history loses to the "
          "flat 3% out of sample -- past growth does not predict future growth, "
          "with a correlation of -0.035 to +0.079 across 30 quarters. So the "
          "constant stays and this page shows what it costs.",
          0.6, 4.2, SLIDE_W - 1.2, 0.8, size=11, colour=MUTED)


def _page_insiders(ctx: _Ctx) -> None:
    s = ctx.subject
    slide = add_page(ctx, "Insider clusters",
                     "Form 4 buys, clustered. A cluster is the signal; a single "
                     "filing is not.")
    if not s.insiders:
        _text(ctx, slide, "No Form 4 clusters on record for this issuer.",
              0.6, 2.0, 8.0, 0.5, size=14, colour=MUTED)
        return
    rows: list[tuple[str, ...]] = [("role", "buyers", "value", "window",
                                   "note")]
    for row in s.insiders[:10]:
        rows.append((
            str(row.get("role") or ""), str(row.get("n_buyers") or ""),
            _money(row.get("value")),
            f"{row.get('first') or '?'} to {row.get('last') or '?'}",
            "fund-like filer" if row.get("fund_like") else "",
        ))
    _rows(ctx, slide, rows, widths=(2.4, 1.4, 2.0, 3.4, 2.9))


def _page_deals(ctx: _Ctx) -> None:
    s = ctx.subject
    slide = add_page(ctx, "Deal history",
                     "By SEC form type, not by news. Filings are legally "
                     "required, timestamped and unambiguous.")
    if not s.deals:
        _text(ctx, slide, "No deal filings on record for this CIK.",
              0.6, 2.0, 8.0, 0.5, size=14, colour=MUTED)
        return
    rows: list[tuple[str, ...]] = [("filed", "form", "type", "value", "role")]
    for row in s.deals[:10]:
        rows.append((
            str(row.get("filed") or row.get("filed_date") or ""),
            str(row.get("form") or row.get("items") or ""),
            str(row.get("deal_type") or ""),
            _money(row.get("value_usd")),
            str(row.get("filer_role") or ""),
        ))
    _rows(ctx, slide, rows, widths=(2.0, 2.2, 2.6, 2.4, 2.9))


def _page_prices(ctx: _Ctx) -> None:
    s = ctx.subject
    slide = add_page(ctx, "Price history",
                     "Split-adjusted. Percent moves on unadjusted prices read a "
                     "reverse split as a 95% fall.")
    if not s.prices:
        _text(ctx, slide,
              "No price history. A delisted company has none -- which is what an "
              "acquisition looks like from inside a survivor-only universe.",
              0.6, 2.0, SLIDE_W - 1.2, 0.8, size=13, colour=WARN)
        return
    lo = min(p for _d, p in s.prices)
    hi = max(p for _d, p in s.prices)
    first, last = s.prices[0], s.prices[-1]
    _rows(ctx, slide, [
        ("field", "value"),
        ("window", f"{first[0]} to {last[0]}"),
        ("sessions", str(len(s.prices))),
        ("range", f"{lo:,.2f} to {hi:,.2f}"),
        ("last", f"{last[1]:,.2f}"),
        ("unexplained moves", str(s.unexplained_moves)),
    ], widths=(3.0, 9.1))
    if s.unexplained_moves:
        _text(ctx, slide,
              f"{s.unexplained_moves} large single-session moves have no "
              "corporate action on record to explain them. Tiingo's per-bar "
              "split factor is itself incomplete, worst on exactly the small "
              "tickers where reverse splits are constant -- so the action table "
              "is not assumed complete.",
              0.6, 4.0, SLIDE_W - 1.2, 0.8, size=11, colour=WARN)
