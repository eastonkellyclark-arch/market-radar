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

    caveats = []
    if s.floor_excluded:
        caveats.append(f"{s.floor_excluded:,} below the ${s.sanity_floor} floor")
    if s.gap_excluded:
        caveats.append(f"{s.gap_excluded:,} spanning a >30d gap")
    if not digest.prior_day:
        caveats.append("no prior session, so NEW is unavailable")

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
        {s.day.isoformat()}{('; ' + '; '.join(caveats)) if caveats else ''}.
        Liquidity gating is not yet trustworthy -- see the Liquidity gate panel.</p>
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
