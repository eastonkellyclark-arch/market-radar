"""The dashboard shell: every panel in the build order, and what it is waiting on.

One static HTML file, written by ``mr dashboard`` and opened in the browser.
No server, no build step, no framework, no network at view time. Data is
embedded rather than fetched, because a ``file://`` page cannot fetch its
neighbours and adding a server to work around that would trade away the whole
point of the constraint.

**Never published.** The screens are computed from Tiingo prices, so putting
them on GitHub Pages or in a Release asset is redistribution -- the same
boundary that keeps ``prices_eod_raw`` in R2 and FRED's ICE series local. The
output path is gitignored and there is deliberately no publish function here.

**The shell is the map.** Every panel in the whole build order is declared,
including the six that do not exist yet, and each renders one of three states:

    live        data is present and current
    waiting     built, but its data has not landed. Says which data.
    not built   on the roadmap. Says which weekend.

An absent panel is indistinguishable from a broken one. A panel that says
"waiting on the 10-year backfill" is not, and that distinction is the reason
the shell declares things it cannot yet draw.

State is a *status*, so it is never carried by colour alone: every chip pairs
a glyph and a word with its colour, per the status rule in the dataviz skill.
"""

from __future__ import annotations

import html
import json
import logging
import webbrowser
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Final

import duckdb

from marketradar import storage

log = logging.getLogger(__name__)

#: Gitignored, and named so it is obvious why. See docs/build-spec.md.
DEFAULT_OUTPUT: Final[Path] = Path(".dashboard") / "index.html"

LIVE: Final[str] = "live"
WAITING: Final[str] = "waiting"
NOT_BUILT: Final[str] = "not built"

#: Status palette from the dataviz reference instance. Fixed, never themed.
#: "not built" is deliberately *not* a status colour -- it is a roadmap state,
#: not an alarm, and muted ink keeps it from competing with `waiting`.
STATE_STYLE: Final[dict[str, tuple[str, str]]] = {
    LIVE: ("#0ca30c", "●"),
    WAITING: ("#fab219", "◐"),
    NOT_BUILT: ("#898781", "○"),
}


@dataclass(frozen=True, slots=True)
class Panel:
    id: str
    title: str
    section: str
    what: str
    #: Set when the panel cannot be live. The whole point of declaring panels
    #: that do not work yet is that they say *why*.
    waiting_on: str = ""
    weekend: str = ""
    probe: Callable[["Context"], tuple[str, str]] | None = None

    def resolve(self, ctx: "Context") -> tuple[str, str]:
        """(state, detail). A panel with no probe is on the roadmap."""
        if self.probe is None:
            return NOT_BUILT, self.weekend
        return self.probe(ctx)


@dataclass
class Context:
    """What the shell knows without touching R2.

    Price coverage is read from ``dataset_stats`` rather than from the
    partitions themselves: that table exists precisely to answer "what is in
    them", and a dashboard that took eleven remote reads to render its own
    status page would be its own worst panel.
    """

    generated_at: datetime
    postgres: bool = False
    macro: dict[str, Any] = field(default_factory=dict)
    prices: dict[str, Any] = field(default_factory=dict)
    entities: dict[str, Any] = field(default_factory=dict)
    filings: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def gather(con: duckdb.DuckDBPyConnection | None = None) -> Context:
    """Everything the shell needs, cheaply. Postgres only; no R2."""
    con = con or storage.connect()
    ctx = Context(generated_at=datetime.now(timezone.utc))
    ctx.postgres = storage.postgres_attached(con)
    if not ctx.postgres:
        ctx.notes.append("No Postgres attached; every panel is reporting blind.")
        return ctx

    def q(sql: str) -> list[tuple]:
        try:
            return con.execute(
                "SELECT * FROM postgres_query('pg', ?)", [sql]
            ).fetchall()
        except Exception as exc:  # a missing table is a state, not a crash
            log.warning("probe failed: %s", str(exc)[:160])
            return []

    rows = q("select series_id, max(obs_date)::text from macro_series "
             "where value is not null group by series_id")
    ctx.macro = {r[0]: r[1] for r in rows}

    # Aliased: two bare max() columns come back both named "max" and DuckDB
    # refuses the duplicate. Same shape as the digest health query.
    rows = q("select partition, max(row_count) as n_rows, "
             "max(max_date)::text as newest "
             "from dataset_stats where dataset = 'prices_eod_raw' "
             "group by partition order by partition")
    ctx.prices = {r[0]: {"rows": int(r[1] or 0), "max_date": r[2]} for r in rows}

    rows = q("select (select count(*) from companies) as a, "
             "(select count(*) from company_tickers) as b")
    if rows:
        ctx.entities = {"companies": int(rows[0][0]), "tickers": int(rows[0][1])}

    rows = q("select kind, count(*), max(occurred_at)::text "
             "from signals group by kind")
    ctx.filings = {r[0]: {"count": int(r[1]), "latest": r[2]} for r in rows}
    return ctx


# --- probes -------------------------------------------------------------
# Each returns (state, detail). Detail is shown verbatim, so it says the
# specific thing rather than "no data".


def _probe_health(ctx: Context) -> tuple[str, str]:
    if not ctx.postgres:
        return WAITING, "no database connection"
    return LIVE, "coverage, staleness and entity counts"


def _probe_macro(ctx: Context) -> tuple[str, str]:
    if not ctx.macro:
        return WAITING, "macro_series is empty -- run `mr fred`"
    newest = max(ctx.macro.values())
    return LIVE, f"{len(ctx.macro)} series, newest {newest}"


def _sessions(ctx: Context) -> int:
    """Partitions carrying prices. A proxy for how much history exists."""
    return sum(1 for v in ctx.prices.values() if v["rows"] > 0)


def _probe_screens(ctx: Context) -> tuple[str, str]:
    if not ctx.prices:
        return WAITING, "no price partitions recorded -- run `mr prices`"
    newest = max((v["max_date"] or "") for v in ctx.prices.values())
    total = sum(v["rows"] for v in ctx.prices.values())
    return LIVE, f"{total:,} bars across {_sessions(ctx)} partitions, to {newest}"


def _probe_names(ctx: Context) -> tuple[str, str]:
    if not ctx.entities:
        return WAITING, "companies is empty -- run `mr sec-tickers`"
    return LIVE, (
        f"{ctx.entities['companies']:,} CIKs; about half the universe has no "
        "SEC match by construction"
    )


def _probe_filings(ctx: Context) -> tuple[str, str]:
    row = ctx.filings.get("edgar_filing")
    if not row:
        return WAITING, "no filings stored -- run `mr edgar`"
    return LIVE, f"{row['count']:,} filings, latest {row['latest'][:16]}"


def _probe_ticker_detail(ctx: Context) -> tuple[str, str]:
    """Mechanically fine on three bars, and useless. That is worth saying."""
    return WAITING, (
        "the 10-year backfill -- most names carry a handful of sessions, so a "
        "chart would be three points"
    )


def _probe_day_over_day(ctx: Context) -> tuple[str, str]:
    return WAITING, (
        "two sessions of whole-universe history; the nightly sweep supplies "
        "this without the backfill"
    )


def _probe_liquidity(ctx: Context) -> tuple[str, str]:
    """Not a data gap. A correctness gap that the backfill will activate."""
    return WAITING, (
        "a trailing-window ADV. Average dollar volume currently has no time "
        "window, so it silently becomes a multi-year average once history "
        "lands -- and 12 of the 24 lists are gated on it"
    )


# --- the registry -------------------------------------------------------

PANELS: Final[tuple[Panel, ...]] = (
    Panel("health", "Health", "Markets",
          "Sweep coverage, staleness per source, entity counts.",
          probe=_probe_health),
    Panel("macro", "Macro", "Markets",
          "10-year Treasury and ICE BofA credit spreads, with 30-day and "
          "1-year changes in basis points.",
          probe=_probe_macro),
    Panel("screens", "Volatility screens", "Markets",
          "24 lists: gainers and losers, three price bands, stocks apart from "
          "ETFs, a parallel >$5M ADV set. Ungated lists collapsed.",
          probe=_probe_screens),
    Panel("names", "Company names", "Markets",
          "Issuer names joined onto screen rows; ambiguous tickers marked "
          "rather than silently resolved.",
          probe=_probe_names),
    Panel("liquidity", "Liquidity gate", "Markets",
          "The >$5M average-dollar-volume gate behind half the screen lists.",
          waiting_on="trailing-window ADV", probe=_probe_liquidity),
    Panel("ticker", "Ticker detail", "Markets",
          "Recent bars for one name, split-adjusted at read time.",
          waiting_on="10-year backfill", probe=_probe_ticker_detail),
    Panel("dod", "Day-over-day", "Markets",
          "NEW markers: names absent from the same list on the prior session.",
          waiting_on="universe history", probe=_probe_day_over_day),

    Panel("filings", "EDGAR filing feed", "Filings",
          "The seven watched form types: 4, 8-K, S-4, DEFM14A, SC 13D, "
          "SC TO-T, SC 13E-3.",
          probe=_probe_filings),
    Panel("clusters", "Form 4 clusters", "Filings",
          "Two lists, not one: officer/director clusters and 10%-holder "
          "clusters, dollar-weighted, plan purchases flagged.",
          weekend="Weekend 3"),
    Panel("news", "News", "Filings",
          "GDELT and Finnhub headlines against watched issuers.",
          weekend="Weekend 3"),

    Panel("private", "Private companies", "Private",
          "Form 5500 sponsors by NAICS, employee count, three-year trend.",
          weekend="Weekend 4"),
    Panel("review", "Entity review queue", "Private",
          "Fuzzy sponsor-name matches, confirmed or rejected by hand. A "
          "working surface rather than a readout.",
          weekend="Weekend 4"),

    Panel("xbrl", "XBRL fundamentals", "Analysis",
          "Normalised financials, plus which tags resolved and which fell "
          "through -- the fastest way to find the next branch tag_map needs.",
          weekend="Beyond"),
    Panel("teardowns", "M&A teardowns", "Analysis",
          "8-K item codes, deal structure, consideration.",
          weekend="Weekend 3 to Beyond"),
    Panel("multiples", "Deal multiples", "Analysis",
          "Comparable transactions, filtered before ranked.",
          weekend="Beyond"),
    Panel("outcomes", "Historical outcomes", "Analysis",
          "Forward returns at +1d/+5d/+30d. Pure SQL, no LLM -- can precede "
          "every embedding.",
          weekend="Beyond"),
    Panel("dcf", "DCF / 3-statement", "Analysis",
          "Model output against the normalised statements.",
          weekend="Beyond"),
    Panel("decks", "Pitch decks", "Analysis",
          "Generated deck preview, before it is a file.",
          weekend="Beyond"),
)

SECTIONS: Final[tuple[str, ...]] = ("Markets", "Filings", "Private", "Analysis")


# --- rendering ----------------------------------------------------------


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _panel_html(panel: Panel, state: str, detail: str, body: str = "") -> str:
    color, glyph = STATE_STYLE[state]
    waiting = ""
    if state != LIVE and detail:
        label = "waiting on" if state == WAITING else "planned for"
        waiting = (
            f'<p class="why"><span class="why-k">{label}</span> {_esc(detail)}</p>'
        )
    elif detail:
        waiting = f'<p class="why"><span class="why-k">now</span> {_esc(detail)}</p>'
    return f"""
      <article class="panel{' wide' if body else ''}"
               data-state="{_esc(state)}" data-panel="{_esc(panel.id)}">
        <header>
          <h3>{_esc(panel.title)}</h3>
          <span class="chip" style="--chip:{color}">
            <span class="glyph" aria-hidden="true">{glyph}</span>{_esc(state)}
          </span>
        </header>
        <p class="what">{_esc(panel.what)}</p>
        {waiting}
        <div class="slot">{body}</div>
      </article>"""


def render(
    ctx: Context,
    panels: tuple[Panel, ...] = PANELS,
    digest: Any = None,
) -> str:
    """The page. With a digest, health/macro/screens get real bodies.

    Without one the shell still renders -- a dashboard that refuses to draw
    because prices are unreachable is less useful than one that opens with
    the panel that says so.
    """
    bodies: dict[str, str] = {}
    if digest is not None:
        from marketradar.dashboard import panels as body_html

        bodies = {
            "health": body_html.health_html(digest),
            "macro": body_html.macro_html(digest),
            "screens": body_html.screens_html(digest),
        }

    resolved = [(p, *p.resolve(ctx)) for p in panels]
    counts = {s: sum(1 for _, st, _ in resolved if st == s)
              for s in (LIVE, WAITING, NOT_BUILT)}

    sections = []
    for section in SECTIONS:
        items = [r for r in resolved if r[0].section == section]
        if not items:
            continue
        body = "".join(
            _panel_html(p, st, d, bodies.get(p.id, "")) for p, st, d in items
        )
        sections.append(
            f'<section><h2>{_esc(section)}</h2><div class="grid">{body}</div></section>'
        )

    legend = "".join(
        f'<span class="chip" style="--chip:{STATE_STYLE[s][0]}">'
        f'<span class="glyph" aria-hidden="true">{STATE_STYLE[s][1]}</span>'
        f'{_esc(s)} · {counts[s]}</span>'
        for s in (LIVE, WAITING, NOT_BUILT)
    )
    notes = "".join(f"<li>{_esc(n)}</li>" for n in ctx.notes)
    stamp = ctx.generated_at.strftime("%Y-%m-%d %H:%M UTC")
    script = ""
    if bodies:
        from marketradar.dashboard.panels import SCRIPT

        script = f"<script>{SCRIPT}</script>"

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Market Radar</title>
<style>
:root {{
  color-scheme: light;
  --plane:#f9f9f7; --surface:#fcfcfb;
  --ink:#0b0b0b; --ink-2:#52514e; --muted:#898781;
  --rule:#e1e0d9;
  font-synthesis-weight: none;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    color-scheme: dark;
    --plane:#0d0d0d; --surface:#1a1a19;
    --ink:#ffffff; --ink-2:#c3c2b7; --muted:#898781;
    --rule:#2c2c2a;
  }}
}}
:root[data-theme="dark"] {{
  color-scheme: dark;
  --plane:#0d0d0d; --surface:#1a1a19;
  --ink:#ffffff; --ink-2:#c3c2b7; --muted:#898781; --rule:#2c2c2a;
}}
* {{ box-sizing:border-box; }}
body {{
  margin:0; padding:28px clamp(16px,4vw,48px) 64px;
  background:var(--plane); color:var(--ink);
  font:14px/1.5 ui-sans-serif,-apple-system,"Segoe UI",system-ui,sans-serif;
}}
h1 {{ font-size:20px; margin:0 0 2px; letter-spacing:-0.01em; }}
h2 {{
  font-size:12px; text-transform:uppercase; letter-spacing:.08em;
  color:var(--muted); margin:34px 0 12px; font-weight:600;
}}
h3 {{ font-size:14px; margin:0; font-weight:600; }}
.sub {{ color:var(--ink-2); margin:0 0 18px; font-size:13px; }}
.legend {{ display:flex; gap:8px; flex-wrap:wrap; margin:14px 0 4px; }}
.chip {{
  display:inline-flex; align-items:center; gap:6px;
  border:1px solid var(--rule); border-radius:999px;
  padding:2px 10px; font-size:11.5px; color:var(--ink-2);
  background:var(--surface); white-space:nowrap;
}}
.glyph {{ color:var(--chip); font-size:13px; line-height:1; }}
.grid {{
  display:grid; gap:12px;
  grid-template-columns:repeat(auto-fill,minmax(288px,1fr));
}}
.panel {{
  background:var(--surface); border:1px solid var(--rule);
  border-radius:10px; padding:14px 15px 13px; min-height:132px;
  display:flex; flex-direction:column;
}}
.panel header {{ display:flex; align-items:center; justify-content:space-between; gap:10px; }}
.what {{ color:var(--ink-2); margin:8px 0 0; font-size:12.5px; }}
.why {{ margin:9px 0 0; font-size:12px; color:var(--muted); }}
.why-k {{
  text-transform:uppercase; letter-spacing:.06em; font-size:10px;
  font-weight:600; margin-right:6px; color:var(--muted);
}}
.slot {{ flex:1; min-height:0; }}
.panel[data-state="not built"] {{ opacity:.62; border-style:dashed; }}
.notes {{ margin:18px 0 0; padding-left:18px; color:var(--muted); font-size:12px; }}
footer {{ margin-top:40px; color:var(--muted); font-size:11.5px;
          border-top:1px solid var(--rule); padding-top:12px; }}
.panel.wide {{ grid-column:1/-1; }}
.status {{ display:flex; align-items:center; gap:7px; margin:10px 0 8px;
           font-weight:600; font-size:13px; }}
.status .glyph {{ color:var(--chip); font-size:12px; font-weight:700; }}
table {{ border-collapse:collapse; width:100%; font-size:12.5px; }}
.kv td, .kv th {{ padding:3px 10px 3px 0; text-align:left; vertical-align:top; }}
.kv th {{ color:var(--muted); font-weight:600; font-size:11px;
          text-transform:uppercase; letter-spacing:.05em; }}
.kv tr.bad td {{ color:var(--ink); }}
td.mark {{ color:#d03b3b; font-weight:700; width:12px; }}
.note {{ color:var(--muted); }}
.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
.strong {{ font-weight:600; }}
.up {{ color:#0ca30c; }} .down {{ color:#d03b3b; }}
.empty {{ color:var(--muted); font-size:12.5px; }}
.filters {{ display:flex; gap:6px; flex-wrap:wrap; margin:12px 0 14px; }}
button.f {{
  font:inherit; font-size:11.5px; cursor:pointer; color:var(--ink-2);
  background:var(--plane); border:1px solid var(--rule);
  border-radius:999px; padding:3px 11px;
}}
button.f.sel {{ background:var(--ink); color:var(--surface); border-color:var(--ink); }}
details.list {{ border-top:1px solid var(--rule); padding:7px 0; }}
details.list[hidden] {{ display:none; }}
summary {{
  cursor:pointer; display:flex; align-items:baseline; gap:10px;
  list-style:none; font-size:12.5px;
}}
summary::-webkit-details-marker {{ display:none; }}
summary::before {{ content:"B8"; color:var(--muted); font-size:10px; }}
details[open] > summary::before {{ content:"BE"; }}
.ltitle {{ font-weight:600; }}
.count {{ color:var(--muted); font-size:11px; }}
.peek {{ margin-left:auto; color:var(--ink-2); font-variant-numeric:tabular-nums; }}
.peek.empty {{ color:var(--muted); }}
table.rows {{ margin:8px 0 4px; }}
table.rows th {{
  color:var(--muted); font-weight:600; font-size:10.5px; text-align:left;
  text-transform:uppercase; letter-spacing:.05em; padding:4px 8px 4px 0;
  border-bottom:1px solid var(--rule); cursor:pointer; user-select:none;
}}
table.rows th.num {{ text-align:right; }}
table.rows td {{ padding:3px 8px 3px 0; border-bottom:1px solid var(--rule); }}
td.tk {{ font-weight:600; font-variant-numeric:tabular-nums; }}
td.new {{ color:#0ca30c; font-size:9.5px; font-weight:700; width:26px; }}
</style></head>
<body>
<h1>Market Radar</h1>
<p class="sub">Panel map · generated {_esc(stamp)}</p>
<div class="legend">{legend}</div>
{''.join(sections)}
{f'<ul class="notes">{notes}</ul>' if notes else ''}
<footer>
  Local file. Never published: the screens are computed from Tiingo prices, so
  putting them on a public host is redistribution.
</footer>
{script}
</body></html>
"""


def write(
    path: Path | None = None,
    con: duckdb.DuckDBPyConnection | None = None,
    ctx: Context | None = None,
    digest: Any = None,
) -> Path:
    """Render to a gitignored local file. There is no publish counterpart."""
    target = Path(path) if path else DEFAULT_OUTPUT
    target.parent.mkdir(parents=True, exist_ok=True)
    ctx = ctx or gather(con)
    target.write_text(render(ctx, digest=digest), encoding="utf-8")
    return target


def open_in_browser(path: Path) -> bool:
    try:
        return webbrowser.open(path.resolve().as_uri())
    except Exception as exc:  # pragma: no cover - depends on the desktop
        log.warning("could not open a browser: %s", exc)
        return False


def summary(ctx: Context, panels: tuple[Panel, ...] = PANELS) -> dict[str, int]:
    resolved = [p.resolve(ctx)[0] for p in panels]
    return {s: resolved.count(s) for s in (LIVE, WAITING, NOT_BUILT)}
