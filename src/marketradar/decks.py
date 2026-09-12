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

# --- the design system -------------------------------------------------
#
# **One scale, one grid, one palette, and every page built from them.** The first
# version positioned each textbox by hand and chose a font size per call, which is
# why nothing lined up between pages: there was no shared answer to "where does a
# table start" or "how big is a label". These constants are that answer.
#
# Written without the pptx skill, which is not present in this environment, so the
# rendering constraints here are the ones python-pptx imposes that I know of rather
# than the ones a skill would have listed: no reliable text autofit (boxes are sized
# generously and the text is kept short), shapes arrive with a default fill and
# outline that must both be cleared, and numeric alignment comes from paragraph
# alignment rather than from trusting a font's tabular figures.

#: Page margin. Everything lives inside it, including the footnote band.
MARGIN: Final[float] = 0.65
#: Twelve columns and a gutter, so a table's column edges are a choice rather than
#: an arithmetic accident. `col()` and `span()` are the only way to get an x.
COLUMNS: Final[int] = 12
GUTTER: Final[float] = 0.12

#: Type scale. Named sizes, because "size=13" at one call site and "size=12" at the
#: next is how a deck ends up with four heading sizes nobody chose.
DISPLAY: Final[int] = 40
COVER_NAME: Final[int] = 30
H1: Final[int] = 22
#: A callout. The one step between a heading and body text, for the sentence on a
#: page that has to be read before the table under it.
LEAD: Final[int] = 14
H2: Final[int] = 12
BODY: Final[int] = 10
SMALL: Final[int] = 9
MICRO: Final[int] = 8

#: Two faces. A serif for the display figure and the page titles, a humanist sans
#: for everything that has to be read in a row. Both ship with Office on Windows and
#: macOS, which is the whole test -- a deck is the artifact that leaves the room, and
#: a missing font is resolved by the reader's machine, not ours.
FONT_DISPLAY: Final[str] = "Georgia"
FONT_BODY: Final[str] = "Calibri"

#: Ink. Muted rather than branded: this is a working document and a deck that
#: looks like a pitch invites being read like one. The accent is a single restrained
#: navy used for rules and the cover band, never for data.
INK: Final[str] = "14161A"
INK_2: Final[str] = "3F4450"
MUTED: Final[str] = "767B86"
RULE: Final[str] = "DDE0E4"
PANEL: Final[str] = "F5F6F7"
ACCENT: Final[str] = "1F3A5F"
WARN: Final[str] = "B45309"
GOOD: Final[str] = "15803D"

#: Candle direction, **the same two hues the dashboard uses**. Shared by value so a
#: deck and the panel cannot show the same month in different colours.
CANDLE_UP: Final[str] = "1D7A4C"
CANDLE_DOWN: Final[str] = "B1402F"

#: Where the footnote band starts. Every page's content has to end above this.
BAND_TOP: Final[float] = 6.35
#: First baseline below the page title, for page bodies.
CONTENT_TOP: Final[float] = 1.72


def content_width() -> float:
    return SLIDE_W - 2 * MARGIN


def col(n: int) -> float:
    """Left edge of column ``n`` (0-based), in inches."""
    unit = (content_width() - GUTTER * (COLUMNS - 1)) / COLUMNS
    return MARGIN + n * (unit + GUTTER)


def span(n: int) -> float:
    """Width of ``n`` columns including the gutters between them."""
    unit = (content_width() - GUTTER * (COLUMNS - 1)) / COLUMNS
    return n * unit + GUTTER * (n - 1)

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
    #: ``[(date, open, high, low, close, volume)]`` -- OHLCV, because the chart
    #: draws candles and a close alone discards three quarters of every bar.
    #:
    #: **Raw, not back-adjusted**, which matches what the dashboard draws and is
    #: therefore the only basis on which the two can agree. It also means a split
    #: reads as a step on both, and the page says so. Cumulative back-adjustment is
    #: owed work; a renderer is the wrong place to invent it, and inferring a ratio
    #: from a price jump is the fabrication `corporate_actions` refuses outright.
    prices: list[tuple[date, float, float, float, float, int]] = field(
        default_factory=list)
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
        from pptx.enum.shapes import MSO_SHAPE
        from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
        from pptx.util import Emu, Inches, Pt
    except ImportError as exc:   # pragma: no cover - exercised by the skip
        raise DeckError(
            "python-pptx is not installed. It is in the `decks` dependency "
            "group, which is deliberately not installed by default: "
            "`uv sync --group decks`."
        ) from exc
    return (Presentation, RGBColor, Inches, Pt, Emu, MSO_SHAPE, PP_ALIGN,
            MSO_ANCHOR)


def build(subject: Subject, dest: Path) -> Path:
    """Render one deck. Ten pages, every one carrying its provenance."""
    (Presentation, RGBColor, Inches, Pt, _Emu, MSO_SHAPE, PP_ALIGN,
     MSO_ANCHOR) = _pptx()
    prs = Presentation()
    prs.slide_width = Inches(SLIDE_W)
    prs.slide_height = Inches(SLIDE_H)
    ctx = _Ctx(prs=prs, RGBColor=RGBColor, Inches=Inches, Pt=Pt,
               subject=subject, shape=MSO_SHAPE, align=PP_ALIGN,
               anchor=MSO_ANCHOR)

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
    shape: Any = None
    align: Any = None
    anchor: Any = None
    pages: int = 0


def add_page(ctx: _Ctx, title: str, subtitle: str = "", *,
             chrome: bool = True) -> Any:
    """A page with its title, its rule, and **the footnote band already on it**.

    The only way to make a slide in this module, which is the point: a page cannot
    be rendered without its caveats because there is no code path that produces one.
    A renderer that called `add_slide` directly could forget, and a forgotten band is
    exactly how a deck becomes more confident than its inputs.
    """
    ctx.pages += 1
    slide = ctx.prs.slides.add_slide(ctx.prs.slide_layouts[6])   # blank
    if not chrome:
        # The cover lays out its own two halves, so it takes the slide without the
        # title bar -- but it still comes through here, because this is the only
        # place `add_slide` is called and therefore the only place the band cannot
        # be skipped. A cover that made its own slide would be a page that could
        # forget its provenance, which is the whole reason this gate exists.
        footnote_band(ctx, slide)
        return slide
    _text(ctx, slide, title, MARGIN, 0.45, span(9), 0.52,
          size=H1, font=FONT_DISPLAY, colour=INK)
    if subtitle:
        _text(ctx, slide, subtitle, MARGIN, 1.02, span(9), 0.46,
              size=H2, colour=MUTED)
    # The rule under the title is the grid made visible: every page's content
    # starts at the same line, which is what makes ten pages read as one document.
    _rule(ctx, slide, CONTENT_TOP - 0.16, colour=RULE)
    _page_number(ctx, slide)
    footnote_band(ctx, slide)
    return slide


def _page_number(ctx: _Ctx, slide: Any) -> None:
    _text(ctx, slide, f"{ctx.pages:02d}", SLIDE_W - MARGIN - 0.6, 0.5, 0.6, 0.3,
          size=SMALL, colour=MUTED, align="right")


def _rect(ctx: _Ctx, slide: Any, left: float, top: float, width: float,
          height: float, *, fill: str, line: str | None = None) -> Any:
    """A filled rectangle with its default outline removed.

    The removal is not optional: a shape arrives from python-pptx with both a theme
    fill and a theme outline, so a "hairline rule" drawn without clearing the line
    comes out as a 1pt box in the template's accent colour.
    """
    box = slide.shapes.add_shape(
        ctx.shape.RECTANGLE, ctx.Inches(left), ctx.Inches(top),
        ctx.Inches(max(width, 0.004)), ctx.Inches(max(height, 0.004)))
    box.fill.solid()
    box.fill.fore_color.rgb = ctx.RGBColor.from_string(fill)
    if line is None:
        box.line.fill.background()
    else:
        box.line.color.rgb = ctx.RGBColor.from_string(line)
        box.line.width = ctx.Pt(0.5)
    box.shadow.inherit = False
    if box.has_text_frame:
        box.text_frame.text = ""
    return box


def _rule(ctx: _Ctx, slide: Any, top: float, *, colour: str = RULE,
          left: float | None = None, width: float | None = None,
          weight: float = 0.01) -> None:
    """A hairline. A thin rectangle rather than a connector: a connector's width is
    a line weight in points and does not scale with the slide, so a 0.5pt rule looks
    different on a 13.3in slide than the 0.01in one asked for here."""
    _rect(ctx, slide, MARGIN if left is None else left, top,
          content_width() if width is None else width, weight, fill=colour)


def footnote_band(ctx: _Ctx, slide: Any) -> None:
    """Every caveat that applies to this subject, in a designed band at the foot.

    **Not an appendix and not a cover disclaimer.** A reader looking at the valuation
    page has to see, on that page, that the beta came from peers -- because the page
    they screenshot is the page they send on.

    A band rather than a loose textbox, and that is the change this rebuild was
    allowed to make: the caveats used to sit in whatever space was left above the
    bottom edge, which read as bolted on and invited being cropped. Now they have a
    panel, a top rule and a standing label, so the page is *designed around* them --
    and a page with nothing to declare still carries the band, saying so. The band's
    presence is therefore not evidence of a problem, which is what stops a reader
    learning to skip it.
    """
    lines = ctx.subject.provenance
    _rect(ctx, slide, 0, BAND_TOP, SLIDE_W, SLIDE_H - BAND_TOP, fill=PANEL)
    _rule(ctx, slide, BAND_TOP, colour=RULE, left=0, width=SLIDE_W, weight=0.012)
    if not lines:
        _text(ctx, slide, "INPUTS", MARGIN, BAND_TOP + 0.14, span(2), 0.22,
              size=MICRO, bold=True, colour=GOOD, spacing=True)
        _text(ctx, slide,
              "Clean apart from the two constants on every valuation here: the "
              "equity risk premium (no free source) and the 3% near-term growth "
              "rate (an assumption, not a forecast).",
              col(2), BAND_TOP + 0.12, span(10), 0.6, size=MICRO, colour=INK_2)
        return
    _text(ctx, slide, f"INPUTS CARRY ({len(lines)})", MARGIN, BAND_TOP + 0.14,
          span(2), 0.22, size=MICRO, bold=True, colour=WARN, spacing=True)
    # Numbered, two columns, so eight caveats stay readable rather than becoming a
    # paragraph nobody finishes.
    half = (len(lines) + 1) // 2
    for which, chunk in enumerate((lines[:half], lines[half:])):
        if not chunk:
            continue
        body = "\n".join(f"{i + 1 + which * half}.  {t}"
                         for i, t in enumerate(chunk))
        _text(ctx, slide, body, col(2 + which * 5), BAND_TOP + 0.12,
              span(5), SLIDE_H - BAND_TOP - 0.18, size=MICRO, colour=INK_2)


def _text(ctx: _Ctx, slide: Any, text: str, left: float, top: float,
          width: float, height: float, *, size: int = BODY, bold: bool = False,
          colour: str = INK, font: str = FONT_BODY, align: str = "left",
          spacing: bool = False) -> Any:
    """One textbox, fully specified.

    Margins are zeroed because the grid already decides where text starts; pptx's
    default 0.1in inset would put every cell a tenth of an inch off its column.
    """
    box = slide.shapes.add_textbox(ctx.Inches(left), ctx.Inches(top),
                                   ctx.Inches(width), ctx.Inches(height))
    frame = box.text_frame
    frame.word_wrap = True
    frame.margin_left = frame.margin_right = 0
    frame.margin_top = frame.margin_bottom = 0
    lines = str(text).split("\n")
    for i, line in enumerate(lines):
        para = frame.paragraphs[0] if i == 0 else frame.add_paragraph()
        if align == "right":
            para.alignment = ctx.align.RIGHT
        elif align == "center":
            para.alignment = ctx.align.CENTER
        para.line_spacing = 1.18
        run = para.add_run()
        run.text = line
        run.font.size = ctx.Pt(size)
        run.font.bold = bold
        run.font.name = font
        run.font.color.rgb = ctx.RGBColor.from_string(colour)
        if spacing:
            # Letter-spaced small caps for a standing label. `spc` is in hundredths
            # of a point and there is no python-pptx property for it.
            run.font._rPr.set("spc", "80")
    return box


#: Which columns in a `_rows` table hold numbers. Right-aligned, because a column of
#: figures that is not aligned on its last digit cannot be scanned -- and alignment
#: by paragraph is reliable where trusting a font's tabular figures is not.
def _rows(ctx: _Ctx, slide: Any, rows: list[tuple[str, ...]], *,
          top: float = CONTENT_TOP, size: int = BODY,
          widths: tuple[float, ...] = (), numeric: tuple[int, ...] = (1,),
          pitch: float = 0.3, left: float = MARGIN,
          width: float | None = None) -> float:
    """A table on the grid: a header in small caps, a rule under it, zebra-free rows.

    No pptx table object, which was the right call and stays: a textbox grid is
    smaller, renders identically everywhere, and cannot inherit a theme. What it
    lacked was alignment and a header that reads as one.

    Returns the y the table ended at, so a caller can place the next element relative
    to it rather than guessing a constant -- which is how the old pages ended up with
    text overlapping a table whenever a row count changed.
    """
    if not rows:
        return top
    avail = content_width() if width is None else width
    cols = max(len(r) for r in rows)
    widths = widths or tuple([avail / cols] * cols)
    # **Normalised here, not at every call site.** The column tuples were written by
    # hand against the old margin and sum to 12.1in against a content width of
    # 12.03in, so every table overhung the grid by a different amount. Scaling them
    # to the content width keeps each column's *proportion* -- which is the part that
    # was a design decision -- while making all ten pages end on the same line.
    total = sum(widths)
    if total > 0:
        widths = tuple(w * avail / total for w in widths)
    head, body = rows[0], rows[1:]

    x = left
    for j in range(cols):
        cell = head[j] if j < len(head) else ""
        _text(ctx, slide, str(cell).upper(), x, top, widths[j], 0.24,
              size=MICRO, bold=True, colour=MUTED, spacing=True,
              align="right" if j in numeric else "left")
        x += widths[j]
    _rule(ctx, slide, top + 0.26, colour=RULE, left=left, width=sum(widths))

    y = top + 0.38
    for row in body:
        x = left
        for j in range(cols):
            cell = row[j] if j < len(row) else ""
            _text(ctx, slide, str(cell), x, y, widths[j], pitch - 0.02,
                  size=size, colour=INK if j == 0 else INK_2,
                  bold=(j == 0),
                  align="right" if j in numeric else "left",
                  font=FONT_BODY)
            x += widths[j]
        y += pitch
    return y


def _money(value: Any) -> str:
    """One unit, two decimals, and a dash for absent.

    `--` rather than `0` or an empty cell: a zero is a measurement and an empty cell
    is ambiguous between "not read" and "nothing there", which is the distinction
    every status column in this system exists to keep.
    """
    if value in (None, ""):
        return "--"
    try:
        n = float(value)
    except (TypeError, ValueError):
        return str(value)
    for cut, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= cut:
            return f"${n / cut:,.2f}{suffix}"
    return f"${n:,.0f}"


def _pct(value: Any, places: int = 1) -> str:
    if value in (None, ""):
        return "--"
    try:
        return f"{float(value) * 100:.{places}f}%"
    except (TypeError, ValueError):
        return str(value)


def _num(value: Any, places: int = 2) -> str:
    if value in (None, ""):
        return "--"
    try:
        return f"{float(value):,.{places}f}"
    except (TypeError, ValueError):
        return str(value)


def candles(ctx: _Ctx, slide: Any, bars: list[tuple[Any, ...]], *,
            left: float, top: float, width: float, height: float,
            vol_share: float = 0.26) -> None:
    """Monthly candles and a volume strip, drawn as shapes.

    **The same marks the dashboard draws, from the same aggregation.** `bars` comes
    from `dashboard.tickers.aggregate`, so a month on a slide and a month on the
    panel are the same bar by construction rather than by two renderers agreeing.
    Body from open to close, wick from low to high, coloured by direction, volume
    beneath sharing the x-axis.

    Monthly rather than daily, and that is a rendering constraint rather than a
    preference: a candle is three shapes, so eleven years of sessions would be ~8,000
    shapes on one slide. Monthly is ~130. The resolution is stated on the page for
    the same reason the dashboard states it -- a monthly candle read as a daily one
    is a wrong answer about what a day did.

    The x-axis is **bar ordinal here, not time**, which is the one place this departs
    from the dashboard: a slide has no hover to explain an empty stretch, and at
    monthly resolution a gap is visible as a flat run rather than being hidden. The
    gap count travels in the footnote band instead.
    """
    if not bars:
        _text(ctx, slide, "No price history. A delisted company has none -- which "
                          "is what an acquisition looks like from inside a "
                          "survivor-only universe.",
              left, top, width, 0.5, size=BODY, colour=WARN)
        return
    price_h = height * (1 - vol_share) - 0.08
    vol_top = top + price_h + 0.08
    vol_h = height * vol_share

    lo = min(b[3] for b in bars)
    hi = max(b[2] for b in bars)
    if hi <= lo:
        hi, lo = lo * 1.02 or 1.0, lo * 0.98
    vmax = max((b[5] for b in bars), default=0) or 1
    pad = (hi - lo) * 0.06
    lo, hi = lo - pad, hi + pad

    def y(v: float) -> float:
        return top + (hi - v) / (hi - lo) * price_h

    slot = width / len(bars)
    body_w = max(0.012, slot * 0.62)

    # Gridlines behind, four of them, labelled at the left.
    for i in range(5):
        val = lo + (hi - lo) * i / 4
        gy = y(val)
        _rule(ctx, slide, gy, colour=RULE, left=left, width=width, weight=0.006)
        _text(ctx, slide, _num(val, 2 if val >= 1 else 4),
              left - 0.78, gy - 0.08, 0.72, 0.18, size=MICRO, colour=MUTED,
              align="right")

    for i, bar in enumerate(bars):
        cx = left + slot * (i + 0.5)
        up = bar[4] >= bar[1]
        hue = CANDLE_UP if up else CANDLE_DOWN
        _rect(ctx, slide, cx - 0.006, y(bar[2]), 0.012,
              max(0.006, y(bar[3]) - y(bar[2])), fill=hue)
        y_o, y_c = y(bar[1]), y(bar[4])
        _rect(ctx, slide, cx - body_w / 2, min(y_o, y_c), body_w,
              max(0.014, abs(y_c - y_o)), fill=hue)
        vh = (bar[5] / vmax) * vol_h
        _rect(ctx, slide, cx - body_w / 2, vol_top + vol_h - vh, body_w,
              max(0.006, vh), fill=hue)

    _rule(ctx, slide, vol_top + vol_h, colour=MUTED, left=left, width=width,
          weight=0.008)
    _text(ctx, slide, f"volume, peak {_compact(vmax)}", left, vol_top + vol_h + 0.05,
          span(4), 0.2, size=MICRO, colour=MUTED)


def _compact(n: float) -> str:
    for cut, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= cut:
            return f"{n / cut:,.1f}{suffix}"
    return f"{n:,.0f}"


# --- the ten pages ------------------------------------------------------


def _page_cover(ctx: _Ctx) -> None:
    """The figure on the left, **the data-quality panel on the right.**

    Two halves of equal weight, which is the layout decision that carries the
    constraint: a cover that is all number invites the number being taken at face
    value, and a cover that is all caveat does not get read. Side by side, the panel
    reads as part of the answer rather than as a retraction of it.
    """
    s = ctx.subject
    slide = add_page(ctx, "", chrome=False)

    # A narrow accent rule at the very top, the only decoration in the deck.
    _rect(ctx, slide, 0, 0, SLIDE_W, 0.085, fill=ACCENT)

    _text(ctx, slide, s.company, MARGIN, 0.75, span(7), 0.9,
          size=COVER_NAME, font=FONT_DISPLAY, colour=INK)
    meta = "  ·  ".join(x for x in (
        f"CIK {s.cik}", s.ticker or None,
        f"SIC {s.sic}" if s.sic else None) if x)
    _text(ctx, slide, meta, MARGIN, 1.62, span(7), 0.3, size=H2, colour=MUTED)
    _rule(ctx, slide, 2.05, colour=RULE, left=MARGIN, width=span(7))

    ev = s.valuation.get("enterprise_value")
    _text(ctx, slide, "ENTERPRISE VALUE", MARGIN, 2.3, span(7), 0.22,
          size=MICRO, bold=True, colour=MUTED, spacing=True)
    _text(ctx, slide, _money(ev) if ev else "no valuation",
          MARGIN, 2.58, span(7), 1.0, size=DISPLAY, bold=True,
          font=FONT_DISPLAY, colour=INK)
    _text(ctx, slide, "discounted free cash flow, perpetuity terminal value",
          MARGIN, 3.62, span(7), 0.3, size=SMALL, colour=MUTED)

    wacc, term = s.valuation.get("wacc"), s.valuation.get("terminal_share")
    _rows(ctx, slide, [
        ("input", "value"),
        ("WACC", _pct(wacc)),
        ("terminal share of value", _pct(term, 0)),
        ("beta", _num(s.valuation.get("beta"))),
        ("near-term growth", _pct(s.valuation.get("growth"), 0)),
    ], top=4.05, widths=(span(4), span(2)), numeric=(1,), pitch=0.27,
        width=span(6))

    # --- the data-quality panel -----------------------------------------
    px, pw = col(7), span(5)
    _rect(ctx, slide, px, 0.75, pw, BAND_TOP - 1.1, fill=PANEL)
    clean = s.clean_but_constants_like
    _text(ctx, slide, "DATA QUALITY", px + 0.22, 0.98, pw - 0.44, 0.22,
          size=MICRO, bold=True, colour=GOOD if clean else WARN, spacing=True)
    _text(ctx, slide, s.weakest, px + 0.22, 1.26, pw - 0.44, 0.62,
          size=LEAD, font=FONT_DISPLAY, colour=INK)
    _rule(ctx, slide, 1.98, colour=RULE, left=px + 0.22, width=pw - 0.44)

    _text(ctx, slide, f"{len(s.substitutions)} SUBSTITUTED INPUT"
                      f"{'' if len(s.substitutions) == 1 else 'S'}",
          px + 0.22, 2.14, pw - 0.44, 0.22, size=MICRO, bold=True,
          colour=MUTED, spacing=True)
    y = 2.44
    for name in s.substitutions:
        unavoidable = name in ("erp_constant", "growth_constant")
        _rect(ctx, slide, px + 0.22, y + 0.04, 0.055, 0.12,
              fill=MUTED if unavoidable else WARN)
        _text(ctx, slide, SUBSTITUTION_WORDS.get(name, name),
              px + 0.38, y, pw - MARGIN, 0.34, size=SMALL,
              colour=INK_2 if unavoidable else INK)
        y += 0.38
    if not s.substitutions:
        _text(ctx, slide, "None. Unusual -- every valuation here carries at least "
                          "the two constants.", px + 0.22, y, pw - 0.44, 0.4,
              size=SMALL, colour=MUTED)
        y += 0.4

    # **One block, two paragraphs, placed after the list.** These were two boxes at
    # fixed tops, which collided the moment a filer had six substitutions instead of
    # two -- the overlap audit caught it on TRACON. A single box cannot overlap
    # itself, and following `y` means the list's length decides where it sits rather
    # than a constant that happened to suit the fixture.
    legend = (
        "Grey marks a constant with no free source, on every valuation here. Amber "
        "marks something substituted for this filer specifically.\n"
        "Not a market valuation. No market capitalisation appears anywhere in this "
        "deck: yfinance fundamentals are current values with no as-of date, which is "
        "wrong for anything historical."
    )
    _text(ctx, slide, legend, px + 0.22, min(max(y + 0.14, 4.5), 4.95),
          pw - 0.44, 1.05, size=MICRO, colour=MUTED)


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
              MARGIN, 3.4, content_width(), 0.8, size=H2, colour=WARN)


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
          MARGIN, 3.6, content_width(), 0.8, size=SMALL, colour=MUTED)


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
        _rows(ctx, slide, rows, top=3.7, size=BODY, widths=(3.4, 8.5))


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
        _text(ctx, slide, "No valuation, so nothing to flex.", MARGIN, 2.0,
              8.0, 0.5, size=LEAD, colour=MUTED)
        return
    if not flex:
        # Said rather than drawn empty: a missing flex means the row was written
        # by an older screen, which is a fact about the row and not about the
        # company.
        _text(ctx, slide,
              "This valuation carries no stored sensitivity. Re-run `mr dcf` -- "
              "the deck renders the flex rather than deriving it, so that this "
              "page and the dashboard cannot disagree.",
              MARGIN, 2.0, content_width(), 0.8, size=LEAD, colour=WARN)
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
          MARGIN, 4.2, content_width(), 0.8, size=SMALL, colour=MUTED)


def _page_insiders(ctx: _Ctx) -> None:
    s = ctx.subject
    slide = add_page(ctx, "Insider clusters",
                     "Form 4 buys, clustered. A cluster is the signal; a single "
                     "filing is not.")
    if not s.insiders:
        _text(ctx, slide, "No Form 4 clusters on record for this issuer.",
              MARGIN, 2.0, 8.0, 0.5, size=LEAD, colour=MUTED)
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
              MARGIN, 2.0, 8.0, 0.5, size=LEAD, colour=MUTED)
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
    """Monthly candles, from the aggregation the dashboard uses.

    Imported rather than reimplemented: the deck and the panel must not disagree
    about what a month did, and the only way to guarantee that is one function. A
    second implementation here would agree right up until one of them was changed.
    """
    s = ctx.subject
    slide = add_page(ctx, "Price history",
                     "Monthly candles -- open of the first session, close of the "
                     "last, max high, min low, summed volume")
    if not s.prices:
        candles(ctx, slide, [], left=col(0) + 0.8, top=CONTENT_TOP,
                width=span(12) - 0.8, height=3.2)
        return

    from marketradar.dashboard.tickers import aggregate, _month

    bars = aggregate(list(s.prices), _month)[-132:]
    candles(ctx, slide, bars, left=col(0) + 0.82, top=CONTENT_TOP + 0.1,
            width=span(12) - 0.82, height=3.05)

    lo = min(b[3] for b in bars)
    hi = max(b[2] for b in bars)
    first, last = s.prices[0], s.prices[-1]
    y = _rows(ctx, slide, [
        ("window", "sessions", "months drawn", "range", "last close"),
        (f"{first[0]} to {last[0]}", f"{len(s.prices):,}", f"{len(bars):,}",
         f"{_num(lo, 4)} - {_num(hi, 4)}", _num(last[4], 4)),
    ], top=CONTENT_TOP + 3.45,
        widths=(span(3), span(2), span(2), span(3), span(2)),
        numeric=(1, 2, 3, 4))

    # **Raw, not back-adjusted, and that is stated rather than implied.** The
    # dashboard's candles are raw too, so the two agree -- but a reverse split
    # therefore reads as a cliff on both, and inferring the ratio from the jump is
    # exactly the fabrication the action table refuses. Cumulative back-adjustment
    # is owed work, not a caveat to bury.
    note = ("Raw prices, not back-adjusted -- the same basis the dashboard draws, "
            "so a split reads as a step on both. ")
    if s.unexplained_moves:
        note += (f"{s.unexplained_moves} large single-session moves have no "
                 "corporate action on record to explain them; Tiingo's per-bar "
                 "split factor is itself incomplete, worst on exactly the small "
                 "tickers where reverse splits are constant.")
    else:
        note += "No unexplained single-session moves on record for this ticker."
    _text(ctx, slide, note, MARGIN, y + 0.12, content_width(), 0.5,
          size=SMALL, colour=WARN if s.unexplained_moves else MUTED)
