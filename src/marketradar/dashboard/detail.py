"""U3 -- the ticker detail drawer: chart, recent bars, actions, gap markers.

Drawing rules come from the dataviz skill. A price history is one series over
time, so: a 2px round-capped line, a hairline recessive grid, no legend (the
title names it), and a crosshair-and-tooltip hover layer.

**Gaps are annotations, not a second series.** A gap is an absence of data. A
second colour or a second axis would imply it carries a value, and the line is
*broken* across one rather than drawn straight through -- a straight segment
spanning three years of nothing is the chart telling a lie politely.

The chart is drawn on demand rather than pre-rendered: 330 inline SVGs would
add megabytes to a file that has to open from disk, to save one click.
"""

from __future__ import annotations

from typing import Final


def panel_html() -> str:
    """The drawer. Empty until a ticker row is clicked."""
    return """
      <div class="tk" id="tk" hidden>
        <header class="tkhead">
          <h4 id="tk-sym"></h4>
          <span id="tk-meta" class="note"></span>
          <button id="tk-x" class="f">close</button>
        </header>
        <svg id="tk-chart" viewBox="0 0 760 220" preserveAspectRatio="none"
             role="img" aria-label="price history"></svg>
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
  var W = 760, H = 220, PL = 48, PR = 12, PT = 12, PB = 22;

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

  function draw(sym, d) {
    /* Resolved once, then held as locals for the rest of the draw: every
       element this function touches belongs to whichever panel is in the DOM
       at this moment, and re-querying per append would say otherwise. */
    var svg = svgEl(), b = box();
    if (!svg || !b) { return false; }
    while (svg.firstChild) { svg.removeChild(svg.firstChild); }
    var s = d.s || [];
    if (!s.length) { return false; }
    var x0 = s[0][0], x1 = s[s.length - 1][0];
    var lo = Infinity, hi = -Infinity;
    for (var i = 0; i < s.length; i++) {
      if (s[i][1] < lo) { lo = s[i][1]; }
      if (s[i][1] > hi) { hi = s[i][1]; }
    }
    if (hi === lo) { hi = lo + 1; }
    var pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
    var span = Math.max(1, x1 - x0);
    function X(v) { return PL + (v - x0) / span * (W - PL - PR); }
    function Y(v) { return PT + (hi - v) / (hi - lo) * (H - PT - PB); }

    var gaps = d.g || [];
    // Bands first so the line sits above them.
    for (var gI = 0; gI < gaps.length; gI++) {
      svg.appendChild(el('rect', {
        x: X(gaps[gI][0]), y: PT,
        width: Math.max(1.5, X(gaps[gI][1]) - X(gaps[gI][0])),
        height: H - PT - PB, fill: 'var(--gapfill)'
      }));
    }

    for (var r = 0; r <= 4; r++) {
      var val = lo + (hi - lo) * r / 4, gy = Y(val);
      svg.appendChild(el('line', {
        x1: PL, x2: W - PR, y1: gy, y2: gy,
        stroke: 'var(--rule)', 'stroke-width': 1
      }));
      var lab = el('text', {
        x: PL - 6, y: gy + 3, 'text-anchor': 'end',
        fill: 'var(--muted)', 'font-size': 9
      });
      lab.textContent = val >= 100 ? val.toFixed(0) : val.toFixed(2);
      svg.appendChild(lab);
    }

    // One path per continuous run. A straight segment drawn across three
    // years of absence is the chart lying politely.
    var runs = [[]], gp = 0;
    for (var p = 0; p < s.length; p++) {
      while (gp < gaps.length && s[p][0] > gaps[gp][0]) {
        if (runs[runs.length - 1].length) { runs.push([]); }
        gp++;
      }
      runs[runs.length - 1].push(s[p]);
    }
    for (var rr = 0; rr < runs.length; rr++) {
      var run = runs[rr];
      if (!run.length) { continue; }
      if (run.length === 1) {
        svg.appendChild(el('circle', {
          cx: X(run[0][0]), cy: Y(run[0][1]), r: 4, fill: 'var(--series)'
        }));
        continue;
      }
      var dd = '';
      for (var q = 0; q < run.length; q++) {
        dd += (q ? 'L' : 'M') + X(run[q][0]).toFixed(1) + ' ' + Y(run[q][1]).toFixed(1);
      }
      svg.appendChild(el('path', {
        d: dd, fill: 'none', stroke: 'var(--series)', 'stroke-width': 2,
        'stroke-linejoin': 'round', 'stroke-linecap': 'round'
      }));
    }

    svg.appendChild(el('line', {
      x1: PL, x2: W - PR, y1: H - PB, y2: H - PB,
      stroke: 'var(--axis)', 'stroke-width': 1
    }));
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
    var dot = el('circle', {
      r: 4, fill: 'var(--series)', stroke: 'var(--surface)',
      'stroke-width': 2, opacity: 0
    });
    svg.appendChild(cross);
    svg.appendChild(dot);

    var meta = document.getElementById('tk-meta');
    var base = s.length + ' points, ' + iso(x0) + ' to ' + iso(x1);
    meta.textContent = base;

    svg.onmousemove = function (ev) {
      var rect = svg.getBoundingClientRect();
      var vx = (ev.clientX - rect.left) / rect.width * W;
      var best = null, bd = 1e9;
      for (var b = 0; b < s.length; b++) {
        var dx = Math.abs(X(s[b][0]) - vx);
        if (dx < bd) { bd = dx; best = s[b]; }
      }
      if (!best) { return; }
      cross.setAttribute('x1', X(best[0]));
      cross.setAttribute('x2', X(best[0]));
      cross.setAttribute('opacity', 1);
      dot.setAttribute('cx', X(best[0]));
      dot.setAttribute('cy', Y(best[1]));
      dot.setAttribute('opacity', 1);
      meta.textContent = iso(best[0]) + '   ' + fx(best[1]);
    };
    svg.onmouseleave = function () {
      cross.setAttribute('opacity', 0);
      dot.setAttribute('opacity', 0);
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

    var rows = d.r || [], bt = '';
    for (var br = 0; br < rows.length; br++) {
      var b2 = rows[br];
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

    document.getElementById('tk-sym').textContent = sym;
    b.hidden = false;
    return true;
  }

  /* Delegated on document, so it survives every panel switch. It routes
     through the shell when there is one: the shell opens the ticker panel in
     the main area first, then asks for the draw. Standalone, it draws in
     place as before. */
  window.__MR_TK_DRAW__ = function (sym) {
    if (D[sym]) { return draw(sym, D[sym]); }
    return false;
  };
  window.__MR_TK_HAS__ = function (sym) { return !!D[sym]; };
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
