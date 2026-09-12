"""U3 -- the ticker detail drawer: candles, volume, timeframes, gaps, actions.

**Candles, because the row already holds OHLC.** A line of closes discards three
quarters of every bar. Body from open to close, wick from low to high, coloured by
direction, with volume as a strip beneath sharing the x-axis.

**The x-axis is time, not bar ordinal, and that is the load-bearing choice.** Most
charting libraries lay candles out evenly and skip non-trading days, which looks
tidier and would quietly close AAAP's 3,045-day hole -- the discontinuity the gap
bands exist to show. On a time axis a gap is empty space, which is what it is. The
cost is that weekends leave small holes between candles; that is the honest version.

**Gaps are annotations, not a second series.** A gap is an absence of data. A second
colour or axis would imply it carries a value.

**Aggregation is labelled.** At 5Y and All the candles are weekly and monthly
aggregates -- open of the first session, close of the last, max high, min low, summed
volume -- and the chart says so, because a monthly candle read as a daily one is a
wrong answer about what a day did.

The chart is drawn on demand rather than pre-rendered: 345 inline SVGs would add
megabytes to a file that has to open from disk, to save one click.
"""

from __future__ import annotations

from typing import Final

#: The timeframes, in the order they render. ``days`` is the window measured back
#: from the last bar; ``res`` names which series answers it. ``None`` days means the
#: whole series.
#:
#: 1D is one session and is included because it was asked for, but say what it is:
#: with end-of-day bars a single candle is the whole of it, so the label reads
#: "1 session" rather than implying an intraday chart we have no data for.
TIMEFRAMES: Final[tuple[tuple[str, int | None, str], ...]] = (
    ("1D", 1, "d"),
    ("1W", 7, "d"),
    ("1M", 31, "d"),
    ("3M", 92, "d"),
    ("YTD", 0, "d"),          # 0 is the sentinel for "since Jan 1"
    ("1Y", 366, "d"),
    ("5Y", 1827, "w"),
    ("All", None, "m"),
)

#: What the default has to be. Not the widest: the widest view of an eleven-year
#: history is monthly candles, and the question a screen row raises is what the last
#: few months did.
DEFAULT_TIMEFRAME: Final[str] = "3M"

#: How the three resolutions describe themselves on the chart.
RESOLUTION_LABEL: Final[dict[str, str]] = {
    "d": "daily candles",
    "w": "weekly candles -- open of the first session, close of the last, max high, "
         "min low, summed volume",
    "m": "monthly candles -- open of the first session, close of the last, max high, "
         "min low, summed volume",
}


def _buttons() -> str:
    return "".join(
        f'<button class="tf" data-tf="{name}" type="button">{name}</button>'
        for name, _days, _res in TIMEFRAMES
    )


def panel_html() -> str:
    """The drawer. Empty until a ticker row is clicked."""
    return f"""
      <div class="tk" id="tk" hidden>
        <header class="tkhead">
          <h4 id="tk-sym"></h4>
          <span id="tk-meta" class="note"></span>
          <button id="tk-x" class="f">close</button>
        </header>
        <div class="tkbar">
          <span class="tfs" id="tk-tfs" role="group"
                aria-label="timeframe">{_buttons()}</span>
          <label class="tflog"><input type="checkbox" id="tk-log"> log</label>
        </div>
        <svg id="tk-chart" viewBox="0 0 760 260" preserveAspectRatio="none"
             role="img" aria-label="price history"></svg>
        <p class="note" id="tk-res"></p>
        <div id="tk-gaps" class="tkgaps"></div>
        <div class="tkcols">
          <div><h5>recent bars</h5><table id="tk-bars" class="rows"></table></div>
          <div><h5>corporate actions</h5><table id="tk-act" class="rows"></table></div>
        </div>
      </div>
      <p class="note">Click any ticker above for its full history, corporate
        actions, and the gaps the screen suppresses.</p>"""


def empty_html() -> str:
    """No chart payload in this render, which is not the same as no history.

    The panel chips live off the price partitions, so it has to own its empty
    case the way the other nine bodies do -- and name the command, because the
    payload is built from the screen rows and an empty screen is the usual
    reason there is none.
    """
    return """
      <p class="empty">No chart payload in this render. The history is in the
        partitions; this page did not build a series for any screen row
        &mdash; usually because the screens came back empty. Re-run
        <code>mr dashboard</code> after <code>mr screens</code>.</p>"""


SCRIPT: Final[str] = r"""
(function () {
  var D = window.__TK__ || {}, EPOCH = Date.UTC(2016, 0, 1), DAY = 86400000;
  /* Resolved per draw, not at load: the ticker panel is injected on demand
     now, so at load time none of these elements exist.

     The load-time guard that used to stand here outlived the variables it
     tested. It read a free `svg`, which is a ReferenceError rather than a
     falsy value, so it did not skip the drawer -- it took the whole script
     down, including the delegated click handlers at the bottom that have
     nothing to do with the chart. The guard belongs per draw, where the
     elements either exist or do not. */
  function box() { return document.getElementById('tk'); }
  function svgEl() { return document.getElementById('tk-chart'); }

  /* Two panes sharing one x-axis: price above, volume below. PV is the price
     pane's height, so the volume strip gets what is left under it. */
  var W = 760, H = 260, PL = 48, PR = 12, PT = 12, PB = 22, PV = 176, VGAP = 8;

  var TF = __TF__;
  var RES = __RES__;

  /* **Remembered across ticker switches**, which is the point: reading 1M and
     clicking a different name keeps 1M. A module-level variable does that much
     on its own; localStorage is attempted on top so it also survives a reload,
     in a try/catch because a file:// page is an opaque origin where touching it
     throws outright rather than returning null. */
  var tf = '__DEFAULT__', useLog = false;
  try {
    var saved = window.localStorage.getItem('mr.tk.tf');
    if (saved) { for (var t = 0; t < TF.length; t++) { if (TF[t][0] === saved) { tf = saved; } } }
    useLog = window.localStorage.getItem('mr.tk.log') === '1';
  } catch (e) { /* opaque origin; the in-memory default stands */ }
  function remember(k, v) {
    try { window.localStorage.setItem('mr.tk.' + k, v); } catch (e) {}
  }

  var shown = null;   /* the symbol currently drawn, for a redraw on tf change */

  function iso(d) { return new Date(EPOCH + d * DAY).toISOString().slice(0, 10); }
  function el(n, a) {
    var e = document.createElementNS('http://www.w3.org/2000/svg', n);
    for (var k in a) { e.setAttribute(k, a[k]); }
    return e;
  }
  function money(v) {
    if (v >= 1e9) { return (v / 1e9).toFixed(1) + 'B'; }
    if (v >= 1e6) { return (v / 1e6).toFixed(1) + 'M'; }
    if (v >= 1e3) { return (v / 1e3).toFixed(0) + 'K'; }
    return String(v);
  }
  function fx(v) { return v >= 100 ? v.toFixed(2) : v.toFixed(4); }

  function spec(name) {
    for (var i = 0; i < TF.length; i++) { if (TF[i][0] === name) { return TF[i]; } }
    return TF[3];
  }

  /* The window for a timeframe, off whichever resolution serves it. Sliced from
     the END of the series rather than by index count: the timeframe is a period,
     and a fixed bar count would give a different span for a thinly-traded name
     than for a liquid one. */
  function slice(d, name) {
    var sp = spec(name), series = d[sp[2]] || [], days = sp[1];
    if (!series.length) { return { bars: [], res: sp[2] }; }
    var last = series[series.length - 1][0], from;
    if (days === null) {
      from = -Infinity;
    } else if (days === 0) {
      var y = new Date(EPOCH + last * DAY).getUTCFullYear();
      from = (Date.UTC(y, 0, 1) - EPOCH) / DAY;
    } else {
      from = last - days + 1;
    }
    var bars = [];
    for (var i = 0; i < series.length; i++) {
      if (series[i][0] >= from) { bars.push(series[i]); }
    }
    /* A window that lands inside a gap can come back empty or with a single bar.
       Showing nothing is correct and is said in words rather than left blank. */
    return { bars: bars, res: sp[2] };
  }

  function draw(sym, d) {
    /* Resolved once, then held as locals for the rest of the draw: every
       element this function touches belongs to whichever panel is in the DOM
       at this moment, and re-querying per append would say otherwise. */
    var svg = svgEl(), b = box();
    if (!svg || !b) { return false; }
    while (svg.firstChild) { svg.removeChild(svg.firstChild); }
    shown = sym;

    var cut = slice(d, tf), s = cut.bars;
    var meta = document.getElementById('tk-meta');
    var resNote = document.getElementById('tk-res');
    document.getElementById('tk-sym').textContent = sym;
    paintButtons();

    if (!s.length) {
      resNote.textContent = 'No bars in ' + tf + '. The window falls inside a gap '
        + 'in this name’s history -- try a wider timeframe.';
      meta.textContent = '';
      b.hidden = false;
      return true;
    }

    var x0 = s[0][0], x1 = s[s.length - 1][0];
    var lo = Infinity, hi = -Infinity, vmax = 0;
    for (var i = 0; i < s.length; i++) {
      if (s[i][3] < lo) { lo = s[i][3]; }       /* low  */
      if (s[i][2] > hi) { hi = s[i][2]; }       /* high */
      if (s[i][5] > vmax) { vmax = s[i][5]; }
    }
    /* Log needs every value strictly positive. Prices here go to 0.0002 so that
       normally holds, but a zero or a negative would make Math.log infinite and
       draw nothing, so the toggle degrades rather than breaking. */
    var logOk = useLog && lo > 0;
    if (hi === lo) { hi = lo * 1.02 || 1; lo = lo * 0.98; }

    var lv = logOk ? Math.log(lo) : lo, hv = logOk ? Math.log(hi) : hi;
    var padv = (hv - lv) * 0.06; lv -= padv; hv += padv;

    var span = Math.max(1, x1 - x0);
    var PW = W - PL - PR;
    function X(v) { return PL + (v - x0) / span * PW; }
    function Y(v) {
      var t = logOk ? Math.log(v) : v;
      return PT + (hv - t) / (hv - lv) * (PV - PT);
    }
    var VTOP = PV + VGAP, VH = H - PB - VTOP;
    function VY(v) { return H - PB - (vmax ? v / vmax : 0) * VH; }

    /* Candle width from the median spacing rather than span/count: one long gap
       would otherwise shrink every candle to a hair. Floored at 1px so a dense
       window still shows a mark per bar. */
    var deltas = [];
    for (var g = 1; g < s.length; g++) { deltas.push(s[g][0] - s[g - 1][0]); }
    deltas.sort(function (a, c) { return a - c; });
    var step = deltas.length ? deltas[Math.floor(deltas.length / 2)] : 1;
    var cw = Math.max(1, Math.min(18, (step / span) * PW * 0.72));

    var gaps = d.g || [];
    /* Bands first so the candles sit above them. Clipped to the window, or a gap
       outside it would paint the whole pane. */
    for (var gI = 0; gI < gaps.length; gI++) {
      var ga = gaps[gI][0], gb = gaps[gI][1];
      if (gb < x0 || ga > x1) { continue; }
      var gx = X(Math.max(ga, x0)), gx2 = X(Math.min(gb, x1));
      svg.appendChild(el('rect', {
        x: gx, y: PT, width: Math.max(1.5, gx2 - gx),
        height: H - PT - PB, fill: 'var(--gapfill)'
      }));
    }

    for (var r = 0; r <= 4; r++) {
      var frac = r / 4;
      var val = logOk ? Math.exp(lv + (hv - lv) * frac) : lv + (hv - lv) * frac;
      var gy = Y(val);
      svg.appendChild(el('line', {
        x1: PL, x2: W - PR, y1: gy, y2: gy,
        stroke: 'var(--rule)', 'stroke-width': 1
      }));
      var lab = el('text', {
        x: PL - 6, y: gy + 3, 'text-anchor': 'end',
        fill: 'var(--muted)', 'font-size': 9
      });
      lab.textContent = val >= 100 ? val.toFixed(0)
        : (val >= 1 ? val.toFixed(2) : val.toFixed(4));
      svg.appendChild(lab);
    }

    /* Candles. One group per bar so the hover layer can find them, wick first so
       the body covers its middle. */
    for (var k = 0; k < s.length; k++) {
      var bar = s[k], cx = X(bar[0]);
      var up = bar[4] >= bar[1];
      var hue = up ? 'var(--cup)' : 'var(--cdown)';
      svg.appendChild(el('line', {
        x1: cx, x2: cx, y1: Y(bar[2]), y2: Y(bar[3]),
        stroke: hue, 'stroke-width': 1, 'class': 'ck-wick'
      }));
      var yo = Y(bar[1]), yc = Y(bar[4]);
      var top = Math.min(yo, yc), hgt = Math.max(1, Math.abs(yc - yo));
      svg.appendChild(el('rect', {
        x: cx - cw / 2, y: top, width: cw, height: hgt,
        fill: hue, 'class': 'ck-body'
      }));
      if (vmax) {
        svg.appendChild(el('rect', {
          x: cx - cw / 2, y: VY(bar[5]), width: cw,
          height: Math.max(0.5, H - PB - VY(bar[5])),
          fill: hue, opacity: 0.45, 'class': 'ck-vol'
        }));
      }
    }

    /* Volume baseline, so the strip reads as its own pane. */
    svg.appendChild(el('line', {
      x1: PL, x2: W - PR, y1: H - PB, y2: H - PB,
      stroke: 'var(--axis)', 'stroke-width': 1
    }));
    var vt = el('text', {
      x: PL - 6, y: VTOP + 8, 'text-anchor': 'end',
      fill: 'var(--muted)', 'font-size': 9
    });
    vt.textContent = money(vmax);
    svg.appendChild(vt);

    var ends = [[x0, 'start'], [x1, 'end']];
    for (var e = 0; e < ends.length; e++) {
      var xt = el('text', {
        x: X(ends[e][0]), y: H - 8, 'text-anchor': ends[e][1],
        fill: 'var(--muted)', 'font-size': 9
      });
      xt.textContent = iso(ends[e][0]);
      svg.appendChild(xt);
    }

    var cross = el('line', {
      x1: 0, x2: 0, y1: PT, y2: H - PB,
      stroke: 'var(--axis)', 'stroke-width': 1, opacity: 0
    });
    svg.appendChild(cross);

    /* **The aggregation, named on the chart.** A monthly candle read as a daily
       one is a wrong answer about what a day did, so the resolution is stated
       rather than inferable from the tick spacing. */
    resNote.textContent = RES[cut.res] + (logOk ? ' -- log price axis' : '')
      + (useLog && !logOk ? ' -- log unavailable, a bar is zero or negative' : '');

    var base = s.length + (cut.res === 'd' ? ' sessions' : ' bars') + ', '
      + iso(x0) + ' to ' + iso(x1);
    if (tf === '1D') { base = '1 session, ' + iso(x1) + ' -- end-of-day bars, so '
      + 'one candle is the whole day'; }
    meta.textContent = base;

    svg.onmousemove = function (ev) {
      var rect = svg.getBoundingClientRect();
      var vx = (ev.clientX - rect.left) / (rect.width || 1) * W;
      var best = null, bd = 1e9;
      for (var q = 0; q < s.length; q++) {
        var dx = Math.abs(X(s[q][0]) - vx);
        if (dx < bd) { bd = dx; best = s[q]; }
      }
      if (!best) { return; }
      cross.setAttribute('x1', X(best[0]));
      cross.setAttribute('x2', X(best[0]));
      cross.setAttribute('opacity', 1);
      meta.textContent = iso(best[0]) + '   O ' + fx(best[1]) + '  H ' + fx(best[2])
        + '  L ' + fx(best[3]) + '  C ' + fx(best[4]) + '  V ' + money(best[5]);
    };
    svg.onmouseleave = function () {
      cross.setAttribute('opacity', 0);
      meta.textContent = base;
    };

    var gl = document.getElementById('tk-gaps');
    if (gaps.length) {
      var parts = [];
      for (var gg = 0; gg < gaps.length; gg++) {
        parts.push(iso(gaps[gg][0]) + ' to ' + iso(gaps[gg][1]) +
                   ' (' + gaps[gg][2] + 'd)');
      }
      gl.innerHTML = '<strong>' + gaps.length + ' gap' +
        (gaps.length > 1 ? 's' : '') + ' over 30 days</strong> -- the screen ' +
        'suppresses a move across each: ' + parts.join(' &middot; ');
    } else {
      gl.innerHTML = '<span class="note">no gaps over 30 days</span>';
    }

    /* The recent table is the tail of the daily series, not a second copy of it
       shipped alongside. Daily whatever the timeframe: it answers "what price",
       which is a different question from the chart's "what shape". */
    var daily = d.d || [], tail = daily.slice(-12), bt = '';
    for (var br = 0; br < tail.length; br++) {
      var b2 = tail[br];
      bt += '<tr><td>' + iso(b2[0]) + '</td><td class="num">' + fx(b2[1]) +
            '</td><td class="num">' + fx(b2[2]) + '</td><td class="num">' +
            fx(b2[3]) + '</td><td class="num">' + fx(b2[4]) +
            '</td><td class="num">' + money(b2[5]) + '</td></tr>';
    }
    document.getElementById('tk-bars').innerHTML =
      '<thead><tr><th>date</th><th class="num">open</th><th class="num">high</th>' +
      '<th class="num">low</th><th class="num">close</th>' +
      '<th class="num">volume</th></tr></thead><tbody>' + bt + '</tbody>';

    var acts = d.a || [], at = '';
    for (var ai = 0; ai < acts.length; ai++) {
      at += '<tr><td>' + iso(acts[ai][0]) + '</td><td class="num">' +
            acts[ai][1] + '</td><td class="num">' + acts[ai][2] + '</td></tr>';
    }
    document.getElementById('tk-act').innerHTML = acts.length
      ? '<thead><tr><th>ex-date</th><th class="num">split</th>' +
        '<th class="num">dividend</th></tr></thead><tbody>' + at + '</tbody>'
      : '<tbody><tr><td class="note">none on record</td></tr></tbody>';

    b.hidden = false;
    return true;
  }

  function paintButtons() {
    var host = document.getElementById('tk-tfs');
    if (!host) { return; }
    var bs = host.getElementsByTagName('button');
    for (var i = 0; i < bs.length; i++) {
      var on = bs[i].getAttribute('data-tf') === tf;
      if (on) { bs[i].setAttribute('aria-pressed', 'true'); }
      else { bs[i].removeAttribute('aria-pressed'); }
    }
    var lg = document.getElementById('tk-log');
    if (lg) { lg.checked = useLog; }
  }

  /* Delegated, like every other handler here: the buttons only exist while the
     ticker panel is the open panel, so a load-time binding finds nothing. */
  document.addEventListener('click', function (ev) {
    var btn = ev.target.closest ? ev.target.closest('#tk-tfs button[data-tf]') : null;
    if (!btn) { return; }
    tf = btn.getAttribute('data-tf');
    remember('tf', tf);
    if (shown && D[shown]) { draw(shown, D[shown]); } else { paintButtons(); }
  });
  document.addEventListener('change', function (ev) {
    if (!ev.target || ev.target.id !== 'tk-log') { return; }
    useLog = !!ev.target.checked;
    remember('log', useLog ? '1' : '0');
    if (shown && D[shown]) { draw(shown, D[shown]); }
  });

  /* Delegated on document, so it survives every panel switch. It routes
     through the shell when there is one: the shell opens the ticker panel in
     the main area first, then asks for the draw. Standalone, it draws in
     place as before. */
  window.__MR_TK_DRAW__ = function (sym) {
    if (D[sym]) { return draw(sym, D[sym]); }
    return false;
  };
  window.__MR_TK_HAS__ = function (sym) { return !!D[sym]; };
  window.__MR_TK_TF__ = function () { return tf; };
  document.addEventListener('click', function (ev) {
    var row = ev.target.closest ? ev.target.closest('tr[data-ticker]') : null;
    if (!row) { return; }
    var sym = row.getAttribute('data-ticker');
    if (!D[sym]) { return; }
    if (window.__MR_OPEN_TICKER__) { window.__MR_OPEN_TICKER__(sym); return; }
    /* No shell: the drawer is already on the page rather than being switched
       to, so it has to be scrolled to. Without this the click reads as having
       done nothing whenever the drawer sits below the fold. The shell path
       does not need it -- switching panels puts the chart at the top. */
    if (draw(sym, D[sym]) && box().scrollIntoView) {
      box().scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
  });
  /* Delegated too: the close button only exists while the ticker panel is
     the open panel, so a load-time binding found nothing. */
  document.addEventListener('click', function (ev) {
    if (!ev.target.closest || !ev.target.closest('#tk-x')) { return; }
    if (window.__MR_BACK__) { window.__MR_BACK__(); return; }
    var b = box();
    if (b) { b.hidden = true; }
  });
})();
"""


def script() -> str:
    """The chart script, with the timeframe table injected from Python.

    One definition of the timeframes, not two. The buttons are rendered from
    `TIMEFRAMES` and the script slices from the same tuple, so a timeframe added in
    one place cannot be missing from the other -- which is the failure this file's
    own panel-map drift check exists to catch, and the one the survivorship caveat
    took three copies to learn.
    """
    import json

    table = json.dumps([[n, d, r] for n, d, r in TIMEFRAMES], separators=(",", ":"))
    return (
        SCRIPT.replace("__TF__", table)
        .replace("__RES__", json.dumps(RESOLUTION_LABEL, separators=(",", ":")))
        .replace("__DEFAULT__", DEFAULT_TIMEFRAME)
    )
