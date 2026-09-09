"""Panel bodies: health, macro, and the twenty-four screen lists.

Built from :func:`marketradar.digest.build`, deliberately. The digest and the
dashboard show the same screens, and giving them separate readers would give
them two chances to disagree about what today's moves were.

**The expansion policy is one function.** Twenty-four lists is more than
anyone scans over coffee, so most open collapsed behind a header carrying the
top row and a count. Which ones open is :func:`default_expanded` and nothing
else -- it is expected to be wrong at first and to be changed after a week of
actually reading it.

Collapse is ``<details>``/``<summary>``: native, keyboard-accessible, and
works with the page's script disabled. Sorting and filtering are a small
inline script, so the file stays one file with no build step -- self-contained
is the constraint, not script-free.
"""

from __future__ import annotations

import html
import json
from decimal import Decimal
from typing import Any, Final

from marketradar.digest import Digest
from marketradar.screens import volatility

#: Rows rendered per list. The screens hold 20; the page shows all of them
#: once a list is open, and the collapsed header shows one.
ROWS_PER_LIST: Final[int] = 20


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _key(sl: volatility.ScreenList) -> tuple[str, str, str, str]:
    return (sl.security_type, sl.band, sl.direction, sl.liquidity)


def default_expanded(
    lists: list[volatility.ScreenList],
) -> set[tuple[str, str, str, str]]:
    """Which lists are open on load.

    Two that are always worth reading -- liquid $10+ gainers and losers --
    plus whichever band actually moved today, so an unusual day in the sub-$1
    band is not hidden behind a header on the one morning it matters.

    A hypothesis, not a conclusion. Change this function; nothing else needs
    to know.
    """
    open_keys = {
        ("stock", "$10+", "gainers", "liquid"),
        ("stock", "$10+", "losers", "liquid"),
    }
    # The loudest list, chosen from the liquid ones so the default view does
    # not mix tradeable with untradeable. Collapsing hides nothing anyway --
    # every closed header carries its own top row -- so this is about what
    # opens, not about what is reachable.
    populated = [sl for sl in lists if sl.rows and sl.liquidity == "liquid"]
    if not populated:
        populated = [sl for sl in lists if sl.rows]
    if populated:
        loudest = max(
            populated,
            key=lambda sl: max(abs(m.pct_move) for m in sl.rows),
        )
        open_keys.add(_key(loudest))
    return open_keys


# --- health -------------------------------------------------------------


def health_html(digest: Digest) -> str:
    h = digest.health
    colour = "#d03b3b" if h.degraded else "#0ca30c"
    glyph = "!" if h.degraded else "OK"
    rows = "".join(
        f'<tr class="{"bad" if not i.ok else ""}">'
        f'<td class="mark">{"!" if not i.ok else ""}</td>'
        f"<td>{_esc(i.name)}</td><td>{_esc(i.detail)}</td>"
        f'<td class="note">{_esc(i.note)}</td></tr>'
        for i in h.items
    )
    return (
        f'<p class="status" style="--chip:{colour}">'
        f'<span class="glyph">{glyph}</span>{_esc(h.status)}</p>'
        f'<table class="kv">{rows}</table>'
    )


# --- macro --------------------------------------------------------------


def macro_html(digest: Digest) -> str:
    if not digest.macro:
        return '<p class="empty">No macro data. Run <code>mr fred</code>.</p>'
    rows = []
    for line in digest.macro:
        changes = "".join(
            f'<td class="num">{_esc(_signed(line.changes.get(name), line.units))}</td>'
            for _, name in [(d, n) for d, n in _lookbacks()]
        )
        rows.append(
            f"<tr><td>{_esc(line.label)}</td>"
            f'<td class="num strong">{line.value:.2f}{_esc(line.units)}</td>'
            f"{changes}"
            f'<td class="note">as of {line.as_of.isoformat()}</td></tr>'
        )
    heads = "".join(f"<th>{_esc(n)}</th>" for _, n in _lookbacks())
    return (
        '<table class="kv"><thead><tr><th></th><th>level</th>'
        f"{heads}<th></th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )


def _lookbacks():
    from marketradar.digest import MACRO_LOOKBACKS

    return MACRO_LOOKBACKS


def _signed(value: Decimal | None, units: str) -> str:
    from marketradar.digest import _signed as fmt

    return fmt(value, units)


# --- screens ------------------------------------------------------------


def _notes_html(notes: list[str]) -> str:
    """What the screen dropped. Shown as a list rather than a run-on line:
    these are four different exclusions and they are read one at a time."""
    if not notes:
        return ""
    items = "".join(f"<li>{_esc(n)}</li>" for n in notes)
    return f'<ul class="caveats">{items}</ul>'


def _row_html(m: volatility.Move, digest: Digest, fresh: set[str]) -> str:
    entry = digest.names.get(m.ticker)
    name = entry.name[:34] + (" ?" if entry.ambiguous else "") if entry else ""
    flags = []
    if m.split_factor != 1:
        flags.append(f"split x{m.split_factor.normalize()}")
    if m.is_ex_div:
        flags.append("ex-div")
    sign = "up" if m.pct_move > 0 else "down"
    return (
        f'<tr data-ticker="{_esc(m.ticker)}">'
        f'<td class="new">{"NEW" if m.ticker in fresh else ""}</td>'
        f'<td class="tk">{_esc(m.ticker)}</td>'
        f"<td>{_esc(name)}</td>"
        f'<td class="num {sign}" data-v="{m.pct_move}">{m.pct_move:.2f}%</td>'
        f'<td class="num" data-v="{m.tick_move}">{m.tick_move:.0f}</td>'
        f'<td class="num" data-v="{m.close}">{m.close:.4f}</td>'
        f'<td class="num" data-v="{m.avg_dollar_volume}">'
        f"{volatility._fmt_money(m.avg_dollar_volume)}</td>"
        f'<td class="note">{_esc(", ".join(flags))}</td></tr>'
    )


def _list_html(
    sl: volatility.ScreenList, digest: Digest, is_open: bool
) -> str:
    fresh = digest.new_tickers.get(_key(sl), set())
    rows = sl.rows[:ROWS_PER_LIST]
    head = rows[0] if rows else None
    peek = (
        f'<span class="peek">{_esc(head.ticker)} '
        f'<span class="{"up" if head.pct_move > 0 else "down"}">'
        f"{head.pct_move:+.2f}%</span></span>"
        if head else '<span class="peek empty">empty</span>'
    )
    body = "".join(_row_html(m, digest, fresh) for m in rows)
    return f"""
      <details class="list" {"open" if is_open else ""}
               data-sec="{_esc(sl.security_type)}" data-band="{_esc(sl.band)}"
               data-dir="{_esc(sl.direction)}" data-liq="{_esc(sl.liquidity)}">
        <summary>
          <span class="ltitle">{_esc(sl.title)}</span>
          <span class="count">{len(sl.rows)}</span>
          {peek}
        </summary>
        <table class="rows">
          <thead><tr><th></th><th data-s="t">ticker</th><th>company</th>
            <th data-s="n" class="num">pct</th><th data-s="n" class="num">ticks</th>
            <th data-s="n" class="num">close</th><th data-s="n" class="num">adv</th>
            <th></th></tr></thead>
          <tbody>{body}</tbody>
        </table>
      </details>"""


def screens_html(digest: Digest) -> str:
    lists = [sl for sl in digest.screen.lists if sl.rows]
    if not lists:
        return '<p class="empty">No moves. Run <code>mr prices</code>.</p>'
    open_keys = default_expanded(lists)
    s = digest.screen

    # Shared with the digest and `mr screens` -- see volatility.caveats.
    notes = list(volatility.caveats(s))
    if not digest.prior_day:
        notes.append("no prior session, so NEW is unavailable")

    bands = sorted({sl.band for sl in lists})
    secs = sorted({sl.security_type for sl in lists})
    chips = "".join(
        f'<button class="f" data-f="band" data-v="{_esc(b)}">{_esc(b)}</button>'
        for b in bands
    ) + "".join(
        f'<button class="f" data-f="sec" data-v="{_esc(x)}">{_esc(x)}s</button>'
        for x in secs
    ) + '<button class="f" data-f="liq" data-v="liquid">&gt;$5M ADV</button>'

    return f"""
      <p class="note">{s.moves_screened:,} moves screened for
        {s.day.isoformat()}.</p>
      {_notes_html(notes)}
      <div class="filters">{chips}
        <button class="f reset" data-f="reset">all</button></div>
      {''.join(_list_html(sl, digest, _key(sl) in open_keys) for sl in lists)}"""


#: Sort and filter only. Kept small and inline on purpose: the file has to
#: stay one file that opens from disk with no server and no build step.
SCRIPT: Final[str] = """
(function () {
  var on = {};
  function apply() {
    document.querySelectorAll('details.list').forEach(function (d) {
      var ok = Object.keys(on).every(function (k) { return d.dataset[k] === on[k]; });
      d.hidden = !ok;
    });
  }
  document.querySelectorAll('button.f').forEach(function (b) {
    b.addEventListener('click', function () {
      if (b.dataset.f === 'reset') { on = {}; }
      else if (on[b.dataset.f] === b.dataset.v) { delete on[b.dataset.f]; }
      else { on[b.dataset.f] = b.dataset.v; }
      document.querySelectorAll('button.f').forEach(function (o) {
        o.classList.toggle('sel', on[o.dataset.f] === o.dataset.v);
      });
      apply();
    });
  });
  document.querySelectorAll('table.rows thead th[data-s]').forEach(function (th) {
    th.addEventListener('click', function () {
      var table = th.closest('table');
      var i = Array.prototype.indexOf.call(th.parentNode.children, th);
      var numeric = th.dataset.s === 'n';
      var body = table.tBodies[0];
      var rows = Array.prototype.slice.call(body.rows);
      var desc = th.dataset.dir !== 'desc';
      rows.sort(function (a, b) {
        var x = a.cells[i], y = b.cells[i];
        if (numeric) {
          var av = parseFloat(x.dataset.v || 0), bv = parseFloat(y.dataset.v || 0);
          return desc ? bv - av : av - bv;
        }
        return desc ? y.textContent.localeCompare(x.textContent)
                    : x.textContent.localeCompare(y.textContent);
      });
      th.dataset.dir = desc ? 'desc' : 'asc';
      rows.forEach(function (r) { body.appendChild(r); });
    });
  });
})();
"""


# --- U5: the filing feed ------------------------------------------------


def filings_html(rows: list[dict[str, Any]]) -> str:
    """Recent filings of the seven watched form types, newest first."""
    if not rows:
        return ('<p class="empty">No filings stored. Run <code>mr edgar</code>.'
                "</p>")
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["form_type"]] = counts.get(r["form_type"], 0) + 1
    chips = "".join(
        f'<button class="f" data-f="form" data-v="{_esc(k)}">{_esc(k)} '
        f'<span class="count">{v}</span></button>'
        for k, v in sorted(counts.items(), key=lambda kv: -kv[1])
    )
    body = "".join(
        f'<tr data-form="{_esc(r["form_type"])}">'
        f'<td class="tk">{_esc(r["form_type"])}</td>'
        f'<td>{_esc(r["company"])}</td>'
        f'<td class="note">{_esc(r["cik"] or "")}</td>'
        f'<td class="note">{_esc(str(r["filed_at"])[:16])}</td>'
        f'<td class="note">{_esc(r["accession"])}</td></tr>'
        for r in rows
    )
    return f"""
      <div class="filters">{chips}
        <button class="f reset" data-f="formreset">all</button></div>
      <table class="rows" id="filing-rows">
        <thead><tr><th data-s="t">form</th><th data-s="t">company</th>
          <th>cik</th><th data-s="t">filed</th><th>accession</th></tr></thead>
        <tbody>{body}</tbody>
      </table>"""


# --- U6: Form 4 clusters ------------------------------------------------


def clusters_html(rows: list[dict[str, Any]], role: str, floor: int) -> str:
    """One role's clusters, with the dollar floor adjustable in the page.

    The floor lives here rather than only in the CLI because it is a
    hypothesis: one week of data suggested $50k and $1M, and the point of
    making it a parameter was to move it after a month of reading. A number
    you can only change by re-running a 14-minute fetch is not adjustable.

    Rows are stored unfiltered, so lowering the floor reveals rather than
    re-queries.
    """
    mine = [r for r in rows if r.get("role") == role]
    if not mine:
        return ('<p class="empty">No clusters stored. Run '
                "<code>mr form4</code>.</p>")
    mine.sort(key=lambda r: float(r.get("value") or 0), reverse=True)

    body = []
    for r in mine:
        value = float(r.get("value") or 0)
        marks = []
        if r.get("planned_buys"):
            marks.append(f'<span class="mk">{r["planned_buys"]} planned</span>')
        if r.get("fund_like"):
            marks.append(
                f'<span class="mk fund" title="{_esc(r.get("fund_why",""))}">FUND</span>'
            )
        window = r["first"] if r["first"] == r["last"] else f'{r["first"]} to {r["last"]}'
        body.append(
            f'<tr class="cl" data-v="{value:.0f}" data-fund="{int(bool(r.get("fund_like")))}">'
            f'<td class="tk">{_esc(r.get("symbol") or "-")}</td>'
            f'<td class="note">{_esc(r["issuer_cik"])}</td>'
            f'<td>{_esc(r.get("issuer_name") or "")[:34]}</td>'
            f'<td class="note">{_esc(window)}</td>'
            f'<td class="num">{r["n_buyers"]}</td>'
            f'<td class="num" data-v="{value:.0f}">${value:,.0f}</td>'
            f'<td class="note">{"".join(marks)}</td></tr>'
            f'<tr class="cl-who" data-parent="{_esc(r["issuer_cik"])}">'
            f'<td></td><td colspan="6" class="note">'
            f'{_esc(", ".join(r.get("buyers", []))[:120])}</td></tr>'
        )
    return f"""
      <div class="filters cl-controls">
        <label class="flr">floor $<input type="number" class="cl-floor"
          data-role="{_esc(role)}" value="{floor}" step="10000" min="0"></label>
        <button class="f" data-f="fund" data-role="{_esc(role)}">hide FUND</button>
        <span class="note cl-count" data-role="{_esc(role)}"></span>
      </div>
      <table class="rows cl-table" data-role="{_esc(role)}">
        <thead><tr><th data-s="t">sym</th><th>cik</th><th data-s="t">issuer</th>
          <th>window</th><th data-s="n" class="num">buyers</th>
          <th data-s="n" class="num">value</th><th>flags</th></tr></thead>
        <tbody>{''.join(body)}</tbody>
      </table>"""


#: Filing-feed filtering and the cluster floor. Kept with the other inline
#: script: one file, no build step.
FEED_SCRIPT: Final[str] = """
(function () {
  var form = null;
  document.querySelectorAll('button.f[data-f="form"]').forEach(function (b) {
    b.addEventListener('click', function () {
      form = (form === b.dataset.v) ? null : b.dataset.v;
      document.querySelectorAll('button.f[data-f="form"]').forEach(function (o) {
        o.classList.toggle('sel', o.dataset.v === form);
      });
      document.querySelectorAll('#filing-rows tbody tr').forEach(function (r) {
        r.hidden = !!form && r.dataset.form !== form;
      });
    });
  });
  var fr = document.querySelector('button.f[data-f="formreset"]');
  if (fr) fr.addEventListener('click', function () {
    form = null;
    document.querySelectorAll('button.f[data-f="form"]').forEach(function (o) {
      o.classList.remove('sel'); });
    document.querySelectorAll('#filing-rows tbody tr').forEach(function (r) {
      r.hidden = false; });
  });

  var hideFund = {};
  function applyClusters(role) {
    var table = document.querySelector('.cl-table[data-role="' + role + '"]');
    var input = document.querySelector('.cl-floor[data-role="' + role + '"]');
    if (!table || !input) return;
    var floor = parseFloat(input.value || 0), shown = 0, total = 0;
    table.querySelectorAll('tbody tr.cl').forEach(function (r) {
      total++;
      var ok = parseFloat(r.dataset.v) >= floor &&
               !(hideFund[role] && r.dataset.fund === '1');
      r.hidden = !ok;
      var who = r.nextElementSibling;
      if (who && who.classList.contains('cl-who')) who.hidden = !ok;
      if (ok) shown++;
    });
    var c = document.querySelector('.cl-count[data-role="' + role + '"]');
    if (c) c.textContent = shown + ' of ' + total + ' clusters';
  }
  document.querySelectorAll('.cl-floor').forEach(function (i) {
    i.addEventListener('input', function () { applyClusters(i.dataset.role); });
    applyClusters(i.dataset.role);
  });
  document.querySelectorAll('button.f[data-f="fund"]').forEach(function (b) {
    b.addEventListener('click', function () {
      var role = b.dataset.role;
      hideFund[role] = !hideFund[role];
      b.classList.toggle('sel', hideFund[role]);
      b.textContent = hideFund[role] ? 'show FUND' : 'hide FUND';
      applyClusters(role);
    });
  });
})();
"""
