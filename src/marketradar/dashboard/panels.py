"""Panel bodies: health, macro, and the twenty-four screen lists.

Built from :func:`marketradar.digest.build`, deliberately. The digest and the
dashboard show the same screens, and giving them separate readers would give
them two chances to disagree about what today's moves were.

**The expansion policy is one function.** Twenty-four lists is more than
anyone scans over coffee, so most open collapsed behind a header carrying the
top row and a count. Which tab opens is :func:`default_tab` and nothing
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


def default_tab(
    lists: list[volatility.ScreenList],
) -> tuple[str, str]:
    """Which (security type, price band) tab opens on load.

    Was ``default_expanded``, which chose which of 24 collapsibles started
    open. The collapsibles are gone and the question survived intact: stock
    $10+ is the pair always worth reading, *unless* another band actually
    moved today -- so an unusual day in the sub-$1 band is not hidden behind a
    tab on the one morning it matters, which is the same reason it was not
    hidden behind a header before.

    A hypothesis, not a conclusion. Change this function; nothing else needs
    to know.
    """
    default = ("stock", "$10+")
    # The loudest list, chosen from the liquid ones so the default view does
    # not open on something untradeable. Every other tab is one click away, so
    # this is about what opens, not about what is reachable.
    populated = [sl for sl in lists if sl.rows and sl.liquidity == "liquid"]
    if not populated:
        populated = [sl for sl in lists if sl.rows]
    if not populated:
        return default
    loudest = max(populated, key=lambda sl: max(abs(m.pct_move) for m in sl.rows))
    # Only defer to it when it beats what $10+ did; otherwise the default
    # stands and the sub-$1 band does not win every quiet day by construction.
    baseline = [sl for sl in populated
                if (sl.security_type, sl.band) == default]
    if baseline:
        best = max(max(abs(m.pct_move) for m in sl.rows) for sl in baseline)
        if max(abs(m.pct_move) for m in loudest.rows) <= best:
            return default
    return (loudest.security_type, loudest.band)


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
    # A div, not <details>. Inside a tab the list is already the thing you
    # asked for, and a collapsible that is always open is a row of chrome.
    return f"""
      <div class="list" data-sec="{_esc(sl.security_type)}"
           data-band="{_esc(sl.band)}" data-dir="{_esc(sl.direction)}"
           data-liq="{_esc(sl.liquidity)}" data-n="{len(sl.rows)}">
        <header class="lhead">
          <span class="ltitle">{_esc(sl.title)}</span>
          <span class="count">{len(sl.rows)}</span>
          {peek}
        </header>
        <table class="rows">
          <thead><tr><th></th><th data-s="t">ticker</th><th>company</th>
            <th data-s="n" class="num">pct</th><th data-s="n" class="num">ticks</th>
            <th data-s="n" class="num">close</th><th data-s="n" class="num">adv</th>
            <th></th></tr></thead>
          <tbody>{body}</tbody>
        </table>
      </div>"""


def screens_html(digest: Digest) -> str:
    """The 24 lists as two tab axes, with gainers and losers side by side.

    Twenty-four stacked collapsibles did not survive contact with fourteen
    panels: finding one list meant scrolling past twenty-three, and the
    default-expanded heuristic was a guess about which three mattered today.

    The axes are the two that are mutually exclusive -- a list is either
    stocks or ETFs, and it is in exactly one price band. Direction is *not* an
    axis, because gainers and losers are read together: a name at the top of
    one and the bottom of the other is the interesting case, and separating
    them hides it. The $5M ADV gate is a toggle rather than a third axis for
    the same reason the bands are kept apart in the first place -- it changes
    which names qualify, not which question is being asked.
    """
    lists = [sl for sl in digest.screen.lists if sl.rows]
    if not lists:
        return '<p class="empty">No moves. Run <code>mr prices</code>.</p>'
    s = digest.screen

    # Shared with the digest and `mr screens` -- see volatility.caveats.
    notes = list(volatility.caveats(s))
    if not digest.prior_day:
        notes.append("no prior session, so NEW is unavailable")

    bands = sorted({sl.band for sl in lists}, key=_band_order)
    secs = sorted({sl.security_type for sl in lists})
    has_gated = any(sl.liquidity == "liquid" for sl in lists)
    open_sec, open_band = default_tab(lists)
    if open_sec not in secs:
        open_sec = secs[0]
    if open_band not in bands:
        open_band = bands[0]

    def count(sec: str, band: str) -> int:
        return sum(len(sl.rows) for sl in lists
                   if sl.security_type == sec and sl.band == band)

    # `open_sec`/`open_band`, not `secs[0]`/`bands[0]`. The first version
    # marked the alphabetically-first tab current and left default_tab's
    # answer on the floor -- so the panel opened on etfs/$1-10, a combination
    # that is routinely empty, and the policy function was dead code that
    # every test of it still passed.
    sec_tabs = "".join(
        f'<button class="tab" data-axis="sec" data-v="{_esc(x)}"'
        f'{" aria-current=\"true\"" if x == open_sec else ""}>'
        f'{_esc(x)}s</button>' for x in secs)
    band_tabs = "".join(
        f'<button class="tab" data-axis="band" data-v="{_esc(b)}"'
        f'{" aria-current=\"true\"" if b == open_band else ""}>'
        f'{_esc(b)}<span class="count">{count(open_sec, b):,}</span></button>'
        for b in bands)
    gate = (
        '<label class="toggle"><input type="checkbox" id="gate"> '
        '&gt;$5M ADV only<span class="note"> &mdash; the liquidity gate, '
        'applied in place</span></label>' if has_gated else "")

    # Every list is rendered; the script shows the two that match. Open by
    # default and not a <details>: inside a tab there is nothing to collapse.
    blocks = "".join(_list_html(sl, digest, True) for sl in lists)
    return f"""
      <p class="note">{s.moves_screened:,} moves screened for
        {s.day.isoformat()}. Gainers and losers are shown together because a
        name near the top of one and the bottom of the other is the case
        worth seeing.</p>
      {_notes_html(notes)}
      <div class="tabs" data-axis-row="sec">{sec_tabs}</div>
      <div class="tabs" data-axis-row="band">{band_tabs}</div>
      {gate}
      <div class="pair" id="pair">{blocks}</div>
      <p class="empty" id="pair-empty" hidden>No list for that combination.</p>"""


#: Bands sort by price, not alphabetically: "$1-10" before "$10+" before
#: "sub-$1" is nobody's reading order.
_BAND_RANK: Final[dict[str, int]] = {"sub-$1": 0, "$1-10": 1, "$10+": 2}


def _band_order(band: str) -> tuple[int, str]:
    return (_BAND_RANK.get(band, 99), band)


#: Sort and filter only. Kept small and inline on purpose: the file has to
#: stay one file that opens from disk with no server and no build step.
SCRIPT: Final[str] = """
/* Registered as a binder rather than run once at load.

   With one panel in the DOM at a time, a script that queried `document` on
   DOMContentLoaded bound to elements that had not been injected yet and
   silently did nothing. Each binder takes the freshly injected root and is
   re-run after every panel switch; the old elements are gone with the old
   innerHTML, so re-binding cannot double up. */
(window.__MR_BINDERS__ = window.__MR_BINDERS__ || []).push(
  function (root) {
  /* Tabs. Two axes, both mutually exclusive, so the state is two strings
     rather than a set of filters. Gainers and losers are deliberately not an
     axis: both matching lists are shown together. */
  var tabs = root.querySelectorAll('.tabs');
  if (tabs.length) {
    var pick = {};
    root.querySelectorAll('.tabs .tab[aria-current]').forEach(function (b) {
      pick[b.dataset.axis] = b.dataset.v;
    });
    var gate = root.querySelector('#gate');
    var empty = root.querySelector('#pair-empty');
    function apply() {
      var shown = 0;
      root.querySelectorAll('div.list').forEach(function (d) {
        var ok = d.dataset.sec === pick.sec && d.dataset.band === pick.band;
        if (ok && gate && gate.checked) { ok = d.dataset.liq === 'liquid'; }
        else if (ok && gate && !gate.checked) { ok = d.dataset.liq !== 'liquid'; }
        d.hidden = !ok;
        if (ok) { shown++; }
      });
      if (empty) { empty.hidden = shown > 0; }
      /* Per-tab counts follow the chosen security type, so the number on a
         band tab is the number you get when you press it. */
      root.querySelectorAll('.tabs .tab[data-axis="band"]').forEach(function (b) {
        var c = b.querySelector('.count');
        if (!c) { return; }
        var n = 0;
        root.querySelectorAll('div.list').forEach(function (d) {
          if (d.dataset.sec === pick.sec && d.dataset.band === b.dataset.v) {
            n += parseInt(d.dataset.n || '0', 10);
          }
        });
        c.textContent = n.toLocaleString();
      });
    }
    root.querySelectorAll('.tabs .tab').forEach(function (b) {
      b.addEventListener('click', function () {
        pick[b.dataset.axis] = b.dataset.v;
        root.querySelectorAll('.tabs .tab[data-axis="' + b.dataset.axis + '"]')
          .forEach(function (o) { o.removeAttribute('aria-current'); });
        b.setAttribute('aria-current', 'true');
        apply();
      });
    });
    if (gate) { gate.addEventListener('change', apply); }
    apply();
  }
  root.querySelectorAll('table.rows thead th[data-s]').forEach(function (th) {
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
});
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
/* Registered as a binder rather than run once at load.

   With one panel in the DOM at a time, a script that queried `document` on
   DOMContentLoaded bound to elements that had not been injected yet and
   silently did nothing. Each binder takes the freshly injected root and is
   re-run after every panel switch; the old elements are gone with the old
   innerHTML, so re-binding cannot double up. */
(window.__MR_BINDERS__ = window.__MR_BINDERS__ || []).push(
  function (root) {
  var form = null;
  root.querySelectorAll('button.f[data-f="form"]').forEach(function (b) {
    b.addEventListener('click', function () {
      form = (form === b.dataset.v) ? null : b.dataset.v;
      root.querySelectorAll('button.f[data-f="form"]').forEach(function (o) {
        o.classList.toggle('sel', o.dataset.v === form);
      });
      root.querySelectorAll('#filing-rows tbody tr').forEach(function (r) {
        r.hidden = !!form && r.dataset.form !== form;
      });
    });
  });
  var fr = document.querySelector('button.f[data-f="formreset"]');
  if (fr) fr.addEventListener('click', function () {
    form = null;
    root.querySelectorAll('button.f[data-f="form"]').forEach(function (o) {
      o.classList.remove('sel'); });
    root.querySelectorAll('#filing-rows tbody tr').forEach(function (r) {
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
  root.querySelectorAll('.cl-floor').forEach(function (i) {
    i.addEventListener('input', function () { applyClusters(i.dataset.role); });
    applyClusters(i.dataset.role);
  });
  root.querySelectorAll('button.f[data-f="fund"]').forEach(function (b) {
    b.addEventListener('click', function () {
      var role = b.dataset.role;
      hideFund[role] = !hideFund[role];
      b.classList.toggle('sel', hideFund[role]);
      b.textContent = hideFund[role] ? 'show FUND' : 'hide FUND';
      applyClusters(role);
    });
  });
});
"""


# --- W3-T3: 8-K deals ---------------------------------------------------

#: How the value states are worded on screen. Spelled out rather than shown
#: as a code, because the entire point of the reason codes is that a reader
#: never has to guess what a blank means.
_VALUE_WORDS: Final[dict[str, str]] = {
    "stated_8k": "in the 8-K",
    "stated_exhibit": "in an exhibit",
    "not_stated": "not stated",
    "not_parsed": "unparsed",
}
_FIN_WORDS: Final[dict[str, str]] = {
    "rule_305_promised": "Rule 3-05 promised",
    "figures_in_filing": "figures given",
    "none_disclosed": "none",
}


def _money(raw: str | None) -> str:
    """A stated value, compactly. Empty when there is none.

    Never a dash and never a zero: an absent price is the common case here,
    and the caller prints the reason in its place.
    """
    if raw in (None, ""):
        return ""
    value = Decimal(raw)
    for cut, suffix in ((Decimal("1e9"), "B"), (Decimal("1e6"), "M"),
                        (Decimal("1e3"), "K")):
        if value >= cut:
            return f"${value / cut:,.2f}{suffix}"
    return f"${value:,.0f}"


def deals_html(rows: list[dict[str, Any]]) -> str:
    """Deal candidates from 8-K Items 1.01 and 2.01.

    **This is a candidate list, not a deal list.** Item 1.01 is "Entry into a
    Material Definitive Agreement" and only about 16% of it is M&A -- the rest
    is credit facilities, equity raises and supply contracts. So each row
    shows both classifiers rather than a verdict: whether an EX-2.x exhibit is
    attached, which is the filer's own Reg S-K 601(b)(2) classification, and
    what the prose names. Rows where the two disagree carry a REVIEW mark and
    can be isolated with one click -- that disagreement is the only honest
    signal available about which rows to distrust.

    SPAC combinations filter separately. A quarter of the set is a de-SPAC,
    which has no operating acquirer and no computable multiple; reading it
    alongside operating deals is what makes an average meaningless.
    """
    if not rows:
        return ('<p class="empty">No deal candidates stored. Run '
                "<code>mr deals</code>.</p>")

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["deal_type"]] = counts.get(r["deal_type"], 0) + 1
    review = sum(1 for r in rows if not r["agree"])
    priced = sum(1 for r in rows if r["value_usd"])

    chips = "".join(
        f'<button class="f" data-f="dtype" data-v="{_esc(k)}">{_esc(k)} '
        f'<span class="count">{v}</span></button>'
        for k, v in sorted(counts.items(), key=lambda kv: -kv[1])
    )

    body = []
    for r in rows:
        marks = []
        if not r["agree"]:
            marks.append('<span class="mk rev" title="the EX-2.x exhibit and '
                         'the prose disagree">REVIEW</span>')
        if r["target_financials"] == "rule_305_promised":
            marks.append('<span class="mk fin" title="audited target '
                         'financials due by amendment">3-05</span>')
        value = _money(r["value_usd"])
        basis = _VALUE_WORDS.get(r["value_basis"], r["value_basis"])
        who = r.get("counterparty") or ""
        cell = value or f'<span class="note">{_esc(basis)}</span>'
        why = (
            "exhibit EX-2.x: <strong>"
            + ("yes" if r["exhibit_signal"] else "no")
            + "</strong> &middot; text reads: <strong>"
            + _esc(r["text_signal"] or "ambiguous")
            + "</strong> &middot; value " + _esc(basis)
            + (f' ({_esc(r["value_text"])})' if r.get("value_text") else "")
            + " &middot; target financials "
            + _esc(_FIN_WORDS.get(r["target_financials"], "?"))
            + " &middot; filer is "
            + _esc(r["filer_role"].replace("_", " "))
        )
        body.append(
            f'<tr class="dl" data-dtype="{_esc(r["deal_type"])}" '
            f'data-review="{int(not r["agree"])}">'
            f'<td class="note">{_esc(r["filed"])}</td>'
            f'<td class="tk">{_esc(r["company"][:30])}</td>'
            f'<td class="note">{_esc(r["deal_type"])}</td>'
            f'<td class="note">{_esc(r["items"])}</td>'
            f'<td class="num" data-v="{_esc(r["value_usd"] or 0)}">{cell}</td>'
            f'<td class="note">{_esc(r["consideration"].replace("_", " "))}</td>'
            f'<td class="note">{_esc(who[:34])}</td>'
            f'<td class="note">{"".join(marks)}</td></tr>'
            f'<tr class="dl-why" data-dtype="{_esc(r["deal_type"])}" '
            f'data-review="{int(not r["agree"])}">'
            f'<td></td><td colspan="7" class="note">{why}</td></tr>'
        )

    return f"""
      <p class="note">Item 1.01 covers every material contract a registrant
        signs and measured <strong>~16% M&amp;A</strong> over a full week, so
        these are candidates rather than deals. {review} of {len(rows)} have
        the exhibit and the prose disagreeing; {priced} of {len(rows)} state a
        value at all.</p>
      <div class="filters">{chips}
        <button class="f" data-f="dreview">review only
          <span class="count">{review}</span></button>
        <button class="f reset" data-f="dreset">all</button>
        <span class="note dl-count"></span></div>
      <table class="rows" id="deal-rows">
        <thead><tr><th data-s="t">filed</th><th data-s="t">company</th>
          <th data-s="t">type</th><th>items</th>
          <th data-s="n" class="num">value</th><th>consideration</th>
          <th data-s="t">counterparty</th><th>flags</th></tr></thead>
        <tbody>{''.join(body)}</tbody>
      </table>"""


#: Deal-panel filtering. Kept with the other inline script: one file, no
#: build step.
DEALS_SCRIPT: Final[str] = """
/* Registered as a binder rather than run once at load.

   With one panel in the DOM at a time, a script that queried `document` on
   DOMContentLoaded bound to elements that had not been injected yet and
   silently did nothing. Each binder takes the freshly injected root and is
   re-run after every panel switch; the old elements are gone with the old
   innerHTML, so re-binding cannot double up. */
(window.__MR_BINDERS__ = window.__MR_BINDERS__ || []).push(
  function (root) {
  var table = document.getElementById('deal-rows');
  if (!table) { return; }
  var dtype = null, reviewOnly = false;
  function apply() {
    var shown = 0, total = 0;
    table.querySelectorAll('tbody tr').forEach(function (r) {
      var ok = (!dtype || r.dataset.dtype === dtype) &&
               (!reviewOnly || r.dataset.review === '1');
      r.hidden = !ok;
      if (r.classList.contains('dl')) {
        total++;
        if (ok) { shown++; }
      }
    });
    var c = document.querySelector('.dl-count');
    if (c) { c.textContent = shown + ' of ' + total + ' candidates'; }
  }
  root.querySelectorAll('button.f[data-f="dtype"]').forEach(function (b) {
    b.addEventListener('click', function () {
      dtype = (dtype === b.dataset.v) ? null : b.dataset.v;
      root.querySelectorAll('button.f[data-f="dtype"]').forEach(function (o) {
        o.classList.toggle('sel', o.dataset.v === dtype);
      });
      apply();
    });
  });
  var rv = document.querySelector('button.f[data-f="dreview"]');
  if (rv) {
    rv.addEventListener('click', function () {
      reviewOnly = !reviewOnly;
      rv.classList.toggle('sel', reviewOnly);
      apply();
    });
  }
  var rs = document.querySelector('button.f[data-f="dreset"]');
  if (rs) {
    rs.addEventListener('click', function () {
      dtype = null;
      reviewOnly = false;
      root.querySelectorAll('button.f').forEach(function (o) {
        o.classList.remove('sel');
      });
      apply();
    });
  }
  apply();
});
"""


# --- Historical outcomes ------------------------------------------------

#: Diverging pair for the excess-return bars: one hue each side of a neutral
#: zero line, never a rainbow and never the status palette, which is reserved
#: for good/warning/serious/critical. The number is printed beside every bar,
#: so colour is a second encoding rather than the only one.
_UP: Final[str] = "#2b6cb0"
_DOWN: Final[str] = "#b45309"

#: Widest bar, as a fraction. Excess returns here live inside a few percent;
#: scaling to the maximum observed value would make noise look like signal.
_BAR_FULL: Final[float] = 0.10


def _bar(value: float | None) -> str:
    """A diverging bar from a centre line. Empty when there is no number."""
    if value is None:
        return ""
    frac = max(-1.0, min(1.0, value / _BAR_FULL))
    width = abs(frac) * 50.0
    left = 50.0 if frac >= 0 else 50.0 - width
    hue = _UP if frac >= 0 else _DOWN
    return (
        '<span class="oc-bar" aria-hidden="true">'
        f'<span class="oc-fill" style="left:{left:.1f}%;width:{width:.1f}%;'
        f'background:{hue}"></span></span>'
    )


#: One sentence, shared with the CLI and the spec so the three cannot drift.
_SURVIVOR: Final[str] = (
    "Biased downward: a completed deal delists the target and leaves the "
    "sample; a collapsed one keeps trading and stays. The survivors are "
    "weighted toward deals that failed, so the true figure is higher."
)


def outcomes_html(rows: list[dict[str, Any]]) -> str:
    """Forward returns after an event, by slice and horizon.

    **The median leads and the mean sits beside it.** Event-study return
    distributions are not normal: one 900% takeout moves the mean of ten
    thousand events and says nothing about the next one. A large gap between
    the two columns is itself the finding.

    Two numbers here are easy to skip and should not be. ``priced`` is how
    much of the population produced any return at all -- a delisted target
    has no thirty-session close, and the deal closing is exactly why its
    history ends, so the drops correlate with the outcome. ``drop`` counts
    events excluded as suspected unrecorded splits, which is a statement
    about our corporate-actions coverage rather than about the market.
    """
    if not rows:
        return ('<p class="empty">No outcome study stored. Run '
                "<code>mr outcomes</code>.</p>")

    studies: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        studies.setdefault(r["study"], []).append(r)

    def num(v: Any) -> float | None:
        return None if v in (None, "") else float(v)

    def pct(v: float | None) -> str:
        return '<span class="note">-</span>' if v is None else f"{v * 100:+.2f}%"

    blocks = []
    for study, group in sorted(studies.items()):
        head = group[0]
        events, priced = head.get("events") or 0, head.get("priced") or 0
        share = f"{priced / events * 100:.0f}%" if events else "-"
        body = []
        for r in sorted(group, key=lambda r: (r["slice"] != "all",
                                              r["slice"], r["horizon"])):
            ex = num(r["median_excess"])
            mean_ex = num(r["mean_excess"])
            win = num(r["win_rate"])
            win_cell = '<span class="note">-</span>' if win is None \
                else f"{win * 100:.0f}%"
            emphasis = ' class="oc-all"' if r["slice"] == "all" else ""
            body.append(
                f"<tr{emphasis}>"
                f'<td class="tk">{_esc(r["slice"])}</td>'
                f'<td class="num">+{int(r["horizon"])}d</td>'
                f'<td class="num">{int(r["n"]):,}</td>'
                f'<td class="num">{pct(num(r["median_ret"]))}</td>'
                f'<td class="num">{pct(ex)}{_bar(ex)}</td>'
                f'<td class="num">{pct(mean_ex)}</td>'
                f'<td class="num">{win_cell}</td>'
                f'<td class="num">{pct(num(r["median_run_up"]))}</td>'
                f'<td class="num note">{int(r["n_suspect"])}</td></tr>'
            )
        blocks.append(f"""
          <h5>{_esc(study)}</h5>
          <p class="note">{events:,} events, {priced:,} priced ({share}).
            Benchmark {_esc(head.get("benchmark") or "SPY")}; horizons are
            trading sessions from the last close before the event, so
            <strong>+1d is the event session itself</strong>.</p>
          <p class="note bias-note"><strong>Every excess figure below is
            biased downward.</strong> The {events - priced:,} events that
            produced no return did not drop out at random &mdash; completing
            an acquisition delists the target, so it has no forward close and
            leaves the sample, while a deal that collapsed keeps trading and
            stays in it. What survives to be measured is weighted toward
            deals that <em>failed</em>, which is also the population that
            gives back the announcement move. The correction goes
            <strong>up</strong>, by an unknown amount.</p>
          <table class="rows">
            <thead><tr><th>slice</th><th class="num">h</th>
              <th class="num">n</th><th class="num">median</th>
              <th class="num" title="{_esc(_SURVIVOR)}">median excess
                <span class="bias">&darr;biased</span></th>
              <th class="num" title="{_esc(_SURVIVOR)}">mean excess
                <span class="bias">&darr;biased</span></th>
              <th class="num">win</th><th class="num">run-up</th>
              <th class="num">drop</th></tr></thead>
            <tbody>{''.join(body)}</tbody>
          </table>""")

    return f"""
      <p class="note">Pure SQL over eleven years of prices - no LLM, no
        embeddings - so this can precede the expensive machinery rather than
        justify it afterwards. <strong>run-up</strong> is the five sessions
        before the event: a signal that only appears after the move already
        happened is a different thing from one that precedes it.
        <strong>drop</strong> counts events excluded as suspected unrecorded
        splits, which measures our corporate-actions coverage, not the
        market.</p>
      {''.join(blocks)}"""


# --- Weekend 4: private companies and the review queue ------------------

#: NAICS codes seen often enough in the Form 5500 private population to be
#: worth naming on screen. Not a lookup table for all of NAICS -- just the
#: head of the distribution, so a code is readable without a second window.
NAICS_LABELS: Final[dict[str, str]] = {
    "541990": "professional / scientific / technical services",
    "621111": "offices of physicians",
    "621210": "offices of dentists",
    "541110": "offices of lawyers",
    "812990": "personal services",
    "541600": "management / consulting services",
    "238900": "specialty trade contractors",
    "813000": "religious / grantmaking / civic organizations",
    "611000": "educational services",
    "238220": "plumbing, heating and air-conditioning contractors",
    "561000": "administrative and support services",
    "722500": "restaurants",
    "236000": "construction of buildings",
    "524210": "insurance agencies and brokerages",
    "523900": "other financial investment activities",
}


#: How a trend reads in the panels. Colour never carries the meaning on its
#: own -- each pairs a glyph with a word, per the status rule.
TREND_MARK: Final[dict[str, tuple[str, str]]] = {
    "growing": ("\u2191", "growing"),
    "flat": ("\u2192", "flat"),
    "declining": ("\u2193", "declining"),
    "unknown": ("\u00b7", "one year"),
}


def trend_html(row: dict[str, Any]) -> str:
    """One sponsor's participant trend, with its absences spelled out.

    Three things share this cell and must not be confused for each other: the
    direction across the years the sponsor *filed*, the years it has not filed
    yet, and the years it skipped. A sponsor that stopped filing shows
    ``lapsed`` and no direction at all -- never a fall to zero, which is what
    a series that zero-filled absences would show and what a decline screen
    would rank first.
    """
    trend = row.get("trend") or "unknown"
    glyph, label = TREND_MARK.get(trend, TREND_MARK["unknown"])
    pct = row.get("pct_change")
    if row.get("status") == "lapsed":
        last = row.get("last_year")
        return (f'<span class="mk stale" title="last filed for plan year '
                f'{last}. Terminated, acquired, re-EIN&#39;d or below the '
                f'filing threshold -- not a headcount decline">'
                f'lapsed {_esc(last)}</span>')
    move = "" if pct is None else f" {pct:+.0%}"
    span = ""
    if (row.get("years_filed") or 0) >= 2:
        span = f" {row.get('first_year')}&ndash;{row.get('last_year')}"
    pending = int(row.get("pending_years") or 0)
    gaps = int(row.get("gap_years") or 0)
    added = int(row.get("plans_added") or 0)
    dropped = int(row.get("plans_dropped") or 0)
    extra = ""
    if pending:
        extra += (f'<span class="note" title="that plan year is still being '
                  f'filed, so the absence carries no information"> '
                  f'+{pending} pending</span>')
    if gaps:
        extra += (f'<span class="note" title="complete plan years the sponsor '
                  f'skipped between two it filed"> {gaps} gap</span>')
    if added or dropped:
        # The trend already excludes these plans. Shown because a sponsor
        # whose plan set churned is a weaker reading than one whose plans are
        # identical at both ends, even though neither is distorted by it.
        bits = ([f"+{added}"] if added else []) + ([f"-{dropped}"] if dropped
                                                   else [])
        extra += (f'<span class="note" title="plans present at only one end. '
                  f'Left out of the trend: a plan opening or closing is a '
                  f'fact about the filing, not about headcount">'
                  f' {"/".join(bits)} plans</span>')
    return (f'<span class="tr tr-{_esc(trend)}" title="measured only between '
            f'plan years the sponsor filed, and only complete ones">'
            f'{glyph} {label}{move}</span>'
            f'<span class="note">{span}</span>{extra}')


def series_bars(row: dict[str, Any]) -> str:
    """A bare inline sparkline of the filed years. No axis, no library.

    Years the sponsor did not file are *gaps* in the row rather than zeros:
    the shape of the series has to show the absence as an absence.
    """
    series = row.get("series") or []
    if len(series) < 2:
        return '<span class="note">&mdash;</span>'
    points = [(int(d["year"]), int(d["participants"] or 0)) for d in series]
    top = max(p for _, p in points) or 1
    years = range(min(y for y, _ in points), max(y for y, _ in points) + 1)
    have = dict(points)
    cells = []
    for y in years:
        if y not in have:
            cells.append('<i class="sp gap" title="no filing for '
                         f'{y}"></i>')
            continue
        h = max(2, round(have[y] / top * 14))
        cells.append(f'<i class="sp" style="height:{h}px" '
                     f'title="{y}: {have[y]:,}"></i>')
    return f'<span class="spark">{"".join(cells)}</span>'


def naics_label(code: str | None) -> str:
    if not code:
        return "unclassified"
    return NAICS_LABELS.get(code, code)


def private_html(rows: list[dict[str, Any]], stats: dict[str, Any]) -> str:
    """Private companies from Form 5500, by NAICS and headcount.

    **This panel's population is the 94.8% that matches nothing.** Sponsors
    that resolve to an SEC filer are public companies we already track from
    the other end; the ones with no match are the reason to read Form 5500 at
    all, so "unresolved" is the filter for the panel rather than a problem
    reported by it.

    Two headcount columns, not one. A sponsor with several plans counts the
    same people in each, so the sum across plans is an upper bound and the
    largest single plan is a lower one. Neither is the headcount, and showing
    only one would imply a precision this data does not have.

    DFE sponsors -- master trusts, collective investment funds, pooled
    separate accounts -- are filterable as their own category rather than
    dropped. They are trustees, not employers, and they dominate any
    plan-weighted view: sorted by plans the top of this list would be
    Transamerica Life and BNY Mellon.
    """
    if not rows:
        return ('<p class="empty">No Form 5500 sponsors loaded. Run '
                "<code>mr form5500</code>.</p>")

    by_naics: dict[str, int] = {}
    for r in rows:
        code = r.get("naics") or ""
        by_naics[code] = by_naics.get(code, 0) + 1
    chips = "".join(
        f'<button class="f" data-f="naics" data-v="{_esc(k)}" '
        f'title="{_esc(naics_label(k))}">{_esc(k or "none")} '
        f'<span class="count">{v:,}</span></button>'
        for k, v in sorted(by_naics.items(), key=lambda kv: -kv[1])[:12]
    )

    body = []
    for r in rows:
        marks = []
        if r.get("is_dfe"):
            marks.append(
                '<span class="mk fund" title="a trustee or pooled vehicle, '
                'not an employer">DFE</span>'
            )
        lo = int(r.get("participants_max") or 0)
        hi = int(r.get("participants_sum") or 0)
        head = f"{lo:,}" if lo == hi else f"{lo:,}&ndash;{hi:,}"
        body.append(
            f'<tr class="pv" data-naics="{_esc(r.get("naics") or "")}" '
            f'data-dfe="{int(bool(r.get("is_dfe")))}" '
            f'data-trend="{_esc(r.get("trend") or "")}" '
            f'data-head="{hi}">'
            f'<td class="tk">{_esc((r.get("sponsor_name") or "")[:38])}</td>'
            f'<td class="note">{_esc(r.get("state") or "")}</td>'
            f'<td class="note" title="{_esc(naics_label(r.get("naics")))}">'
            f'{_esc(r.get("naics") or "-")}</td>'
            f'<td class="num">{r.get("plans") or 0}</td>'
            f'<td class="num">{head}</td>'
            f'<td class="spark-cell">{series_bars(r)}</td>'
            f'<td class="trend">{trend_html(r)}</td>'
            f'<td class="note">{"".join(marks)}</td></tr>'
        )

    total = int(stats.get("sponsors") or 0)
    private = int(stats.get("private") or 0)
    dfe = int(stats.get("dfe") or 0)
    ambiguous = int(stats.get("ambiguous") or 0)
    year = stats.get("plan_year") or "?"
    note = stats.get("completeness") or ""
    return f"""
      <p class="note">Plan year {_esc(year)}. <strong>{private:,}</strong> of
        {total:,} sponsors match no SEC filer by any means &mdash; that
        population <em>is</em> the source, not a resolution failure. A
        further {ambiguous:,} have a name that matched while the EIN did not
        and sit in the review queue rather than being counted either way.
        {dfe:,} are Direct Filing Entities (trustees, flagged not dropped).
        {_esc(note)}</p>
      <p class="note">Headcount is a <strong>range</strong>: a sponsor with
        several plans counts the same people in each, so the largest single
        plan is a floor and the sum across plans a ceiling. The figure shown
        is <strong>total participants</strong> &mdash; which counts retirees
        and separated ex-employees still holding a balance, and runs about
        a quarter above the active count. The trend uses active participants
        instead, so the two columns are deliberately not the same measure.</p>
      <p class="note">The trend is measured <strong>only between plan years
        the sponsor actually filed</strong>, and only complete ones. A sponsor
        missing from a later year reads as <em>lapsed</em> or <em>pending</em>
        &mdash; never as a fall to zero. Filings lag the plan year by about
        eighteen months, so the newest year is thin for everyone and an
        absence there means nothing at all.</p>
      <div class="filters">{chips}
        <button class="f" data-f="dfeonly">DFE only</button>
        <button class="f reset" data-f="pvreset">all</button>
        <span class="note pv-count"></span></div>
      <table class="rows" id="private-rows">
        <thead><tr><th data-s="t">sponsor</th><th>state</th>
          <th data-s="t">naics</th><th data-s="n" class="num">plans</th>
          <th data-s="n" class="num">participants</th>
          <th>3y</th><th data-s="t">trend</th>
          <th>flags</th></tr></thead>
        <tbody>{''.join(body)}</tbody>
      </table>"""


#: How a nonprofit flag reads, and how strong its evidence is. Never a
#: silent exclusion: the panel says how many were set aside and on what.
_NP_MARK: Final[dict[str, tuple[str, str]]] = {
    "plan_type": ("NP", "sponsors a 403(b) -- only a 501(c)(3) or a public "
                        "school may, so this one is structural"),
    "both": ("NP", "sponsors a 403(b) and sits in a nonprofit-dense NAICS"),
    "naics": ("NP?", "in a nonprofit-dense NAICS. A guess -- this tier also "
                     "catches for-profit hospitals and trade schools"),
}


def _nonprofit_mark(row: dict[str, Any]) -> str:
    basis = row.get("nonprofit_basis")
    if not basis or basis not in _NP_MARK:
        return ""
    label, why = _NP_MARK[basis]
    return f'<span class="mk fund" title="{_esc(why)}">{label}</span>'


def mature_html(rows: list[dict[str, Any]], stats: dict[str, Any]) -> str:
    """Old private employers whose headcount has stopped growing.

    **Every column here is a bound and the panel says which direction it is
    wrong in.** Age is the effective date of the oldest plan the sponsor still
    files, so a 1971 company that started its 401(k) in 1985 reads as 1985:
    the screen can only ever understate age, which hides targets rather than
    inventing them. Headcount is a range for the same reason it is in the
    panel above. And a sponsor's absence from a later plan year is never
    counted as a decline -- lapsed sponsors are excluded outright, because
    "stopped filing" and "shrinking" are different facts and only one of them
    is what this list is for.

    Ordered by the age floor, oldest first -- the one input whose direction
    is not a judgement call. There is deliberately no composite score: the
    first version had one, every candidate in the top forty scored between
    0.992 and 0.999 because all three components saturated, and what looked
    like a ranking was a sort by headcount with three decimal places on it.
    """
    if not rows:
        return ('<p class="empty">No mature-target candidates. Build the '
                "series first: <code>mr form5500 --year 2022</code> (and "
                "2023, 2024), then <code>mr targets</code>.</p>")

    body = []
    for r in rows:
        # Active participants, matching what the screen filtered and ordered
        # on. Showing the total here instead put 474 in a column whose band
        # is 20 to 1,000 and whose CLI row said 96 -- the same sponsor, two
        # different measures, no label saying so.
        lo = int(r.get("active_last") or 0)
        hi = int(r.get("active_sum") or 0)
        head = f"{lo:,}" if lo == hi else f"{lo:,}&ndash;{hi:,}"
        age = r.get("age_years")
        eff = r.get("oldest_plan_eff")
        body.append(
            f'<tr class="mtg" data-naics="{_esc(r.get("naics") or "")}" '
            f'data-state="{_esc(r.get("state") or "")}" '
            f'data-trend="{_esc(r.get("trend") or "")}">'
            f'<td class="tk">{_esc((r.get("sponsor_name") or "")[:38])}</td>'
            f'<td class="note">{_esc(r.get("state") or "")}</td>'
            f'<td class="note" title="{_esc(naics_label(r.get("naics")))}">'
            f'{_esc(r.get("naics") or "-")}</td>'
            f'<td class="num" title="the oldest plan still filed is from '
            f'{_esc(eff)}. The company is at least this old and may be much '
            f'older -- a plan cannot predate the firm">'
            f'&ge;{0 if age is None else float(age):.0f}y</td>'
            f'<td class="num">{head}</td>'
            f'<td class="spark-cell">{series_bars(r)}</td>'
            f'<td class="trend">{trend_html(r)}{_nonprofit_mark(r)}</td></tr>'
        )

    pop = stats.get("population") or {}
    steps = " &rarr; ".join(
        f'{k.replace("_", " ")} {v:,}' for k, v in pop.items()) if pop else ""
    return f"""
      <p class="note">Old, still filing, not growing, and matching no SEC
        filer. <strong>{len(rows):,}</strong> candidates.
        {f"Population: {steps}." if steps else ""}</p>
      <p class="note"><strong>Age is a floor.</strong> It is the effective
        date of the oldest plan the sponsor still files &mdash; a company
        founded in 1971 whose plan started in 1985 reads as 1985. The column
        can only understate, so it hides targets rather than inventing them,
        and it ranks plan history rather than incorporation dates. The real
        number needs state SoS or UCC filings, which this project has not
        touched.</p>
      <p class="note"><strong>Nonprofits are set aside, not deleted.</strong>
        A college, a church or a museum clears every other filter here and
        cannot be bought. Two tiers of evidence: sponsoring a 403(b) is
        structural, since only a 501(c)(3) or a public school may, while a
        nonprofit-dense NAICS code is a guess that also catches for-profit
        hospitals and trade schools. Run
        <code>mr targets --nonprofits only</code> to read what was set
        aside.</p>
      <p class="note"><strong>A lapse is not a decline.</strong> Sponsors that
        stopped filing are excluded rather than ranked: terminated, acquired,
        re-EIN'd and below-threshold all look identical here, and none of them
        is the shrinking-headcount signal this list is for.</p>
      <table class="rows" id="mature-rows">
        <thead><tr><th data-s="t">sponsor</th><th>state</th><th data-s="t">naics</th>
          <th data-s="n" class="num">age</th>
          <th data-s="n" class="num" title="active participants: employees
            still accruing, not the total, which counts retirees and
            separated ex-employees holding a balance">active</th>
          <th>3y</th><th data-s="t">trend</th></tr></thead>
        <tbody>{''.join(body)}</tbody>
      </table>"""


def review_html(rows: list[dict[str, Any]], counts: dict[str, int]) -> str:
    """The entity review queue: name matched, EIN did not.

    **22,755 rows, not 800,000.** The original plan was to fuzzy match every
    sponsor name into a permanent queue. Measured: EIN is on 100% of filings,
    so a sponsor that is also an SEC filer resolves exactly and needs no
    review, and the 813,654 that match nothing are private companies with
    nothing to resolve against. What is left is the genuinely ambiguous
    residue -- a name that matched while the EIN disagreed.

    Ordered by how much the claim is worth. An exact name matching exactly one
    filer is a plausible subsidiary or rename; a normalized name matching
    eleven filers is noise. Sorting by candidate count puts the noise last
    instead of deleting it, and the ``candidates`` column shows why.

    There is deliberately no "confirm all above N% similarity" control.
    Normalized-name precision is 44.2% against EIN ground truth, so a bulk
    confirm would be wrong about half the time, invisibly.
    """
    if not rows:
        return ('<p class="empty">Review queue is empty. Run '
                "<code>mr form5500</code>.</p>")

    pending = counts.get("pending", 0)
    confirmed = counts.get("confirmed", 0)
    rejected = counts.get("rejected", 0)

    body = []
    for r in rows:
        cand = int(r.get("candidates") or 1)
        basis = r.get("match_basis") or ""
        strength = "exact" if basis == "exact_name" else "normalized"
        weak = ' class="mk rev"' if cand > 1 or basis == "normalized_name" else ""
        body.append(
            f'<tr class="rv" data-basis="{_esc(basis)}" '
            f'data-cand="{cand}">'
            f'<td class="tk">{_esc((r.get("sponsor_name") or "")[:34])}</td>'
            f'<td class="note">{_esc(r.get("ein") or "")}</td>'
            f'<td>{_esc((r.get("matched_name") or "")[:34])}</td>'
            f'<td class="note">{_esc(r.get("matched_cik") or "")}</td>'
            f'<td class="note"><span{weak}>{_esc(strength)}</span></td>'
            f'<td class="num">{cand}</td>'
            f'<td class="note">{_esc(r.get("naics") or "")} '
            f'{_esc(r.get("state") or "")}</td></tr>'
        )

    return f"""
      <p class="note"><strong>{pending:,} pending</strong>, {confirmed:,}
        confirmed, {rejected:,} rejected. These are sponsors whose
        <em>name</em> matched an SEC filer while their <em>EIN</em> did not
        &mdash; each is a subsidiary, a rename, or a coincidence.</p>
      <p class="note">There is no bulk-confirm control on purpose.
        Normalized-name matching scores <strong>44.2% precision</strong>
        against EIN ground truth, so confirming by similarity would be wrong
        about half the time and the errors would be invisible. Rows matching
        more than one filer are marked and sorted last: 11 SEC filers share
        the normalized name <code>energy</code>.</p>
      <div class="filters">
        <button class="f" data-f="basis" data-v="exact_name">exact only</button>
        <button class="f" data-f="single">one candidate only</button>
        <button class="f reset" data-f="rvreset">all</button>
        <span class="note rv-count"></span></div>
      <table class="rows" id="review-rows">
        <thead><tr><th data-s="t">sponsor (Form 5500)</th><th>ein</th>
          <th data-s="t">matched filer (SEC)</th><th>cik</th>
          <th>basis</th><th data-s="n" class="num">candidates</th>
          <th>naics / state</th></tr></thead>
        <tbody>{''.join(body)}</tbody>
      </table>"""


#: Filtering for both Weekend 4 panels. Same inline-script rule as the rest.
PRIVATE_SCRIPT: Final[str] = """
/* Registered as a binder rather than run once at load.

   With one panel in the DOM at a time, a script that queried `document` on
   DOMContentLoaded bound to elements that had not been injected yet and
   silently did nothing. Each binder takes the freshly injected root and is
   re-run after every panel switch; the old elements are gone with the old
   innerHTML, so re-binding cannot double up. */
(window.__MR_BINDERS__ = window.__MR_BINDERS__ || []).push(
  function (root) {
  function wire(tableId, countSel, rowClass, filters) {
    var table = document.getElementById(tableId);
    if (!table) { return; }
    var state = {};
    function apply() {
      var shown = 0, total = 0;
      table.querySelectorAll('tbody tr.' + rowClass).forEach(function (r) {
        total++;
        var ok = filters.every(function (f) { return f(r, state); });
        r.hidden = !ok;
        if (ok) { shown++; }
      });
      var c = document.querySelector(countSel);
      if (c) { c.textContent = shown.toLocaleString() + ' of ' +
                               total.toLocaleString(); }
    }
    return { apply: apply, state: state };
  }

  var pv = wire('private-rows', '.pv-count', 'pv', [
    function (r, s) { return !s.naics || r.dataset.naics === s.naics; },
    function (r, s) { return !s.dfeOnly || r.dataset.dfe === '1'; }
  ]);
  if (pv) {
    root.querySelectorAll('button.f[data-f="naics"]').forEach(function (b) {
      b.addEventListener('click', function () {
        pv.state.naics = (pv.state.naics === b.dataset.v) ? null : b.dataset.v;
        root.querySelectorAll('button.f[data-f="naics"]').forEach(
          function (o) { o.classList.toggle('sel', o.dataset.v === pv.state.naics); });
        pv.apply();
      });
    });
    var d = document.querySelector('button.f[data-f="dfeonly"]');
    if (d) d.addEventListener('click', function () {
      pv.state.dfeOnly = !pv.state.dfeOnly;
      d.classList.toggle('sel', pv.state.dfeOnly);
      pv.apply();
    });
    var pr = document.querySelector('button.f[data-f="pvreset"]');
    if (pr) pr.addEventListener('click', function () {
      pv.state.naics = null; pv.state.dfeOnly = false;
      root.querySelectorAll('button.f').forEach(function (o) {
        o.classList.remove('sel'); });
      pv.apply();
    });
    pv.apply();
  }

  var rv = wire('review-rows', '.rv-count', 'rv', [
    function (r, s) { return !s.basis || r.dataset.basis === s.basis; },
    function (r, s) { return !s.single || r.dataset.cand === '1'; }
  ]);
  if (rv) {
    var be = document.querySelector('button.f[data-f="basis"]');
    if (be) be.addEventListener('click', function () {
      rv.state.basis = rv.state.basis ? null : be.dataset.v;
      be.classList.toggle('sel', !!rv.state.basis);
      rv.apply();
    });
    var sg = document.querySelector('button.f[data-f="single"]');
    if (sg) sg.addEventListener('click', function () {
      rv.state.single = !rv.state.single;
      sg.classList.toggle('sel', rv.state.single);
      rv.apply();
    });
    var rr = document.querySelector('button.f[data-f="rvreset"]');
    if (rr) rr.addEventListener('click', function () {
      rv.state.basis = null; rv.state.single = false;
      if (be) be.classList.remove('sel');
      if (sg) sg.classList.remove('sel');
      rv.apply();
    });
    rv.apply();
  }
});
"""


def _funnel_html(funnel: dict[str, Any]) -> str:
    """A funnel's stages, with the reason printed beside each count.

    The first renderer for ``screens.funnel`` on the page, and it is here
    rather than in a screens panel because the normalizer is where the counts
    first had somewhere to go. Two things are marked rather than merely listed,
    matching what the type itself checks: a stage that removed over 90% of what
    reached it, and a stage that emptied the population.
    """
    stages = funnel.get("stages") or []
    if not stages:
        return ""
    rows = []
    for i, stage in enumerate(stages):
        share = float(stage.get("share") or 0)
        removed = int(stage.get("removed") or 0)
        cut = '<span class="note">-</span>' if not i else (
            f"-{removed:,} <span class=\"note\">({share * 100:.1f}%)</span>"
        )
        mark = ('<span class="collapsed">removed nearly everything &mdash; '
                "check it</span>" if stage.get("collapsed") else "")
        rows.append(
            "<tr>"
            f'<td class="tk">{_esc(str(stage.get("name", "")))}</td>'
            f'<td class="num">{int(stage.get("remaining") or 0):,}</td>'
            f"<td class=\"num\">{cut}</td>"
            f'<td class="note">{_esc(str(stage.get("why") or ""))} {mark}</td>'
            "</tr>"
        )
    emptied = funnel.get("emptied")
    warn = ""
    if emptied:
        warn = (f'<p class="why"><span class="why-k">empty</span> '
                f"&#39;{_esc(str(emptied))}&#39; removed every remaining row. "
                "The list is not short, it is gone.</p>")
    return f"""
      <h5>population by stage</h5>
      <p class="note">A short list is either selective or broken, and the
        surviving count at each stage is what tells you which.</p>
      <table class="rows">
        <thead><tr><th>stage</th><th class="num">remaining</th>
          <th class="num">removed</th><th>why it exists</th></tr></thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
      {warn}"""


# --- U10: XBRL fundamentals coverage ------------------------------------

#: Shown against every concept, because a concept's number is not readable
#: without it. A concept at 51% and one at 99% are both just a number in a
#: column otherwise -- the funnel rule, applied one layer further in.
_XBRL_STATUS_WHY: Final[dict[str, str]] = {
    "stated": "a mapped tag, consolidated, at the filing's own period end",
    "absent": "the filer presents no line for it -- pre-revenue, or a nil "
              "tag. Nothing to fix",
    "unmapped": "a line exists under a tag the map does not carry. The only "
                "status that is a work queue",
    "not_usd": "reported in another currency. Out of scope for a USD table, "
               "not a gap",
    "segment_only": "present only disaggregated; no consolidated total",
    "period_mismatch": "the tag is reported, but not for this period",
}


def xbrl_html(
    coverage: list[dict[str, Any]],
    funnel: dict[str, Any] | None = None,
    *,
    quarter: str = "",
) -> str:
    """Per-concept coverage, the statuses behind it, and the tags to add.

    **This panel ships with the tag map rather than after it**, which is the
    one thing about it worth arguing. The map is maintained by hand and
    baseline tag churn between sampled years ran 11-20%, so its coverage rots
    quietly; a screen of which tags resolved and which fell through is the
    fastest way to find what the map needs next, and it is useless if it
    arrives a month after the map does.

    Every concept carries its own number and they are never averaged. Six
    concepts individually clear 98% and all six on one filer is 64%, so a
    single "coverage" figure would describe the intersection and hide which
    concept did the excluding.

    ``drift`` is the number that catches rot: how far this quarter sits from
    the figure the map records for itself. Negative and growing means a tag
    has moved.
    """
    if not coverage:
        return ('<p class="empty">No quarter normalized. Run '
                "<code>mr xbrl --quarter 2024q1</code>.</p>")

    rows = []
    for cov in sorted(coverage, key=lambda c: -float(c.get("rate") or 0)):
        rate = float(cov.get("rate") or 0)
        drift = cov.get("drift")
        # Shown with its direction and never as a bare magnitude: a concept
        # five points *below* what the map claims is the thing to act on, and
        # five points above is not.
        drift_cell = '<span class="note">-</span>'
        if drift is not None:
            cls = " class=\"rot\"" if float(drift) <= -0.01 else ""
            drift_cell = f'<span{cls}>{float(drift) * 100:+.1f}pp</span>'
        statuses = cov.get("by_status") or {}
        chips = "".join(
            f'<span class="xs" data-status="{_esc(name)}" '
            f'title="{_esc(_XBRL_STATUS_WHY.get(name, name))}">'
            f'{_esc(name)} {int(count):,}</span>'
            for name, count in statuses.items()
            if name != "stated" and int(count or 0)
        )
        queue = cov.get("unmapped_tags") or []
        queue_cell = '<span class="note">-</span>'
        if queue:
            queue_cell = ", ".join(
                f'<code>{_esc(str(tag))}</code> <span class="note">'
                f"({int(n):,})</span>" for tag, n in queue[:4]
            )
            if len(queue) > 4:
                queue_cell += f' <span class="note">+{len(queue) - 4} more</span>'
        rows.append(
            f"<tr>"
            f'<td class="tk">{_esc(str(cov.get("concept", "")))}</td>'
            f'<td class="num">{int(cov.get("resolved") or 0):,}</td>'
            f'<td class="num">{rate * 100:.1f}%{_bar(rate)}</td>'
            f'<td class="num">{drift_cell}</td>'
            f"<td>{chips}</td>"
            f"<td>{queue_cell}</td></tr>"
        )

    stages = ""
    if funnel:
        stages = _funnel_html(funnel)
    head = f" &mdash; {_esc(quarter)}" if quarter else ""
    return f"""
      <p class="note">One row per (accession, concept), so asking for revenue
        costs the coverage of revenue and nothing else. <strong>Each concept
        carries its own number and they are never averaged</strong>: these six
        individually clear 98%, and all six on the same filer is 64%, so a
        single figure would report the intersection and hide which concept did
        the excluding. Operating companies only, post-606 only &mdash; banks,
        insurers, brokers and REITs are a different table and are counted out
        in the funnel rather than silently absent.</p>
      <table class="rows">
        <thead><tr><th>concept{head}</th><th class="num">resolved</th>
          <th class="num">rate</th>
          <th class="num" title="how far this quarter sits from the coverage
            the tag map records for itself. The map is hand-maintained and tag
            churn ran 11-20% between sampled years, so it rots quietly.">
            drift</th>
          <th>did not resolve</th><th>tags to add</th></tr></thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
      <p class="note"><strong>Five ways not to resolve, and only one is
        work.</strong> <code>unmapped</code> names a tag to add.
        <code>absent</code> means the filer reports no such line &mdash;
        pre-revenue, or a nil tag, and there is nothing to find.
        <code>not_usd</code> and <code>segment_only</code> are out of scope,
        and <code>period_mismatch</code> is about the filing rather than the
        map. Summing them into "missing" would describe none of them.</p>
      {stages}"""


# --- U15: comps ---------------------------------------------------------

#: How each outcome reads on screen. Spelled out rather than shown as a code,
#: for the same reason the XBRL statuses are: the whole point of having four
#: outcomes instead of one "no peers" is that a reader never has to guess.
_COMPS_OUTCOME_WHY: Final[dict[str, str]] = {
    "served": "a peer set at the floor or better",
    "unplaceable": "no SIC to group on, or no assets to band on -- it cannot "
                   "be placed at all, which is not the same as being alone",
    "immaterial": "the filer's own revenue is below the floor. Widening the "
                  "industry cannot fix this, which is why it is said apart",
    "too_few_peers": "placed, material, and still short at two digits -- the "
                     "end of the ladder, there is no coarser code",
}

#: The two axes a set can be compromised on, plus the intersection. All three
#: are shown because they are different compromises: a fallback means the
#: industry is broader than asked for, a thinned set means the members are fewer
#: than the band selected.
_COMPS_AXES: Final[tuple[tuple[str, str], ...]] = (
    ("industry widened", "the ladder fell past 4-digit SIC"),
    ("thinned", "over half the banded peers are below the revenue floor"),
    ("both", "twice removed from what was asked for"),
    ("clean", "4-digit SIC, set intact"),
)


def _comps_depth_mark(depth: int | None) -> str:
    """The SIC depth, marked when it is not the one that was asked for."""
    if depth is None:
        return '<span class="note">--</span>'
    if depth == 4:
        return "4-digit"
    return f'<span class="collapsed">{depth}-digit</span>'


def _comps_set_mark(row: dict[str, Any]) -> str:
    """Banded against material, so a thinned set is visible as a fraction.

    Printed as ``8 of 50`` rather than as ``8``, because the count alone reads
    as a healthy set when it is the remainder of an unhealthy one.
    """
    banded = int(row.get("peers_banded") or 0)
    material = int(row.get("peers_material") or 0)
    if banded and material < banded:
        cls = "collapsed" if material < banded * 0.5 else "note"
        return f'{material:,} <span class="{cls}">of {banded:,} banded</span>'
    return f"{material:,}"


def comps_html(
    rows: list[dict[str, Any]],
    stats: dict[str, Any] | None = None,
    funnel: dict[str, Any] | None = None,
) -> str:
    """Peer sets, the depth each one settled on, and what it gave up.

    **The depth column is the panel**, not a detail beside it. Measured
    2026-09-12: the SIC digit buys nothing measurable on similarity -- within-set
    asset-turnover IQR is 0.432 at 4-digit and 0.492 at 2-digit on the 2,441
    filers that clear eight peers at all three depths -- while the size band
    moves margin IQR from 0.956 to 0.613. So the ladder is a defensible default
    rather than a validated one, and a reader who disagrees needs to see what
    they got.

    Spread sits beside every median for the same reason a coverage number sits
    beside every XBRL concept: a median with no spread is a number that cannot
    be distrusted.
    """
    stats = stats or {}
    if not rows:
        return ('<p class="why"><span class="why-k">waiting</span> No peer sets '
                "built. Run <code>mr comps</code> over a normalized quarter.</p>")
    body = []
    for row in rows[:ROWS_PER_LIST]:
        iqr = row.get("turnover_iqr")
        med = row.get("turnover_median")
        own = row.get("turnover_self")
        caveat = row.get("caveat") or ""
        body.append(
            "<tr>"
            f'<td class="tk">{_esc(str(row.get("company") or ""))}</td>'
            f'<td class="num">{_esc(str(row.get("sic") or ""))}</td>'
            f'<td>{_comps_depth_mark(row.get("sic_depth"))}</td>'
            f'<td class="num">{_comps_set_mark(row)}</td>'
            f'<td class="num">{"--" if med is None else format(float(med), ".2f")}</td>'
            f'<td class="num">{"--" if iqr is None else format(float(iqr), ".2f")}</td>'
            f'<td class="num">{"--" if own is None else format(float(own), ".2f")}</td>'
            f'<td class="note">{_esc(caveat)}</td>'
            "</tr>"
        )
    outcomes = stats.get("outcomes") or {}
    total = sum(int(v) for v in outcomes.values()) or 1
    why_rows = "".join(
        "<tr>"
        f'<td class="tk">{_esc(name)}</td>'
        f'<td class="num">{int(outcomes.get(name, 0)):,}</td>'
        f'<td class="num">{int(outcomes.get(name, 0)) / total * 100:.1f}%</td>'
        f'<td class="note">{_esc(why)}</td>'
        "</tr>"
        for name, why in _COMPS_OUTCOME_WHY.items()
    )
    depths = stats.get("depths") or {}

    def at_depth(depth: int) -> int:
        return int(depths.get(str(depth), depths.get(depth, 0)) or 0)

    served = sum(at_depth(d) for d in (4, 3, 2)) or 1
    depth_rows = "".join(
        "<tr>"
        f'<td class="tk">{d}-digit</td>'
        f'<td class="num">{at_depth(d):,}</td>'
        f'<td class="num">{at_depth(d) / served * 100:.1f}%</td>'
        f'<td class="note">'
        f'{"what was asked for" if d == 4 else "a fallback"}</td>'
        "</tr>"
        for d in (4, 3, 2)
    )
    axes = stats.get("axes") or {}
    axis_rows = "".join(
        "<tr>"
        f'<td class="tk">{_esc(name)}</td>'
        f'<td class="num">{int(axes.get(name, 0)):,}</td>'
        f'<td class="note">{_esc(why)}</td>'
        "</tr>"
        for name, why in _COMPS_AXES
    )
    alts = stats.get("alternatives") or {}
    alt_rows = "".join(
        "<tr>"
        f'<td class="tk">{_esc(str(label))}</td>'
        f'<td class="num">{int(got):,}</td>'
        "</tr>"
        for label, got in sorted(alts.items())
    )
    stages = _funnel_html(funnel) if funnel else ""
    return f"""
      <p class="why"><span class="why-k">how to read this</span>
        A peer set is an assertion that these companies are alike enough for
        one's ratio to say something about another's, and the assertion is
        usually weaker than it looks. <strong>The depth column is the
        panel.</strong> Measured 2026-09-12 on the 2,441 filers that clear eight
        peers at every depth &mdash; the only population where the three numbers
        compare &mdash; within-set asset-turnover spread is 0.43 at 4-digit and
        0.49 at 2-digit. The extra SIC digit buys nothing measurable. What does
        the work is the size band: margin spread runs 0.96 with no band and 0.61
        inside a 3&times; one.</p>
      <table class="rows">
        <thead><tr><th>company</th><th class="num">SIC</th><th>depth</th>
          <th class="num" title="peers clearing the revenue floor, against the
            number the industry and size rules selected">peers</th>
          <th class="num" title="median asset turnover across the peer set">
            turnover</th>
          <th class="num" title="interquartile range of the peer set's asset
            turnover -- how alike the set it just averaged is">spread</th>
          <th class="num">own</th><th>what it gave up</th></tr></thead>
        <tbody>{''.join(body)}</tbody>
      </table>
      <p class="note"><strong>Asset turnover, not net margin.</strong> Margin's
        denominator is the thing half the biggest SIC code does not have: 2834,
        pharmaceutical preparations, is 793 filers &mdash; 12.3% of the universe
        &mdash; and 53% of them report under $1M of revenue, giving the code a
        within-group margin spread of 15.2 around a <em>median of
        &minus;1.7</em>. Turnover has assets underneath it, and every filer has
        assets.</p>
      <h5>why a filer has no peer set</h5>
      <table class="rows">
        <thead><tr><th>outcome</th><th class="num">filers</th>
          <th class="num">share</th><th>what it means</th></tr></thead>
        <tbody>{why_rows}</tbody>
      </table>
      <h5>the depth the ladder settled on</h5>
      <table class="rows">
        <thead><tr><th>depth</th><th class="num">served</th>
          <th class="num">share</th><th></th></tr></thead>
        <tbody>{depth_rows}</tbody>
      </table>
      <h5>how the served sets are degraded</h5>
      <table class="rows">
        <thead><tr><th>axis</th><th class="num">sets</th><th>why it matters</th>
          </tr></thead>
        <tbody>{axis_rows}</tbody>
      </table>
      <p class="note"><strong>The <code>both</code> row is the one a clean
        median hides.</strong> A widened industry and a thinned set are
        different compromises, so a set carrying both says both &mdash;
        reporting only the worse one would make those look singly degraded.</p>
      <h5>coverage at floors that were not chosen</h5>
      <p class="note">The default is a choice with a cost. Raising the revenue
        floor to $50M tightens turnover spread only from 0.44 to 0.35 while
        dropping coverage from 82.6% to 53.9% of the universe &mdash; so the
        floor is set low, as a correctness floor against a near-zero
        denominator, not as a similarity floor.</p>
      <table class="rows">
        <thead><tr><th>floor</th><th class="num">served</th></tr></thead>
        <tbody>{alt_rows}</tbody>
      </table>
      {stages}"""


# --- U13: DCF / 3-statement ----------------------------------------------

#: Each substitution, and the direction it pushes the answer. Shown as a legend
#: rather than a tooltip: the whole argument of this panel is that the inputs are
#: not the thing the method asks for, and a reader who has to hover to find that
#: out will not.
_DCF_SUBSTITUTION_WHY: Final[dict[str, str]] = {
    "erp_constant": "the equity risk premium is a dated constant, not a measured "
                    "figure. On every row &mdash; there is no free source",
    "growth_constant": "near-term growth is a flat 3%, not a forecast for this "
                       "filer. <strong>Understates</strong> anything growing "
                       "faster",
    "growth_mismatch": "this filer's own history is over 10 points from that "
                       "constant, so 3% is unlikely to be its centre. A warning, "
                       "not a rate",
    "peer_beta": "beta is the peer set's median, not this filer's own returns",
    "comp_depth_fallback": "that peer set is a widened industry, so the beta is "
                           "of a broader group than the label implies",
    "absent_capex": "no capex line, so FCF is operating cash flow undiminished. "
                    "<strong>Overstates</strong> it by anything folded into an "
                    "aggregated investing total",
    "no_beta": "no beta, own or peer. A flat equity cost with no "
               "company-specific risk at all",
}

#: The cohort worth reading first, and the one a reader should not have to build.
#:
#: Note what it already excludes. ``clean_but_constants`` means the row's
#: substitutions are a subset of the two unavoidable constants, and
#: ``growth_mismatch`` is deliberately not one of them -- so a row in this cohort
#: cannot carry the mismatch flag. "Clean apart from the constants, and no
#: mismatch" is one filter, not two, and saying so here is cheaper than letting
#: someone discover it by composing them.
_DCF_COHORT: Final[str] = (
    "own beta, 4-digit comp depth, a real capex line, and a history that does "
    "not contradict the growth constant"
)


def _dcf_subs_html(subs: list[str]) -> str:
    """Substitution chips, worst-first, never a count."""
    if not subs:
        return '<span class="note">none</span>'
    out = []
    for name in subs:
        # The two unavoidable constants are muted; everything else is a choice
        # that went a particular way for this filer and reads as one.
        cls = "note" if name in ("erp_constant", "growth_constant") else "collapsed"
        out.append(f'<span class="{cls}">{_esc(name)}</span>')
    return " ".join(out)


def dcf_html(
    rows: list[dict[str, Any]],
    stats: dict[str, Any] | None = None,
    funnel: dict[str, Any] | None = None,
) -> str:
    """Enterprise values, best-evidence first, with every substitution on the row.

    **Sorted so the readable cohort is the top of the page**, because the
    alternative is a reader reconstructing it from a substitution column every
    time. Depth ascending, then terminal share ascending -- a row whose answer is
    90% terminal value is resting on one growth number however clean its inputs
    are.

    A deck or a screen built on this is the easiest place for the discipline to get
    laundered into something authoritative, so the legend is on the page and the
    substitutions are in the row rather than in a footnote.
    """
    stats = stats or {}
    if not rows:
        return ('<p class="why"><span class="why-k">waiting</span> No valuations '
                "built. Run <code>mr dcf</code> over a normalized quarter.</p>")
    body = []
    for row in rows[:ROWS_PER_LIST]:
        subs = list(row.get("substitutions") or [])
        cohort = "1" if row.get("clean_but_constants") else "0"
        ev = row.get("enterprise_value")
        body.append(
            f'<tr class="dcfr" data-cohort="{cohort}" '
            f'data-depth="{len(subs)}">'
            f'<td class="tk">{_esc(str(row.get("company") or ""))}</td>'
            f'<td class="num">{_money(str(ev)) if ev else "--"}</td>'
            f'<td class="num">'
            f'{"--" if row.get("wacc") is None else f"{float(row["wacc"]) * 100:.1f}%"}'
            "</td>"
            f'<td class="num">'
            f'{"--" if row.get("terminal_share") is None else f"{float(row["terminal_share"]) * 100:.0f}%"}'
            "</td>"
            f'<td class="num">'
            f'{"--" if row.get("beta") is None else f"{float(row["beta"]):.2f}"}'
            "</td>"
            f'<td class="num">{len(subs)}</td>'
            f"<td>{_dcf_subs_html(subs)}</td>"
            "</tr>"
        )
    depths = stats.get("depths") or {}
    valued = sum(int(v) for v in depths.values()) or 1
    depth_rows = "".join(
        "<tr>"
        f'<td class="tk">{_esc(str(n))}</td>'
        f'<td class="num">{int(c):,}</td>'
        f'<td class="num">{int(c) / valued * 100:.1f}%</td>'
        "</tr>"
        for n, c in sorted(depths.items(), key=lambda kv: int(kv[0]))
    )
    counts = stats.get("substitutions") or {}
    sub_rows = "".join(
        "<tr>"
        f'<td class="tk">{_esc(name)}</td>'
        f'<td class="num">{int(counts.get(name, 0)):,}</td>'
        f'<td class="num">{int(counts.get(name, 0)) / valued * 100:.1f}%</td>'
        f"<td class=\"note\">{why}</td>"
        "</tr>"
        for name, why in _DCF_SUBSTITUTION_WHY.items()
        if counts.get(name)
    )
    outcomes = stats.get("outcomes") or {}
    total = sum(int(v) for v in outcomes.values()) or 1
    outcome_rows = "".join(
        "<tr>"
        f'<td class="tk">{_esc(name)}</td>'
        f'<td class="num">{int(got):,}</td>'
        f'<td class="num">{int(got) / total * 100:.1f}%</td>'
        "</tr>"
        for name, got in outcomes.items()
    )
    cohort_n = int(stats.get("cohort") or 0)
    stages = _funnel_html(funnel) if funnel else ""
    return f"""
      <p class="why"><span class="why-k">how to read this</span>
        <strong>No row has zero substitutions, and that is structural.</strong>
        Two constants sit on every valuation because neither has a free source:
        the equity risk premium and the near-term growth rate. So the cohort worth
        reading is the one below &mdash; {_esc(_DCF_COHORT)} &mdash; and the page
        opens sorted to it rather than leaving you to build it from a column.</p>
      <p class="note">A growth rate fitted from our own 30 quarters
        <em>loses</em> to the flat 3% out of sample, on revenue and on free cash
        flow, by CAGR and by log-linear fit &mdash; the correlation between a
        filer's past and future growth runs &minus;0.035 to +0.079. So the
        constant stays and <code>growth_mismatch</code> warns where it is least
        likely to hold. Measured against rough market caps on 20 large caps, the
        mature cohort lands near parity (AbbVie 1.75&times;, Mastercard
        1.08&times;, J&amp;J 0.96&times;) and heavy reinvestors do not (Amazon
        0.03&times;, Tesla 0.07&times;). <strong>This is a lower bound for a
        growth company, not a valuation of one.</strong></p>
      <p class="filters">
        <button class="f sel" data-f="dcohort">best evidence only
          ({cohort_n:,})</button>
        <span class="dcf-count note"></span>
      </p>
      <table class="rows">
        <thead><tr><th>company</th><th class="num">enterprise value</th>
          <th class="num">WACC</th>
          <th class="num" title="share of the enterprise value coming from the
            terminal value. A row at 90% is resting on one growth number however
            clean its other inputs are.">terminal</th>
          <th class="num">beta</th><th class="num">subs</th>
          <th>substitutions</th></tr></thead>
        <tbody id="dcf-rows">{''.join(body)}</tbody>
      </table>
      <h5>how many substitutions deep</h5>
      <table class="rows">
        <thead><tr><th>depth</th><th class="num">rows</th>
          <th class="num">share</th></tr></thead>
        <tbody>{depth_rows}</tbody>
      </table>
      <h5>which substitutions, and which direction each one pushes</h5>
      <table class="rows">
        <thead><tr><th>substitution</th><th class="num">rows</th>
          <th class="num">share</th><th>what it does to the answer</th></tr>
        </thead>
        <tbody>{sub_rows}</tbody>
      </table>
      <h5>why a filer has no valuation</h5>
      <table class="rows">
        <thead><tr><th>outcome</th><th class="num">filers</th>
          <th class="num">share</th></tr></thead>
        <tbody>{outcome_rows}</tbody>
      </table>
      <p class="note">Negative free cash flow is the big exclusion and it is
        real: 3,299 of 6,344 filers have negative <em>operating</em> cash flow,
        dominated by companies with no revenue or under $100M. A growing
        perpetuity of a negative number is a confident-looking negative value, so
        the method does not apply rather than the row being wrong.</p>
      {stages}"""


#: DCF filtering. Same inline-script rule as the rest: one file, no build step.
DCF_SCRIPT: Final[str] = """
(window.__MR_BINDERS__ = window.__MR_BINDERS__ || []).push(
  function (root) {
  var table = document.getElementById('dcf-rows');
  if (!table) { return; }
  /* Starts on, not off. The cohort is the readable population and the page
     should open at it; a reader who wants the degraded rows can ask. */
  var cohortOnly = true;
  function apply() {
    var shown = 0, total = 0;
    table.querySelectorAll('tr.dcfr').forEach(function (r) {
      var ok = !cohortOnly || r.dataset.cohort === '1';
      r.hidden = !ok;
      total++;
      if (ok) { shown++; }
    });
    var c = root.querySelector('.dcf-count');
    if (c) {
      c.textContent = shown + ' of ' + total + ' shown' +
        (cohortOnly ? ' \\u2014 best evidence: own beta, 4-digit comps, a real' +
                      ' capex line, no growth mismatch' : ' \\u2014 all rows');
    }
  }
  var b = root.querySelector('button.f[data-f="dcohort"]');
  if (b) {
    b.addEventListener('click', function () {
      cohortOnly = !cohortOnly;
      b.classList.toggle('sel', cohortOnly);
      apply();
    });
  }
  apply();
});
"""
