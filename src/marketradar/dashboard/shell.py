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

#: Shown in place of a digest-backed body when the digest was skipped. The
#: panel is not empty because the data is missing -- it is empty because this
#: render did not build the thing that fills it, and those are different.
NO_DIGEST: Final[str] = (
    '<p class="empty">Rendered with <code>--fast</code>, which skips the '
    "digest &mdash; so this panel has no body. The data may well be there; "
    "this page just did not build it. Re-run <code>mr dashboard</code> "
    "without <code>--fast</code>.</p>"
)

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
    recent_filings: list[dict[str, Any]] = field(default_factory=list)
    clusters: list[dict[str, Any]] = field(default_factory=list)
    deals: list[dict[str, Any]] = field(default_factory=list)
    outcomes: list[dict[str, Any]] = field(default_factory=list)
    review: list[dict[str, Any]] = field(default_factory=list)
    review_counts: dict[str, int] = field(default_factory=dict)
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

    rows = q("select kind, count(*) as n, max(occurred_at)::text as newest "
             "from signals group by kind")
    ctx.filings = {r[0]: {"count": int(r[1]), "latest": r[2]} for r in rows}

    import json as _json

    for acc, when, payload in q(
        "select accession, occurred_at::text as when_utc, payload::text "
        "from signals where kind = 'edgar_filing' "
        "order by occurred_at desc limit 120"
    ):
        try:
            body = _json.loads(payload)
        except Exception:
            continue
        ctx.recent_filings.append({
            "accession": acc, "filed_at": when,
            "form_type": body.get("form_type", "?"),
            "company": body.get("company", ""), "cik": body.get("cik"),
        })

    for acc, payload in q(
        "select accession, payload::text from signals "
        "where kind = 'form4_cluster' order by occurred_at desc limit 400"
    ):
        try:
            body = _json.loads(payload)
        except Exception:
            continue
        # accession is "cluster:<cik>:<role>:<first>"
        parts = (acc or "").split(":")
        body["issuer_cik"] = parts[1] if len(parts) > 2 else ""
        ctx.clusters.append(body)

    # value_usd is cast to text: it is numeric(20,2) and the point of this
    # column is that a NULL is never zero, so it travels as NULL or as its
    # own digits, never through a float.
    for row in q(
        "select accession, company, cik, filed_date::text as filed, items, "
        "exhibit_signal, text_signal, classifiers_agree, deal_type, "
        "consideration, value_usd::text as value_usd, value_basis, "
        "value_text, filer_role, counterparty, acquirer, target, "
        "party_basis, target_financials, url "
        "from deals order by filed_date desc, value_usd desc nulls last "
        "limit 400"
    ):
        ctx.deals.append({
            "accession": row[0], "company": row[1], "cik": row[2],
            "filed": row[3], "items": row[4], "exhibit_signal": bool(row[5]),
            "text_signal": row[6], "agree": bool(row[7]), "deal_type": row[8],
            "consideration": row[9], "value_usd": row[10],
            "value_basis": row[11], "value_text": row[12],
            "filer_role": row[13], "counterparty": row[14],
            "acquirer": row[15], "target": row[16], "party_basis": row[17],
            "target_financials": row[18], "url": row[19],
        })

    # Numerics as text: these are ratios that must not pass through a float
    # on the way to a page that prints them to two decimals.
    for row in q(
        "select study, slice, horizon, n, n_suspect, median_ret::text, "
        "mean_ret::text, median_excess::text, mean_excess::text, "
        "win_rate::text, median_run_up::text, events, priced, benchmark "
        "from outcome_stats order by study, slice, horizon"
    ):
        ctx.outcomes.append({
            "study": row[0], "slice": row[1], "horizon": row[2],
            "n": row[3], "n_suspect": row[4], "median_ret": row[5],
            "mean_ret": row[6], "median_excess": row[7],
            "mean_excess": row[8], "win_rate": row[9],
            "median_run_up": row[10], "events": row[11], "priced": row[12],
            "benchmark": row[13],
        })

    for status, n in q("select status, count(*) as n from entity_review "
                       "group by status"):
        ctx.review_counts[status] = int(n)
    for row in q(
        "select ein, plan_year, sponsor_name, matched_cik, matched_name, "
        "match_basis, candidates, naics, state, participants, status "
        "from entity_review where status = 'pending' "
        "order by (match_basis <> 'exact_name'), candidates, ein limit 300"
    ):
        ctx.review.append({
            "ein": row[0], "plan_year": row[1], "sponsor_name": row[2],
            "matched_cik": row[3], "matched_name": row[4],
            "match_basis": row[5], "candidates": row[6], "naics": row[7],
            "state": row[8], "participants": row[9], "status": row[10],
        })
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


#: A partition carrying the whole universe rather than a sample. The backfill
#: put ~1.2M-2.7M rows in each year; a sweep-only partition holds tens of
#: thousands. Used to answer "is there enough history for this panel to mean
#: anything" from dataset_stats, without reading a byte from R2.
DEEP_PARTITION_ROWS: Final[int] = 250_000


def _deep_years(ctx: Context) -> int:
    return sum(1 for v in ctx.prices.values() if v["rows"] >= DEEP_PARTITION_ROWS)


def _probe_ticker_detail(ctx: Context) -> tuple[str, str]:
    """The data arrived; the panel has not been built yet.

    Worth distinguishing: this was blocked on the backfill and is now blocked
    on U3, which is a different answer and a different queue.
    """
    years = _deep_years(ctx)
    if not years:
        return WAITING, "the 10-year backfill -- a chart would be three points"
    return NOT_BUILT, f"Weekend 2.5 (U3); {years} years of history are ready"


def _probe_day_over_day(ctx: Context) -> tuple[str, str]:
    if _deep_years(ctx) < 1:
        return WAITING, "two sessions of whole-universe history"
    return LIVE, "NEW marks names absent from the same list last session"


def _probe_liquidity(ctx: Context) -> tuple[str, str]:
    """Was a correctness gap, not a data gap. Both are now closed."""
    years = _deep_years(ctx)
    if not years:
        return WAITING, (
            "a trailing-window ADV and the history to compute it over"
        )
    return LIVE, (
        f"30-session trailing average over {years} years; names with fewer "
        "sessions stay in the ungated lists and are counted"
    )


def _cluster_count(ctx: Context, role: str) -> int:
    return sum(1 for c in ctx.clusters if c.get("role") == role)


def _probe_clusters_insider(ctx: Context) -> tuple[str, str]:
    n = _cluster_count(ctx, "insider")
    if not n:
        return WAITING, "no clusters stored -- run `mr form4`"
    return LIVE, f"{n} officer/director clusters stored, unfiltered"


def _probe_clusters_tenpct(ctx: Context) -> tuple[str, str]:
    n = _cluster_count(ctx, "ten_percent")
    if not n:
        return WAITING, "no clusters stored -- run `mr form4`"
    return LIVE, f"{n} 10%-holder clusters stored, unfiltered"


def _probe_review(ctx: Context) -> tuple[str, str]:
    if not ctx.review_counts:
        return WAITING, "queue is empty -- run `mr form5500`"
    pending = ctx.review_counts.get("pending", 0)
    done = sum(v for k, v in ctx.review_counts.items() if k != "pending")
    return LIVE, (f"{pending:,} pending, {done:,} decided -- name matched, "
                  "EIN did not")


def _probe_private(ctx: Context) -> tuple[str, str]:
    """Driven by render(), which loads the sponsor parquet."""
    stats = getattr(ctx, "_private_stats", None) or {}
    if not stats:
        return WAITING, "no sponsor parquet -- run `mr form5500`"
    return LIVE, (f"{int(stats.get('private') or 0):,} private sponsors, "
                  f"plan year {stats.get('plan_year')}")


def _probe_mature(ctx: Context) -> tuple[str, str]:
    """Driven by render(), which builds the series from the year parquets."""
    stats = getattr(ctx, "_mature_stats", None) or {}
    if not stats:
        return WAITING, ("no multi-year series -- run `mr form5500` for "
                         "2022-2024, then `mr targets`")
    years = stats.get("years") or ()
    if len(years) < 2:
        return WAITING, (f"only plan year {years[0] if years else '?'} is "
                         "loaded; a trend needs two complete years")
    return LIVE, (f"{stats.get('candidates', 0):,} candidates over plan years "
                  f"{years[0]}-{years[-1]}")


def _probe_outcomes(ctx: Context) -> tuple[str, str]:
    if not ctx.outcomes:
        return WAITING, "no study stored -- run `mr outcomes`"
    studies = {r["study"] for r in ctx.outcomes}
    events = sum(r["events"] or 0 for r in ctx.outcomes if r["slice"] == "all"
                 and r["horizon"] == 1)
    return LIVE, (f"{len(studies)} population(s), {events:,} events scored "
                  "at +1/+5/+30 sessions")


def _probe_deals(ctx: Context) -> tuple[str, str]:
    if not ctx.deals:
        return WAITING, "no deal candidates stored -- run `mr deals`"
    review = sum(1 for d in ctx.deals if not d["agree"])
    spac = sum(1 for d in ctx.deals if d["deal_type"] == "spac")
    return LIVE, (f"{len(ctx.deals)} candidates, {review} for review, "
                  f"{spac} SPAC combinations")


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
    Panel("clusters_insider", "Form 4 clusters -- officers & directors",
          "Filings",
          "2+ distinct open-market buyers at one issuer inside 72 hours. "
          "Dollar floor adjustable here, not only in the CLI.",
          probe=_probe_clusters_insider),
    Panel("clusters_tenpct", "Form 4 clusters -- 10% holders", "Filings",
          "The same rule for holders with no officer or director role. Kept "
          "apart because the medians are 78x apart, so one floor cannot "
          "serve both.",
          probe=_probe_clusters_tenpct),
    Panel("deals", "8-K deals", "Filings",
          "Items 1.01 and 2.01, classified. Item 1.01 is only ~16% M&A, so "
          "both classifiers are shown and disagreements are a review queue "
          "rather than a hidden judgement call.",
          probe=_probe_deals),
    Panel("news", "News", "Filings",
          "GDELT and Finnhub headlines against watched issuers.",
          weekend="Weekend 3"),

    Panel("private", "Private companies", "Private",
          "Form 5500 sponsors with no SEC match -- 94.8% of them, which is "
          "the source working rather than failing. NAICS, headcount range, "
          "DFE trustees filterable as their own category.",
          probe=_probe_private),
    Panel("mature", "Mature targets", "Private",
          "Old private employers whose headcount has stopped growing. Age is "
          "a floor from the oldest plan still filed, headcount is a range, "
          "and a sponsor that stopped filing is excluded rather than read as "
          "a decline.",
          probe=_probe_mature),
    Panel("review", "Entity review queue", "Private",
          "Sponsors whose name matched an SEC filer while their EIN did not. "
          "~23k rows, not 800k: EIN is on 100% of filings, so everything "
          "else resolves exactly or is private.",
          probe=_probe_review),

    Panel("xbrl", "XBRL fundamentals", "Analysis",
          "Normalised financials, plus which tags resolved and which fell "
          "through -- the fastest way to find the next branch tag_map needs.",
          weekend="Beyond"),
    Panel("multiples", "Deal multiples", "Analysis",
          "Comparable transactions, filtered before ranked.",
          weekend="Beyond"),
    Panel("outcomes", "Historical outcomes", "Analysis",
          "Forward returns at +1/+5/+30 trading sessions, against a "
          "benchmark. Pure SQL, no LLM -- so it precedes every embedding "
          "rather than justifying one afterwards.",
          probe=_probe_outcomes),
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
    details: dict[str, Any] | None = None,
    private: list[dict[str, Any]] | None = None,
    private_stats: dict[str, Any] | None = None,
    mature: list[dict[str, Any]] | None = None,
    mature_stats: dict[str, Any] | None = None,
) -> str:
    """The page. With a digest, health/macro/screens get real bodies.

    Without one the shell still renders -- a dashboard that refuses to draw
    because prices are unreachable is less useful than one that opens with
    the panel that says so.
    """
    from marketradar.dashboard import detail as tk
    from marketradar.dashboard import panels as body_html

    # **Every renderer is called unconditionally.** Each one owns its empty
    # case and returns a message naming the command that fills it. Gating
    # these on `if <data>:` made all nine of those messages unreachable, so a
    # panel with zero rows rendered byte-identical to one that was never
    # wired up -- which is the exact distinction this shell exists to draw,
    # and it was broken for every panel at once.
    bodies: dict[str, str] = {}
    if digest is not None:
        bodies["health"] = body_html.health_html(digest)
        bodies["macro"] = body_html.macro_html(digest)
        bodies["screens"] = body_html.screens_html(digest)
    else:
        # Four panels take their body from the digest, and their probes read
        # dataset_stats instead -- so without a digest they chipped *live*
        # with an empty slot, which is how `--fast` quietly produced a page
        # whose screens panel looked broken rather than skipped. Say which it
        # is, in the panel, rather than leaving it to be diagnosed.
        for pid in ("health", "macro", "screens"):
            bodies[pid] = NO_DIGEST
    if details:
        bodies["ticker"] = tk.panel_html()
    elif digest is None:
        bodies["ticker"] = NO_DIGEST
    bodies["filings"] = body_html.filings_html(ctx.recent_filings)
    bodies["clusters_insider"] = body_html.clusters_html(
        ctx.clusters, "insider", 50_000)
    bodies["clusters_tenpct"] = body_html.clusters_html(
        ctx.clusters, "ten_percent", 1_000_000)
    bodies["deals"] = body_html.deals_html(ctx.deals)
    bodies["outcomes"] = body_html.outcomes_html(ctx.outcomes)
    bodies["review"] = body_html.review_html(ctx.review, ctx.review_counts)
    bodies["private"] = body_html.private_html(private or [], private_stats or {})
    bodies["mature"] = body_html.mature_html(mature or [], mature_stats or {})

    # Stashed so the probes can see what render() loaded without the probe
    # signature growing a parameter every panel does not need.
    object.__setattr__(ctx, "_private_stats", private_stats or {})
    object.__setattr__(ctx, "_mature_stats", mature_stats or {})
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
    parts = []
    if digest is not None:
        parts.append(body_html.SCRIPT)
    if ctx.recent_filings or ctx.clusters:
        parts.append(body_html.FEED_SCRIPT)
    if ctx.deals:
        parts.append(body_html.DEALS_SCRIPT)
    if private or mature or ctx.review:
        parts.append(body_html.PRIVATE_SCRIPT)
    if details:
        parts.append(
            "window.__TK__=" + json.dumps(details, separators=(",", ":")) + ";"
        )
        parts.append(tk.SCRIPT)
    script = f"<script>{''.join(parts)}</script>" if parts else ""

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
ul.caveats {{ margin:6px 0 2px; padding-left:18px; color:var(--muted);
              font-size:12px; }}
ul.caveats li {{ margin:1px 0; }}
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
:root {{ --series:#2a78d6; --axis:#c3c2b7; --gapfill:rgba(250,178,25,.14); }}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{ --series:#3987e5; --axis:#383835;
    --gapfill:rgba(250,178,25,.10); }}
}}
:root[data-theme="dark"] {{ --series:#3987e5; --axis:#383835;
  --gapfill:rgba(250,178,25,.10); }}
.tk {{ border:1px solid var(--rule); border-radius:8px; padding:12px 14px;
       margin:8px 0 12px; background:var(--plane); }}
.tkhead {{ display:flex; align-items:baseline; gap:12px; margin-bottom:8px; }}
.tkhead h4 {{ margin:0; font-size:15px; font-variant-numeric:tabular-nums; }}
.tkhead .f {{ margin-left:auto; }}
#tk-chart {{ width:100%; height:220px; display:block; }}
.tkgaps {{ font-size:11.5px; color:var(--muted); margin:6px 0 10px; }}
.tkcols {{ display:grid; gap:18px; grid-template-columns:1.4fr 1fr; }}
.tkcols h5 {{ margin:0 0 4px; font-size:10.5px; text-transform:uppercase;
              letter-spacing:.05em; color:var(--muted); font-weight:600; }}
@media (max-width:720px) {{ .tkcols {{ grid-template-columns:1fr; }} }}
.mk {{ display:inline-block; font-size:9.5px; font-weight:700; letter-spacing:.04em;
       border:1px solid var(--rule); border-radius:3px; padding:0 4px;
       margin-right:4px; color:var(--muted); }}
.mk.fund {{ color:#fab219; border-color:#fab219; }}
.mk.stale {{ color:#898781; border-color:#898781; }}
/* The survivorship marker on the excess columns. A caveat that lives only
   in a docstring is a caveat nobody reading the number ever sees. */
.bias {{ display:block; font-size:9px; font-weight:600; color:#fab219;
         letter-spacing:.03em; text-transform:none; }}
.bias-note {{ border-left:2px solid #fab219; padding-left:9px; }}
/* The participant sparkline. A year the sponsor did not file is a *gap* --
   drawn as an empty slot rather than a zero-height bar, because the whole
   point of the series is that an absence is not a headcount of nothing. */
.spark {{ display:inline-flex; align-items:flex-end; gap:2px; height:16px; }}
.spark .sp {{ display:inline-block; width:5px; background:var(--series);
  border-radius:1px; }}
.spark .sp.gap {{ height:16px; width:5px; background:var(--gapfill);
  border-bottom:1px dashed var(--axis); }}
td.spark-cell {{ width:64px; }}
/* Trend is a status, so each carries a glyph and a word as well as a hue. */
.tr {{ font-size:11.5px; font-weight:600; white-space:nowrap; }}
.tr-growing {{ color:#0ca30c; }}
.tr-declining {{ color:#d03b3b; }}
.tr-flat {{ color:var(--ink-2); }}
.tr-unknown {{ color:var(--muted); font-weight:500; }}
td.trend {{ white-space:nowrap; font-size:11.5px; }}
/* Status colours, not series colours: REVIEW is the warning step and 3-05 the
   good one, and both carry a word so the state is never colour alone. */
.mk.rev {{ color:#fab219; border-color:#fab219; }}
.mk.fin {{ color:#0ca30c; border-color:#0ca30c; }}
/* Diverging bar for excess returns: one hue each side of a neutral zero
   line. The number is printed beside it, so colour never carries the value
   alone. */
.oc-bar {{ position:relative; display:inline-block; width:64px; height:6px;
  margin-left:6px; vertical-align:middle; background:var(--rule);
  border-radius:3px; }}
.oc-fill {{ position:absolute; top:0; height:6px; border-radius:3px; }}
tr.oc-all td {{ font-weight:600; }}
tr.dl-why td {{ padding-top:0; padding-bottom:6px; }}
.cl-controls {{ align-items:center; }}
.flr {{ font-size:11.5px; color:var(--ink-2); display:inline-flex;
        align-items:center; gap:4px; }}
.flr input {{ width:104px; font:inherit; font-size:11.5px; padding:2px 6px;
  border:1px solid var(--rule); border-radius:4px; background:var(--surface);
  color:var(--ink); }}
tr.cl-who td {{ padding-top:0; border-bottom:1px solid var(--rule);
                font-size:11px; }}
tr.cl td {{ border-bottom:none; }}
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
    details: dict[str, Any] | None = None,
    private: list[dict[str, Any]] | None = None,
    private_stats: dict[str, Any] | None = None,
    mature: list[dict[str, Any]] | None = None,
    mature_stats: dict[str, Any] | None = None,
) -> Path:
    """Render to a gitignored local file. There is no publish counterpart."""
    target = Path(path) if path else DEFAULT_OUTPUT
    target.parent.mkdir(parents=True, exist_ok=True)
    ctx = ctx or gather(con)
    target.write_text(
        render(ctx, digest=digest, details=details,
               private=private, private_stats=private_stats,
               mature=mature, mature_stats=mature_stats),
        encoding="utf-8",
    )
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
