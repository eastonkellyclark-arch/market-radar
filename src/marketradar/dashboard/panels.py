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
(function () {
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
  document.querySelectorAll('button.f[data-f="dtype"]').forEach(function (b) {
    b.addEventListener('click', function () {
      dtype = (dtype === b.dataset.v) ? null : b.dataset.v;
      document.querySelectorAll('button.f[data-f="dtype"]').forEach(function (o) {
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
      document.querySelectorAll('button.f').forEach(function (o) {
        o.classList.remove('sel');
      });
      apply();
    });
  }
  apply();
})();
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
          <table class="rows">
            <thead><tr><th>slice</th><th class="num">h</th>
              <th class="num">n</th><th class="num">median</th>
              <th class="num">median excess</th><th class="num">mean excess</th>
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
            f'data-head="{hi}">'
            f'<td class="tk">{_esc((r.get("sponsor_name") or "")[:38])}</td>'
            f'<td class="note">{_esc(r.get("state") or "")}</td>'
            f'<td class="note" title="{_esc(naics_label(r.get("naics")))}">'
            f'{_esc(r.get("naics") or "-")}</td>'
            f'<td class="num">{r.get("plans") or 0}</td>'
            f'<td class="num">{head}</td>'
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
        plan is a floor and the sum across plans a ceiling.</p>
      <div class="filters">{chips}
        <button class="f" data-f="dfeonly">DFE only</button>
        <button class="f reset" data-f="pvreset">all</button>
        <span class="note pv-count"></span></div>
      <table class="rows" id="private-rows">
        <thead><tr><th data-s="t">sponsor</th><th>state</th>
          <th data-s="t">naics</th><th data-s="n" class="num">plans</th>
          <th data-s="n" class="num">participants</th>
          <th>flags</th></tr></thead>
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
(function () {
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
    document.querySelectorAll('button.f[data-f="naics"]').forEach(function (b) {
      b.addEventListener('click', function () {
        pv.state.naics = (pv.state.naics === b.dataset.v) ? null : b.dataset.v;
        document.querySelectorAll('button.f[data-f="naics"]').forEach(
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
      document.querySelectorAll('button.f').forEach(function (o) {
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
})();
"""
