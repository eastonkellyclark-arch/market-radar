"""``mr`` — every job runs standalone from here.

If something only works inside a GitHub Action, it is built wrong: the Action
should call the same subcommand you would call by hand.

Unimplemented subcommands exit non-zero rather than doing nothing quietly.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import logging
import os
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from marketradar import __version__

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOT_IMPLEMENTED = 2


def load_dotenv(path: Path | None = None) -> int:
    """Load .env into the environment if present. Never overrides a real var.

    Kept deliberately tiny rather than taking a dependency: it reads one file
    at the I/O boundary and does nothing clever. Existing environment values
    win, so Actions secrets are never shadowed by a stray local file.
    """
    path = path or Path.cwd() / ".env"
    if not path.is_file():
        return 0
    loaded = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name, value = name.strip(), value.strip().strip("\"'")
        if name and name not in os.environ:
            os.environ[name] = value
            loaded += 1
    return loaded


def _iso_date(value: str) -> date:
    """Dates are ``date`` objects everywhere except the I/O boundary."""
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not an ISO date (YYYY-MM-DD)"
        ) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mr",
        # ASCII only: this string reaches Windows consoles that are not UTF-8.
        description="Market Radar - equities intelligence and deal sourcing.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="debug-level logging"
    )

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p_prices = sub.add_parser("prices", help="load EOD prices from Tiingo")
    p_prices.add_argument("--date", type=_iso_date, help="trading date (default: latest)")
    p_prices.add_argument(
        "--start", type=_iso_date, help="range start (default: --days before end)"
    )
    p_prices.add_argument("--days", type=int, default=5, help="lookback window")
    p_prices.add_argument(
        "--limit", type=int, help="cap the universe — for proving the mechanism cheaply"
    )
    p_prices.add_argument("--chunk-size", type=int, default=100)
    p_prices.add_argument("--rate-per-hour", type=int, default=9000)
    p_prices.add_argument(
        "--dry-run", action="store_true", help="resolve the universe, fetch nothing"
    )
    p_prices.add_argument(
        "--no-publish", action="store_true", help="sweep and stage, do not publish"
    )
    p_prices.add_argument(
        "--restart",
        action="store_true",
        help="discard checkpoints and re-sweep from scratch",
    )
    p_prices.add_argument(
        "--restate",
        action="store_true",
        help="replace each partition with exactly what this run staged, "
             "instead of merging into what is already there. Needed after a "
             "schema change, and the only way to remove rows. Destructive: "
             "history outside this run's window is dropped.",
    )

    p_sec = sub.add_parser(
        "sec-tickers", help="load the SEC CIK/ticker map and seed companies"
    )
    p_sec.add_argument(
        "--no-publish", action="store_true", help="upsert only, skip the Release"
    )
    p_sec.add_argument(
        "--reconcile", action="store_true",
        help="report coverage against the Tiingo universe and exit",
    )

    p_screens = sub.add_parser("screens", help="rebuild screens from local data")
    p_screens.add_argument(
        "--as-of", type=_iso_date, metavar="YYYY-MM-DD",
        help="trading day to screen (default: newest date in the data)",
    )
    p_screens.add_argument(
        "--top", type=int, default=20, metavar="N", help="rows per list",
    )
    p_screens.add_argument(
        "--sanity-floor", default="0.01", metavar="PRICE",
        help="drop moves where either end is below this price (default 0.01). "
             "Lower it to see the sub-penny band; it is not a liquidity gate.",
    )
    p_screens.add_argument(
        "--adjust-dividends", action="store_true",
        help="add cash dividends back for a total-return view",
    )
    p_screens.add_argument(
        "--summary", action="store_true",
        help="one line per list instead of the full lists",
    )
    p_screens.add_argument(
        "--show-empty", action="store_true", help="print empty lists too",
    )

    p_edgar = sub.add_parser(
        "edgar", help="poll EDGAR's current-filings feed into signals"
    )
    p_edgar.add_argument(
        "--forms", nargs="+", metavar="TYPE",
        help="form types to poll (default: the watched set)",
    )
    p_edgar.add_argument(
        "--no-load", action="store_true", help="fetch and report, store nothing"
    )

    p_5500 = sub.add_parser(
        "form5500",
        help="load DOL Form 5500 and resolve sponsors by EIN",
    )
    p_5500.add_argument("--year", type=int, default=2024, metavar="YYYY",
                        help="plan year (default 2024, the newest complete "
                             "one -- filings lag the plan year by ~18 months)")
    p_5500.add_argument("--cache", default=".cache", metavar="DIR",
                        help="where the DOL zips and sec_eins.parquet live")
    p_5500.add_argument("--out", default=".cache/form5500", metavar="DIR",
                        help="where to write the sponsor parquet")
    p_5500.add_argument("--baseline", type=int, metavar="N",
                        help="filings in the newest complete year, for the "
                             "partial-year check")
    p_5500.add_argument("--top-naics", type=int, default=10, metavar="N",
                        help="NAICS codes to print (0 to skip)")
    p_5500.add_argument("--no-load", action="store_true",
                        help="report only; write no parquet and no queue rows")

    p_xbrl = sub.add_parser(
        "xbrl",
        help="normalize one quarter of SEC fundamentals and report coverage",
    )
    p_xbrl.add_argument("--quarter", nargs="+", required=True, metavar="YYYYqQ",
                        help="one or more quarters, e.g. --quarter 2023q4 "
                             "2024q1. Each is loaded into its own partition")
    p_xbrl.add_argument("--out", default=".cache/xbrl/out", metavar="DIR",
                        help="where the partition parquet is written")
    p_xbrl.add_argument("--cache", default=None, metavar="DIR",
                        help="where the quarterly zips are kept "
                             "(default .cache/xbrl)")
    p_xbrl.add_argument("--concept", nargs="+", default=None, metavar="NAME",
                        help="only these concepts. The table is long, so "
                             "asking for revenue costs the coverage of "
                             "revenue and nothing else")
    p_xbrl.add_argument("--no-load", action="store_true",
                        help="report coverage only; write no parquet")

    p_targets = sub.add_parser(
        "targets",
        help="old private employers whose headcount stopped growing",
    )
    p_targets.add_argument("--years", type=int, nargs="+", metavar="YYYY",
                           help="plan years to build the series from "
                                "(default: every published parquet found)")
    p_targets.add_argument("--out", default=".cache/form5500", metavar="DIR",
                           help="where the sponsor parquets live")
    p_targets.add_argument("--min-age", type=int, default=None, metavar="Y",
                           help="minimum years since the oldest plan still "
                                "filed -- a floor on entity age, never the age")
    p_targets.add_argument("--min-participants", type=int, default=None,
                           metavar="N", help="headcount floor, read against "
                                             "the largest single plan")
    p_targets.add_argument("--max-participants", type=int, default=None,
                           metavar="N", help="headcount ceiling")
    p_targets.add_argument("--state", metavar="XX",
                           help="only this state")
    p_targets.add_argument("--naics", metavar="CODE",
                           help="only NAICS codes starting with this")
    p_targets.add_argument("--nonprofits", default="exclude",
                           choices=("exclude", "only", "include"),
                           help="colleges, churches and museums fit every "
                                "other filter and cannot be bought. "
                                "'only' shows what is being set aside")
    p_targets.add_argument("--sort", default="age",
                           choices=("age", "decline", "size", "smallest"),
                           help="order the list (default age: the only input "
                                "whose direction is not a judgement call)")
    p_targets.add_argument("--top", type=int, default=40, metavar="N",
                           help="rows to print (default 40)")

    p_audit = sub.add_parser(
        "actions-audit",
        help="large price moves no corporate action explains (missing splits)",
    )
    p_audit.add_argument("--days", type=int, metavar="N",
                         help="only scan the last N days (default: all history)")
    p_audit.add_argument("--top", type=int, default=30, metavar="N",
                         help="rows to print (default 30)")
    p_audit.add_argument("--jump", default="1.00", metavar="RATIO",
                         help="gain that counts as a candidate reverse split "
                              "(default 1.00, i.e. +100%%)")
    p_audit.add_argument("--fall", default="-0.60", metavar="RATIO",
                         help="fall that counts as a candidate forward split "
                              "(default -0.60)")

    p_out = sub.add_parser(
        "outcomes",
        help="forward returns after Form 4 clusters and 8-K deals (pure SQL)",
    )
    p_out.add_argument("--study", choices=("form4", "deals", "both"),
                       default="both", help="which population to score")
    p_out.add_argument("--clusters", metavar="PATH",
                       help="parquet of historical Form 4 clusters (default: "
                            ".cache/form4_clusters.parquet, else the live "
                            "signals table)")
    p_out.add_argument("--benchmark", default="SPY", metavar="TICKER",
                       help="benchmark for excess returns (default SPY)")
    p_out.add_argument("--no-load", action="store_true",
                       help="compute and print, store nothing")

    p_deals = sub.add_parser(
        "deals", help="extract M&A from 8-K Items 1.01 and 2.01"
    )
    p_deals.add_argument("--date", type=_iso_date, metavar="YYYY-MM-DD",
                         help="last day to read (default: today)")
    p_deals.add_argument("--days", type=int, default=5, metavar="N",
                         help="how many days back to read (default 5)")
    p_deals.add_argument(
        "--no-exhibits", action="store_true",
        help="skip the press-release fetch. Body-only value coverage was 68%% "
             "against 77%% with exhibits, so this trades nine points of "
             "coverage for one fewer request per valueless candidate.",
    )
    p_deals.add_argument("--no-load", action="store_true",
                         help="extract and report, store nothing")
    p_deals.add_argument("--review", action="store_true",
                         help="print only candidates whose classifiers disagreed")

    p_f4 = sub.add_parser(
        "form4", help="read Form 4s and report open-market purchase clusters"
    )
    p_f4.add_argument("--date", type=_iso_date, metavar="YYYY-MM-DD",
                      help="last day to read (default: today)")
    p_f4.add_argument("--days", type=int, default=5, metavar="N",
                      help="how many days back to read (default 5)")
    p_f4.add_argument(
        "--insider-floor", type=Decimal, default=Decimal("50000"),
        metavar="USD",
        help="minimum cluster value for officer/director lists (default 50000)",
    )
    p_f4.add_argument(
        "--tenpct-floor", type=Decimal, default=Decimal("1000000"),
        metavar="USD",
        help="minimum cluster value for 10%%-holder lists (default 1000000). "
             "Two orders of magnitude above the insider floor because the "
             "populations are: sample-week medians were $339k and $26.4M.",
    )
    p_f4.add_argument("--window-days", type=int, default=3, metavar="N",
                      help="cluster window in days (default 3, i.e. 72h)")
    p_f4.add_argument("--min-buyers", type=int, default=2, metavar="N",
                      help="distinct buyers required (default 2)")
    p_f4.add_argument("--top", type=int, default=20, metavar="N",
                      help="clusters to print per list")
    p_f4.add_argument("--names", action="store_true",
                      help="list the buyers in each cluster")
    p_f4.add_argument("--no-load", action="store_true",
                      help="report only; do not store clusters in signals")

    # No publish flags: FRED is local-only. FRED redistributes the ICE BofA
    # series under permission, so they are not ours to republish, and there is
    # deliberately no switch that could turn that back on. See CLAUDE.md.
    sub.add_parser("fred", help="load Treasury yields and credit spreads from FRED")

    p_digest = sub.add_parser("digest", help="render the daily email")
    p_digest.add_argument("--dry-run", action="store_true", help="render, do not send")
    p_digest.add_argument(
        "--as-of", type=_iso_date, metavar="YYYY-MM-DD",
        help="trading day to render (default: newest in the data)",
    )
    p_digest.add_argument(
        "--top", type=int, default=10, metavar="N",
        help="rows per list in the email (default 10)",
    )
    p_digest.add_argument(
        "--all", dest="include_ungated", action="store_true",
        help="include the ungated lists as well as the >$5M ADV ones",
    )
    p_digest.add_argument(
        "--html", action="store_true", help="print the HTML body instead of text"
    )

    p_dash = sub.add_parser(
        "dashboard", help="render the local dashboard and open it"
    )
    p_dash.add_argument(
        "--out", type=Path, metavar="PATH",
        help="output file (default: .dashboard/index.html, gitignored)",
    )
    p_dash.add_argument(
        "--no-open", action="store_true", help="write the file, do not open it"
    )
    p_dash.add_argument(
        "--fast", action="store_true",
        help="panel map only -- skip the screens, which read prices from R2",
    )

    p_backfill = sub.add_parser("backfill", help="drain N queue items")
    p_backfill.add_argument("--budget", type=int, default=100, help="items to drain")

    p_selftest = sub.add_parser(
        "selftest", help="end-to-end pipeline check with synthetic data"
    )
    p_selftest.add_argument(
        "--inject-staleness",
        action="store_true",
        help="deliberately publish stale data; must exit non-zero",
    )

    sub.add_parser("manifest", help="show dataset locations and engine capabilities")

    p_migrate = sub.add_parser("migrate", help="apply SQL migrations to Supabase")
    p_migrate.add_argument(
        "--dry-run", action="store_true", help="list statements, execute nothing"
    )

    return parser


def _cmd_manifest() -> int:
    """Works offline, with no credentials. Useful as a first smoke test."""
    from marketradar import manifest, storage

    print(f"manifest: {manifest.manifest_path()}")
    refs = manifest.datasets()
    if not refs:
        print("  (no datasets defined)")
    width = max((len(f"{r.dataset}/{r.partition}") for r in refs), default=0)
    for ref in refs:
        label = f"{ref.dataset}/{ref.partition}"
        flag = "private" if ref.is_private else "public "
        print(f"  {label:<{width}}  {flag}  {ref.backend:<15} {ref.location}")

    con = storage.connect(enable_http=False)
    caps = storage.describe_connection(con)
    print("\nengine:")
    for key, value in caps.items():
        print(f"  {key:<16} {value}")
    return EXIT_OK


def _cmd_prices(args: argparse.Namespace) -> int:
    """Chunked, resumable Tiingo sweep."""
    import time
    from pathlib import Path as _Path

    from datetime import timedelta

    from marketradar import storage
    from marketradar.clock import market_today
    from marketradar.sources import tiingo

    # The trading date, not the UTC date. At 03:30 UTC the UTC calendar has
    # rolled over but the US market has not, so a UTC-derived date names a
    # session that has not happened -- and on January 1st it files December
    # 31st's bars into next year's immutable partition.
    end = args.date or market_today()
    start = args.start or (end - timedelta(days=args.days))
    partition = str(end.year)

    # Validate credentials before downloading anything. The universe zip is
    # several megabytes, and failing after fetching it wastes time and made a
    # unit test reach the network to discover a missing token.
    if not args.dry_run:
        tiingo._token()

    print(f"universe: fetching supported tickers (free, outside the request budget)")
    rows = tiingo.download_universe_rows()
    universe = tiingo.fetch_universe(rows=rows)
    universe_size = len(universe)
    print(f"  {universe_size:,} US listed stock/ETF symbols currently trading")

    # Listing periods, so every bar can be attributed to the company that
    # actually traded it. A ticker is not an entity across time.
    spans = tiingo.listing_spans(rows)
    recycled = sum(1 for t in universe if len(spans.get(t.ticker, [])) > 1)
    print(f"  {recycled:,} of them have carried more than one listing")

    if args.limit:
        universe = sorted(universe, key=lambda t: t.ticker)[: args.limit]
        print(f"  limited to {len(universe):,} for this run")

    run_id = f"{partition}-{start.isoformat()}-{end.isoformat()}-{len(universe)}"
    staging = _Path(".checkpoints") / f"staging-{run_id}"

    print(f"\nwindow  : {start} .. {end}")
    print(f"chunks  : {len(universe)} tickers / {args.chunk_size} per chunk")
    print(f"pacing  : {args.rate_per_hour:,} req/hour")
    if args.dry_run:
        print("\ndry run - no requests made")
        return EXIT_OK

    print()
    result = tiingo.sweep(
        universe,
        start,
        end,
        staging=staging,
        run_id=run_id,
        chunk_size=args.chunk_size,
        rate_per_hour=args.rate_per_hour,
        restart=args.restart,
        spans=spans,
    )

    print(f"\nattempted {result.attempted:,}  succeeded {result.succeeded:,}  "
          f"failed {result.failed:,}")
    print(f"rows {result.rows:,}  corporate actions {result.actions:,}")
    print(f"retries {result.retries}  429s {result.rate_limited}")
    print(f"wall time {result.elapsed:.1f}s")
    fetched_now = result.attempted - result.resumed_attempted
    if fetched_now and result.elapsed:
        per = result.elapsed / fetched_now
        if result.resumed_attempted:
            print(f"  ({fetched_now:,} fetched this process; "
                  f"{result.resumed_attempted:,} resumed from checkpoint)")
        print(f"per ticker {per:.3f}s")
        for label, n in (("active universe", universe_size), ("full 12,000", 12000)):
            print(f"  projected {label:<16} = {per * n / 60:6.1f} min "
                  f"({n:,} requests)")
    if result.failures:
        print(f"\nfirst failures ({len(result.failures)} total):")
        for ticker, why in result.failures[:5]:
            print(f"  {ticker}: {why}")

    if args.no_publish:
        print(f"\n--no-publish: {result.rows:,} rows staged in {staging}")
        return EXIT_OK

    con = storage.connect()

    # Bars that fall outside every known listing period. Not fatal -- the bar
    # is real and the gap guard in the screens still protects it -- but it
    # must never pass silently, because "unknown listing" collapsing into
    # "the first listing" is the exact failure this column exists to prevent.
    unattributed = int(
        con.execute(
            "SELECT count(*) FROM read_parquet(?) WHERE listing_id IS NULL",
            [(staging / "chunk_*.parquet").as_posix()],
        ).fetchone()[0]
    )
    if unattributed:
        share = unattributed / max(1, result.rows)
        print(f"\nWARNING: {unattributed:,} of {result.rows:,} bars "
              f"({share:.2%}) fall outside every known listing period and "
              "carry listing_id = NULL.", file=sys.stderr)

    # One publish per year present in staging. A sweep that crosses New Year
    # writes two partitions; a backfill writes as many as it spans.
    observed = tiingo.publish_all(staging, con=con, restate=args.restate)
    print()
    for obs in observed:
        print(f"published {obs.partition}: {obs.row_count:,} rows, "
              f"max_date {obs.max_date}")
    n = tiingo.upsert_corporate_actions(staging, con=con)
    print(f"corporate actions upserted: {n:,}")
    return EXIT_OK


def _cmd_sec_tickers(args: argparse.Namespace) -> int:
    from marketradar import storage
    from marketradar.sources import sec_company_tickers as sec

    print("fetching SEC company_tickers.json (one request, bulk file)")
    filers = sec.fetch()
    ciks = {f.cik for f in filers}
    print(f"  {len(filers):,} (cik, ticker) pairs across {len(ciks):,} filers")

    multi = len(filers) - len(ciks)
    print(f"  {multi:,} extra tickers from share classes")

    con = storage.connect()

    if args.reconcile:
        return _reconcile(con, filers)

    if not args.no_publish:
        observed = sec.publish(filers, con=con)
        print(f"\npublished {observed.row_count:,} rows to the GitHub Release")

    print("\nupserting into companies + company_tickers")
    stats = sec.load(filers, con=con)
    print(f"  companies : {stats['companies_before']:,} -> {stats['companies_after']:,} "
          f"(+{stats['companies_inserted']:,})")
    print(f"  tickers   : {stats['tickers_before']:,} -> {stats['tickers_after']:,} "
          f"(+{stats['tickers_inserted']:,})")
    return EXIT_OK


def _series_con(out_dir: str = ".cache/form5500"):
    """One DuckDB holding the multi-year participant series, or None.

    Built once and shared by both Form 5500 panels. Two separate builds would
    each stack every published year -- 858,480 sponsors a year -- to answer
    questions about a few hundred rows, and would be free to disagree with
    each other about who is lapsed.

    Returns ``(con, TrendBuild)``, or ``(None, None)`` when fewer than two
    plan years are published: one year is a valid state and a trend needs
    two, so the panels degrade to no trend rather than refusing to draw.
    """
    import duckdb as _dd

    from marketradar.sources import form5500

    paths = _sponsor_parquets(out_dir)
    if len(paths) < 2:
        return None, None
    con = _dd.connect()
    form5500.build_history(con, paths)
    return con, form5500.build_trend(con)


def _private_rows(limit: int = 400, series=None):
    """Top private sponsors by headcount, plus the plan year's shape.

    Read from the parquet rather than Postgres: 858,480 sponsors is a data
    file, and the manifest sends government data to a Release. The panel
    needs the head of the list, not the table.
    """
    from pathlib import Path as _PP

    import duckdb as _dd

    found = sorted(_PP(".cache/form5500").glob("form5500_sponsors_*.parquet"))
    if not found:
        return [], {}
    newest = found[-1]
    d = _dd.connect()
    d.register("spons", d.read_parquet(newest.as_posix()))
    stats = d.execute("""
        select min(plan_year), count(*),
               count(*) filter (where not by_ein and not name_matched),
               count(*) filter (where is_dfe),
               count(*) filter (where by_ein_listed),
               count(*) filter (where not by_ein and name_matched)
        from spons
    """).fetchone()
    # ein is selected so the trend can be joined on row by row. Running a
    # second query and zipping the two results would re-order ties
    # independently -- the same class of defect as picking a name with
    # any_value(), and just as invisible. `ein` also breaks the tie, so the
    # order is a rule rather than whatever the scan produced.
    cols = ("ein", "sponsor_name", "state", "naics", "plans",
            "participants_sum", "participants_max", "is_dfe")
    rows = d.execute(f"""
        select {', '.join(cols)} from spons
        where not by_ein
        order by participants_max desc, plans desc, ein limit {int(limit)}
    """).fetchall()
    out = [dict(zip(cols, r)) for r in rows]

    # The trend is a separate build over several plan years, and the panel
    # renders fine without it: one published year is a valid state, and a
    # panel that refused to draw until three existed would be worse than one
    # whose trend column says "one year".
    if series is not None:
        for row in out:
            row.update(_trend_for(series, row["ein"]))
    return (
        out,
        {"plan_year": stats[0], "sponsors": stats[1], "private": stats[2],
         "dfe": stats[3], "listed": stats[4], "ambiguous": stats[5],
         "completeness": f"Source file {newest.name}."},
    )


#: What the panels read out of the trend table. Named once so the private
#: panel and the mature-target panel cannot render different columns.
_TREND_COLS: Final[tuple[str, ...]] = (
    "trend", "status", "pct_change", "first_year", "last_year",
    "years_filed", "pending_years", "gap_years", "series",
    "common_plans", "plans_added", "plans_dropped",
    "matched_first", "matched_last",
)


def _trend_for(con, ein: str) -> dict:
    """One sponsor's series. Queried per row rather than pulled as a dict.

    The panel shows a few hundred sponsors out of 858,480, so materialising
    the whole table to look up the ones on screen is the wrong way round.
    """
    row = con.execute(
        f"select {', '.join(_TREND_COLS)} from f5500_trend where ein = ?",
        [ein],
    ).fetchone()
    return dict(zip(_TREND_COLS, row)) if row else {}


def _mature_rows(limit: int = 200, series=None, built=None):
    """Mature-target candidates, plus the population each filter left."""
    from marketradar.screens import mature_target

    if series is None:
        return [], {"years": tuple(sorted(_sponsor_parquets(".cache/form5500")))}

    targets = mature_target.candidates(series, limit=limit)
    rows = []
    for t in targets:
        row = {k: getattr(t, k) for k in (
            "ein", "sponsor_name", "naics", "city", "state", "oldest_plan_eff",
            "age_years", "participants_last", "participants_sum", "trend",
            "pct_change", "first_year", "last_year", "years_filed",
            "active_last", "active_sum", "is_multiemployer")}
        row.update(_trend_for(series, t.ein))
        rows.append(row)
    return rows, {
        "years": built.complete_years if built else (),
        "candidates": len(targets),
        "population": mature_target.population(series),
    }


def _xbrl_coverage(con: Any, out_dir: Path) -> dict[str, Any]:
    """Per-concept coverage, read back out of the newest stored partition.

    Recomputed from the rows rather than stored beside them, and that is only
    possible because **every filing gets a row carrying a status** -- the
    coverage report is a group-by over the partition, not a separate artifact
    that could disagree with it. A stored summary is a second copy of a number,
    and the two drift.

    The funnel cannot be recovered this way: it counts what was *excluded*, and
    excluded rows are not in the file. So the panel renders it when a load
    produced one in the same session and omits it otherwise, rather than
    inventing stages from what survived.
    """
    files = sorted(out_dir.glob("xbrl_fundamentals_*.parquet"))
    if not files:
        return {}
    newest = files[-1]
    quarter = newest.stem.rsplit("_", 1)[-1]
    from marketradar.sources.xbrl import tag_map

    population = int(con.execute(
        "select count(distinct adsh) from read_parquet(?)",
        [newest.as_posix()],
    ).fetchone()[0])
    rows = con.execute(
        "select concept, status, count(*) from read_parquet(?) group by 1, 2",
        [newest.as_posix()],
    ).fetchall()
    tags = con.execute(
        "select concept, unmapped_tag, count(*) n from read_parquet(?) "
        "where status = ? and unmapped_tag is not null group by 1, 2 "
        "order by 1, n desc, 2",
        [newest.as_posix(), tag_map.UNMAPPED],
    ).fetchall()

    by_concept: dict[str, dict[str, int]] = {}
    for concept, status, count in rows:
        by_concept.setdefault(concept, {})[status] = int(count)
    queues: dict[str, list[tuple[str, int]]] = {}
    for concept, tag, n in tags:
        queues.setdefault(concept, []).append((tag, int(n)))

    coverage = []
    for concept, statuses in by_concept.items():
        resolved = statuses.get(tag_map.STATED, 0)
        rate = resolved / population if population else 0.0
        declared = tag_map.CONCEPTS.get(concept)
        coverage.append({
            "concept": concept,
            "population": population,
            "resolved": resolved,
            "rate": rate,
            "drift": None if declared is None
                     else rate - declared.coverage_2024q1,
            "by_status": {s: statuses.get(s, 0) for s in tag_map.STATUSES},
            "by_tag": {},
            "unmapped_tags": queues.get(concept, []),
        })
    return {"quarter": quarter, "coverage": coverage}


def _cmd_xbrl(args: argparse.Namespace) -> int:
    """Normalize quarters of SEC fundamentals, printing the coverage report.

    The coverage report *is* the deliverable, not a side effect. A concept that
    resolves for 51% of filers and one that resolves for 99% look identical
    downstream -- both are a number in a column -- so the funnel and the
    per-concept figures print every run, the same way a screen prints its own.
    """
    import duckdb

    from marketradar.sources.xbrl import resolve as xbrl

    out = Path(args.out)
    cache = Path(args.cache) if args.cache else None
    concepts = tuple(args.concept) if args.concept else None
    worst = 0.0
    for quarter in args.quarter:
        con = duckdb.connect()
        # A quarter is ~600k facts and the order they land in is never read.
        con.execute("set preserve_insertion_order=false")
        if args.no_load:
            _, result = xbrl.build(quarter, con=con, cache=cache,
                                   concepts=concepts)
        else:
            result = xbrl.load(quarter, out, con=con, cache=cache,
                               concepts=concepts)
        for line in result.lines():
            print(line)
        if result.target:
            print()
            print(f"wrote {result.target}")
        print()
        for cov in result.coverage:
            if cov.drift is not None:
                worst = min(worst, cov.drift)

    # Drift is the thing that rots quietly: the map is hand-maintained and
    # baseline tag churn between sampled years ran 11-20%. Said out loud rather
    # than left in a column nobody reads.
    if worst < -0.01:
        print(f"note: a concept is {abs(worst):.1%} below the coverage the tag "
              "map records. The map may need a tag -- the unmapped counts "
              "above name which.", file=sys.stderr)
    return EXIT_OK


def _cmd_dashboard(args: argparse.Namespace) -> int:
    """Render the panel map to a local file and open it.

    Local only, and there is no publish path anywhere in the module: the
    screens are computed from Tiingo prices, so a public host would be
    redistribution. See docs/build-spec.md, "The UI track".
    """
    from marketradar import storage
    from marketradar.dashboard import shell

    con = storage.connect()
    ctx = shell.gather(con)

    # The digest is the single reader for health, macro and the screens, so
    # the two surfaces cannot disagree about what today's moves were. If it
    # cannot be built the shell still renders -- a dashboard that refuses to
    # draw because R2 is unreachable is worse than one that opens with the
    # panel saying so.
    digest = None
    if not args.fast:
        from marketradar import digest as digest_mod

        try:
            digest = digest_mod.build(con, include_ungated=True)
        except Exception as exc:
            ctx.notes.append(f"Screens unavailable: {str(exc)[:160]}")
            print(f"  screens unavailable: {str(exc)[:120]}", file=sys.stderr)

    details = None
    if digest is not None and not args.fast:
        from marketradar.dashboard import tickers
        from marketradar.screens import volatility

        try:
            names = {m.ticker for sl in digest.screen.lists for m in sl.rows}
            details = tickers.build(
                con, volatility.read_prices(con, digest.day), names,
                actions=volatility.read_actions(con),
            )
        except Exception as exc:
            ctx.notes.append(f"Ticker detail unavailable: {str(exc)[:140]}")

    # One build, both panels. A panel that cannot be drawn says so in the
    # shell rather than taking the whole page down with it.
    series = built = None
    try:
        series, built = _series_con()
    except Exception as exc:
        ctx.notes.append(f"Participant series unavailable: {str(exc)[:140]}")
    # U10. A panel that cannot be drawn says so in the shell rather than
    # taking the page down, the same as every other body here.
    try:
        ctx.xbrl = _xbrl_coverage(con, Path(".cache/xbrl/out"))
    except Exception as exc:
        ctx.notes.append(f"XBRL coverage unavailable: {str(exc)[:140]}")

    private, private_stats = _private_rows(series=series)
    try:
        mature, mature_stats = _mature_rows(series=series, built=built)
    except Exception as exc:
        mature, mature_stats = [], {}
        ctx.notes.append(f"Mature targets unavailable: {str(exc)[:140]}")
    target = shell.write(args.out, ctx=ctx, digest=digest,
                         details=details, private=private,
                         private_stats=private_stats,
                         mature=mature, mature_stats=mature_stats)
    counts = shell.summary(ctx)

    print(f"wrote {target}")
    # Every state, read off shell.STATES rather than listed here: a state the
    # shell grew and this line did not would otherwise go uncounted, which is
    # the drift the shell spent a commit closing.
    print("  " + ", ".join(f"{counts[s]} {s}" for s in shell.STATES))
    marks = {shell.LIVE: "+", shell.WAITING: "~", shell.NOT_BUILT: ".",
             shell.DECLINED: "x"}
    for panel in shell.PANELS:
        state, detail = panel.resolve(ctx)
        print(f"  {marks[state]} {panel.title:<24} {state:<10} {detail[:52]}")

    if not args.no_open and shell.open_in_browser(target):
        print("opened in your browser")
    return EXIT_OK


def _outcome_prices(con):
    """Every price partition, not just the ones a screen needs."""
    from marketradar import storage
    from marketradar.screens.volatility import DATASET

    rels = []
    for year in range(2016, _dt.date.today().year + 1):
        try:
            rels.append(storage.read_dataset(DATASET, year, con=con))
        except Exception:
            continue
    if not rels:
        raise RuntimeError("no price partitions are readable")
    rel = rels[0]
    for other in rels[1:]:
        rel = rel.union(other)
    return rel


def _cluster_events(con, path: str | None):
    """Form 4 clusters as events, anchored on when they became public.

    Anchored on the *filing* date, never the transaction date. A Form 4 is
    due two business days after the trade, so scoring returns from the day
    the insider bought would measure a return nobody could have earned. That
    is the single easiest way to make this signal look better than it is.
    """
    from pathlib import Path

    default = Path(".cache/form4_clusters.parquet")
    source = Path(path) if path else default
    if source.exists():
        return con.sql(f"""
            select event_id, symbol as ticker, visible_on as event_date,
                   role,
                   case when fund_like then 'fund-like' else 'people' end
                       as flag,
                   case when value >= 1000000 then 'over $1M'
                        else 'under $1M' end as size
            from read_parquet('{source.as_posix()}')
            where symbol is not null and visible_on is not null
        """)

    # No history file: score whatever the nightly job has stored. Far fewer
    # events, and the answer will be correspondingly weak -- said out loud
    # rather than left for the reader to infer from a small n.
    print("  (no cluster history file; scoring the live signals table only)")
    return con.sql("""
        select accession as event_id,
               payload->>'symbol' as ticker,
               cast(payload->>'last' as date) as event_date,
               payload->>'role' as role,
               case when (payload->>'fund_like')::boolean then 'fund-like'
                    else 'people' end as flag,
               case when (payload->>'value')::double >= 1000000
                    then 'over $1M' else 'under $1M' end as size
        from postgres_query('pg', '
            select accession, payload::text as payload from signals
            where kind = ''form4_cluster''')
        where payload->>'symbol' is not null
    """)


def _deal_events(con):
    """8-K deal candidates as events, joined to a ticker."""
    # companies.cik is zero-padded and deals.cik is not, and a company can
    # hold several tickers at once -- joining through company_tickers fanned
    # 10,684 deals out into 13,686 rows, which would have weighted those
    # events several times over in every median below.
    return con.sql("""
        select event_id, ticker, event_date, deal_type, confidence
        from (
            select d.accession as event_id, c.ticker,
                   d.filed_date as event_date, d.deal_type,
                   case when d.classifiers_agree then 'both agree'
                        else 'review' end as confidence,
                   row_number() over (partition by d.accession
                                      order by c.id) as rn
            from postgres_query('pg', '
                select accession, cik, filed_date, deal_type, classifiers_agree
                from deals') d
            join postgres_query('pg', '
                select id, cik, ticker from companies
                where cik is not null and ticker is not null') c
              on ltrim(c.cik, '0') = d.cik
        )
        where rn = 1
    """)


def _cmd_form5500(args: argparse.Namespace) -> int:
    """Load DOL Form 5500 for one plan year and resolve sponsors by EIN.

    Both forms. Resolution is an exact EIN join -- names are compared only to
    populate the review queue, because normalized-name matching scores 44.2%
    precision against EIN ground truth and a matcher wrong more than half the
    time is worse than none.
    """
    from pathlib import Path as _P

    import duckdb as _ddb

    from marketradar import storage
    from marketradar.sources import form5500

    cache = _P(args.cache)
    work = cache / "dol"
    print(f"plan year {args.year}: fetching both forms")
    archives = form5500.fetch(args.year, cache)
    for a in archives:
        print(f"  {a.kind:<6} {a.path.name}  "
              f"{a.path.stat().st_size / 1e6:.1f} MB")

    con = _ddb.connect()
    filings = form5500.load_filings(con, archives, work)
    print(f"\n{filings:,} filings")
    for form, n, eins in con.execute(
        "select form, count(*), count(distinct ein) from f5500_filings "
        "group by form order by form"
    ).fetchall():
        print(f"  {form:<6} {n:>9,} filings  {eins:>8,} distinct EINs")

    complete, why = form5500.completeness(
        con, args.year, filings, baseline=args.baseline)
    print(f"  {'complete' if complete else 'PARTIAL'}: {why}")

    sponsors = form5500.build_sponsors(con)
    print(f"\n{sponsors:,} distinct sponsors, keyed on EIN")

    # SEC filers, for the EIN join. The EIN comes from the bulk submissions
    # file; companies.ein is empty, which is why this is a parquet and not a
    # table read.
    filers_path = cache / "sec_eins.parquet"
    if not filers_path.exists():
        print(f"\n{filers_path} is missing -- cannot resolve without SEC EINs.",
              file=sys.stderr)
        return EXIT_ERROR
    filers = con.read_parquet(filers_path.as_posix())
    res = form5500.resolve(con, filers)

    print(f"\nresolution, plan year {res.plan_year}")
    print(f"  sponsors                    {res.sponsors:>9,}")
    print(f"  EIN match to an SEC filer   {res.by_ein:>9,}  "
          f"{res.by_ein / res.sponsors * 100:5.2f}%")
    print(f"  ...of those, listed         {res.by_ein_listed:>9,}  "
          f"{res.by_ein_listed / res.sponsors * 100:5.2f}%")
    print(f"  name matched, EIN did not   {res.name_only:>9,}  "
          f"{res.name_only / res.sponsors * 100:5.2f}%  -> review queue")
    print(f"  private (no match at all)   {res.private:>9,}  "
          f"{res.private_share * 100:5.2f}%  <- the population")
    print(f"  DFE filers (flagged)        {res.dfe:>9,}  "
          f"{res.dfe / res.sponsors * 100:5.2f}%  trustees, not employers")

    if args.top_naics:
        print("\ntop NAICS among the private population:")
        for code, c, part in con.execute("""
            select naics, count(*) as c, sum(participants_max) as p
            from f5500_resolved
            where not by_ein and not is_dfe and naics is not null
            group by 1 order by 2 desc limit ?
        """, [args.top_naics]).fetchall():
            print(f"  {code}  {c:>8,} sponsors  {int(part or 0):>10,} participants")

    if args.no_load:
        print("\n--no-load: nothing stored, nothing written")
        return EXIT_OK

    out = form5500.publish(con, args.year, _P(args.out))
    print(f"\npublished {out.partition}: {out.row_count:,} sponsors")

    rows = form5500.review_rows(con)
    stats = form5500.load_review_queue(
        rows, con=storage.connect(attach_postgres=True))
    print(f"review queue: {stats['inserted']:,} new, "
          f"{stats['pending']:,} pending, {stats['after']:,} total")
    print("  decided rows are never reopened by a re-run")
    return EXIT_OK


def _sponsor_parquets(out_dir, years=None) -> dict:
    """Published sponsor files on disk, keyed by plan year.

    Reads whatever has been published rather than requiring a fixed set:
    the series is built from the years that exist, and
    :func:`form5500.build_trend` refuses outright if none of them is complete.
    """
    from pathlib import Path as _PP

    found = {}
    for path in sorted(_PP(out_dir).glob("form5500_sponsors_*.parquet")):
        try:
            year = int(path.stem.rsplit("_", 1)[1])
        except (IndexError, ValueError):
            continue
        if years and year not in years:
            continue
        found[year] = path
    return found


def _cmd_targets(args: argparse.Namespace) -> int:
    """Rank mature private employers from the multi-year participant series.

    Age is a floor and headcount is a range, so every line prints both the
    bound and its direction. The one thing this must never do is read a
    sponsor's absence from a later plan year as a headcount decline; that
    distinction lives in build_trend and the screen only consumes it.
    """
    import duckdb as _ddb

    from marketradar.screens import mature_target
    from marketradar.sources import form5500

    paths = _sponsor_parquets(args.out, args.years)
    if not paths:
        print(f"No sponsor parquets in {args.out}. Run `mr form5500 --year "
              "2024` (and 2023, 2022) first.", file=sys.stderr)
        return EXIT_ERROR

    con = _ddb.connect()
    rows = form5500.build_history(con, paths)
    print(f"{rows:,} sponsor-years from {len(paths)} plan years")
    for shape in form5500.series_shape(con):
        mark = " " if shape.complete else "*"
        print(f"  {mark}{shape.plan_year}  {shape.sponsors:>9,} sponsors  "
              f"{shape.note}")
    built = form5500.build_trend(con)
    print(f"\n{built}")

    counts = dict(con.execute(
        "select trend, count(*) from f5500_trend group by 1 order by 2 desc"
    ).fetchall())
    status = dict(con.execute(
        "select status, count(*) from f5500_trend group by 1 order by 2 desc"
    ).fetchall())
    print("  trend :", ", ".join(f"{k} {v:,}" for k, v in counts.items()))
    print("  status:", ", ".join(f"{k} {v:,}" for k, v in status.items()))
    print("  a lapsed sponsor is a question, never a decline")

    kw = {}
    if args.min_age is not None:
        kw["min_age_years"] = args.min_age
    if args.min_participants is not None:
        kw["min_participants"] = args.min_participants
    if args.max_participants is not None:
        kw["max_participants"] = args.max_participants

    pop = mature_target.population(con)
    print("\npopulation, filter by filter:")
    for label, n in pop.items():
        print(f"  {label:<16} {n:>9,}")

    kw["sort"] = args.sort
    kw["nonprofits"] = args.nonprofits
    targets = mature_target.candidates(con, **kw)
    if args.state:
        targets = [t for t in targets if (t.state or "") == args.state.upper()]
    if args.naics:
        targets = [t for t in targets
                   if (t.naics or "").startswith(args.naics)]

    if targets:
        # The concentration is what says whether the pool is the right
        # population at all. Sorted by age the head of the list is old
        # institutions, which is the sort working rather than the screen
        # failing -- colleges are 2.9% of the pool, and the bulk is physician
        # offices, law firms, dealerships, machine shops and small banks.
        from collections import Counter as _Counter

        mix = _Counter((t.naics or "??") for t in targets)
        print("\ncandidates by NAICS:")
        for code, n in mix.most_common(10):
            print(f"  {code}  {n:>6,}  {n / len(targets):>5.1%}")

    print(f"\n{len(targets):,} candidates"
          + (f", showing {min(args.top, len(targets))}" if targets else ""))
    for t in targets[:args.top]:
        print(f"  {t.sponsor_name[:40]:<40} "
              f"{(t.state or '--'):<3} {(t.naics or '------'):<6} "
              f"{t.headcount_note:>13}  {t.age_note}")
        print(f"         {t.trend:<10} "
              f"{'' if t.pct_change is None else f'{t.pct_change:+.1%}'} "
              f"over {t.first_year}-{t.last_year} "
              f"({t.years_filed} years filed)")

    if targets:
        summary = mature_target.summarise(targets)
        print(f"\n  median age floor {summary['median_age']:.0f}y, oldest "
              f"plan {summary['oldest']}")
        print("  age is a floor: the company is at least this old, and the "
              "plan can only be younger than the firm")
    return EXIT_OK


def _cmd_actions_audit(args: argparse.Namespace) -> int:
    """Moves the action table cannot account for.

    Two causes, and the detector does not care which: our upsert silently
    dropped 90% of what it staged (fixed), and Tiingo's per-bar splitFactor
    misses splits outright on small tickers (not fixable on this plan). A
    large one-session move with no action to explain it is a candidate
    missing split either way.
    """
    from decimal import Decimal as _D

    from marketradar import storage
    from marketradar.screens import action_audit, volatility

    con = storage.connect(attach_postgres=True)
    prices = _outcome_prices(con)
    actions = volatility.read_actions(con)

    since = None
    if args.days:
        from marketradar.clock import market_today

        since = market_today() - _dt.timedelta(days=args.days)
        print(f"scanning sessions since {since}")
    else:
        print("scanning all history")

    found = action_audit.candidates(
        con, prices, actions,
        jump=_D(args.jump), fall=_D(args.fall), since=since,
    )
    con.register("audit_rel", found)
    con.execute("drop table if exists audit_rows")
    con.execute("create table audit_rows as select * from audit_rel")
    rows = con.execute("select * from audit_rows").fetchall()

    for line in action_audit.funnel(con, prices,
                                    con.table("audit_rows")).lines():
        print(line)
    print()
    stats = action_audit.summarize(con, con.table("audit_rows"))
    print()
    print(action_audit.health_line(stats))
    print()
    print(action_audit.render(rows, limit=args.top))
    return EXIT_OK


def _cmd_outcomes(args: argparse.Namespace) -> int:
    """Forward returns after an event. Pure SQL, no LLM.

    This is the cheapest question in the system and the one every expensive
    thing depends on: if a signal has not historically preceded anything,
    nothing built on top of it can. So it runs before the embeddings, not
    after them.
    """
    from marketradar import storage
    from marketradar.screens import outcomes, volatility

    con = storage.connect(attach_postgres=True)
    print("reading price history ...")
    prices = _outcome_prices(con)
    actions = volatility.read_actions(con)
    con.register("outcome_px", prices)
    bars = con.execute("select count(*) from outcome_px").fetchone()[0]
    print(f"  {bars:,} bars")

    studies = []
    if args.study in ("form4", "both"):
        studies.append(("form4_cluster", "FORM 4 PURCHASE CLUSTERS",
                        _cluster_events(con, args.clusters),
                        ("role", "flag", "size")))
    if args.study in ("deals", "both"):
        studies.append(("deal_8k", "8-K DEAL CANDIDATES",
                        _deal_events(con), ("deal_type", "confidence")))

    for key, title, events, groups in studies:
        con.register("ev_in", events)
        n_events = con.execute("select count(*) from ev_in").fetchone()[0]
        if not n_events:
            print(f"\n{title}: no events")
            continue

        res = outcomes.forward_returns(
            con, events, prices, actions, benchmark=args.benchmark)
        con.register("res_in", res)
        # Materialise: every slice below would otherwise re-read the
        # partitions from R2.
        con.execute("drop table if exists scored")
        con.execute("create table scored as select * from res_in")
        scored = con.table("scored")
        for line in outcomes.funnel(con, events, scored).lines():
            print(line)
        print()
        cov = outcomes.coverage(con, events, scored)

        print()
        print("=" * 78)
        print(title)
        print("=" * 78)
        print(f"  events {cov['events']:,}   priced {cov['priced']:,} "
              f"({cov['priced'] / max(1, cov['events']) * 100:.0f}%)"
              f"   benchmark {args.benchmark}")

        overall = outcomes.summarize(con, scored)
        print()
        print(outcomes.render(overall, "all events"))
        rows = list(overall)
        for group in groups:
            sliced = outcomes.summarize(con, scored, group_by=group)
            print()
            print(outcomes.render(sliced, f"by {group}"))
            rows.extend(sliced)

        if not args.no_load:
            n = outcomes.persist(
                con, key, rows, events=cov["events"], priced=cov["priced"],
                benchmark=args.benchmark)
            print(f"\n  stored {n} summary rows")

    if args.no_load:
        print("\n--no-load: nothing stored")
    return EXIT_OK


def _cmd_deals(args: argparse.Namespace) -> int:
    """Extract deal candidates from 8-K Items 1.01 and 2.01.

    Item 1.01 is mostly not M&A -- measured at roughly 16% over a full week
    of filings -- so the output is a candidate list with both classifiers
    shown, not a deal list. Rows where the exhibit and the text disagree are
    the review queue and are marked rather than dropped.
    """
    from datetime import timedelta

    from marketradar.clock import market_today
    from marketradar.signals import deals

    end = args.date or market_today()
    start = end - timedelta(days=args.days - 1)
    print(f"reading 8-K filings {start} to {end}")

    found = deals.fetch(start, end, read_exhibits=not args.no_exhibits)
    if not found:
        print("no deal candidates in that window")
        return EXIT_OK

    agree = [d for d in found if d.classifiers_agree]
    review = [d for d in found if not d.classifiers_agree]
    by_type: dict[str, int] = {}
    for d in found:
        by_type[d.deal_type] = by_type.get(d.deal_type, 0) + 1

    print(f"\n{len(found)} candidates: {len(agree)} both classifiers agree, "
          f"{len(review)} for review")
    print("  " + "  ".join(f"{k}={v}" for k, v in sorted(by_type.items())))

    priced = [d for d in found if d.value_usd is not None]
    print(f"  value stated: {len(priced)}/{len(found)}"
          f" ({len(priced) / len(found) * 100:.0f}%)"
          f"; from an exhibit: "
          f"{sum(1 for d in priced if d.value_basis == 'stated_exhibit')}")
    promised = sum(1 for d in found if d.target_financials == "rule_305_promised")
    print(f"  target financials promised under Rule 3-05: {promised}/{len(found)}")

    shown = review if args.review else found
    print()
    for d in sorted(shown, key=lambda d: (d.value_usd or 0), reverse=True):
        flag = "  " if d.classifiers_agree else "??"
        money = f"${d.value_usd:,.0f}" if d.value_usd is not None else d.value_basis
        print(f"{flag} {d.filed_date}  {d.company[:30]:30s} {d.deal_type:14s} "
              f"{d.consideration:10s} {money:>18s}")
        print(f"     items {d.items}  exhibit={'Y' if d.exhibit_signal else 'n'} "
              f"text={d.text_signal or '-'}  role={d.filer_role}"
              + (f"  vs {d.counterparty[:40]}" if d.counterparty else ""))

    if args.no_load:
        print("\n--no-load: nothing stored")
        return EXIT_OK

    stats = deals.load(found)
    print(f"\nstored: {stats['inserted']} new, "
          f"{stats['candidates'] - stats['inserted']} updated, "
          f"{stats['after']} total")
    return EXIT_OK


def _cmd_form4(args: argparse.Namespace) -> int:
    """Read Form 4s from the daily index and report purchase clusters.

    Two lists, kept apart. The floors differ by two orders of magnitude
    because the populations do: in a sample week the median officer/director
    cluster was $339k and the median 10%-holder cluster $26.4M. They are
    arguments rather than constants because one week is a hypothesis.
    """
    from datetime import timedelta

    import httpx

    from marketradar.clock import market_today
    from marketradar.signals import form4
    from marketradar.signals.edgar_rss import Pacer

    end = args.date or market_today()
    days = [end - timedelta(days=n) for n in range(args.days)]

    client = httpx.Client(timeout=60.0, follow_redirects=True)
    pacer = Pacer(8.0)
    paths: list[str] = []
    for day in sorted(days):
        got = form4.daily_index_paths(day, client=client)
        if got:
            print(f"  {day}: {len(got):,} filings")
        paths.extend(got)
    print(f"reading {len(paths):,} Form 4s (4 and 4/A)")

    filings, failed = [], 0
    for accession, text in form4.fetch_documents(paths, client=client, pacer=pacer):
        try:
            filings.append(form4.parse(text, accession=accession))
        except form4.Form4Error:
            failed += 1
    client.close()

    kept = form4.supersede(filings)
    print(f"  parsed {len(filings):,} (failed {failed}); "
          f"{len(filings) - len(kept):,} superseded by a 4/A")

    found = form4.clusters(
        kept,
        insider_floor=args.insider_floor,
        tenpct_floor=args.tenpct_floor,
        window_days=args.window_days,
        min_buyers=args.min_buyers,
    )

    if not args.no_load:
        from marketradar import storage

        stats = form4.load(found, con=storage.connect())
        print(f"\nsignals: {stats['before']:,} -> {stats['after']:,} clusters "
              f"(+{stats['inserted']:,}); stored unfiltered, floors are display")

    for role, floor in ((form4.INSIDER, args.insider_floor),
                        (form4.TEN_PERCENT, args.tenpct_floor)):
        rows = found[role]
        label = "officer / director" if role == form4.INSIDER else "10% holders"
        print(f"\n--- {label}: {len(rows)} clusters "
              f"(>= {args.min_buyers} buyers, >= ${floor:,.0f}, "
              f"{args.window_days}d window) ---")
        if not rows:
            print("    (none)")
            continue
        print(f"    {'issuer':<10} {'cik':<12} {'window':<24} {'buyers':>6} "
              f"{'value':>16}  flags")
        flagged = 0
        for c in rows[: args.top]:
            marks = []
            if c.planned_buys:
                marks.append(f"{c.planned_buys} planned")
            is_fund, why = c.fund_flag
            if is_fund:
                flagged += 1
                marks.append("FUND")
            flags = ", ".join(marks)
            window = (f"{c.first}" if c.first == c.last
                      else f"{c.first} .. {c.last}")
            print(f"    {(c.symbol or '-'):<10} {c.issuer_cik:<12} {window:<24} "
                  f"{len(c.buyers):>6} ${c.value:>15,.0f}  {flags}")
            if is_fund:
                print(f"      ^ fund-like: {why}")
            if args.names:
                print(f"      {', '.join(c.names)[:96]}")
        if flagged:
            print(f"    {flagged} of {min(len(rows), args.top)} marked FUND -- "
                  "funds accumulating each other. Marked, never dropped: it is "
                  "real information, just not insider conviction.")
    return EXIT_OK


def _cmd_edgar(args: argparse.Namespace) -> int:
    """Poll the current-filings feed and land everything in signals.

    A tripwire, not a history: the feed holds only the last few hundred
    filings across all of EDGAR, so this wants running often. Reading a week
    back is a job for the daily index files.
    """
    from collections import Counter

    from marketradar import storage
    from marketradar.signals import edgar_rss

    forms = tuple(args.forms) if args.forms else edgar_rss.FORM_TYPES
    print(f"polling EDGAR current filings: {', '.join(forms)}")
    filings = edgar_rss.fetch(form_types=forms)
    print(f"  {len(filings):,} filings in the current window")
    for form, n in Counter(f.form_type for f in filings).most_common():
        print(f"    {form:<10} {n:>4}")

    if args.no_load:
        print("\n--no-load: nothing stored")
        return EXIT_OK

    stats = edgar_rss.load(filings, con=storage.connect())
    print(f"\nsignals: {stats['before']:,} -> {stats['after']:,} "
          f"(+{stats['inserted']:,} new)")
    return EXIT_OK


def _cmd_fred(args: argparse.Namespace) -> int:
    """Treasury yields and credit spreads. Three requests, no budget.

    Stores to Postgres only. There is no publish step and no flag to add one:
    FRED carries the ICE BofA series under permission from ICE Data Indices,
    LLC, so they are not ours to mirror.
    """
    from marketradar import storage
    from marketradar.sources import fred

    print("fetching FRED series (one request each)")
    observations = fred.fetch()
    by_series: dict[str, int] = {}
    for o in observations:
        by_series[o.series_id] = by_series.get(o.series_id, 0) + 1
    for s in fred.SERIES:
        print(f"  {s.series_id:<14} {by_series.get(s.series_id, 0):>6,} observations"
              f"  ({s.label})")

    con = storage.connect()
    print("\nupserting into macro_series (local only, not published)")
    stats = fred.load(observations, con=con)
    print(f"  rows: {stats['rows_before']:,} -> {stats['rows_after']:,} "
          f"(+{stats['rows_inserted']:,})")

    print("\nlatest:")
    for series_id, obs in fred.latest(con).items():
        series = fred.BY_ID[series_id]
        print(f"  {series.label:<14} {obs.value}{series.units}  as of {obs.obs_date}")
    return EXIT_OK


def _cmd_digest(args: argparse.Namespace) -> int:
    """Render the daily email, and send it unless --dry-run."""
    from marketradar import digest as digest_mod
    from marketradar import storage
    from marketradar.freshness import StaleDataError

    con = storage.connect()
    try:
        digest = digest_mod.build(
            con, as_of=args.as_of, top_n=args.top,
            include_ungated=args.include_ungated,
        )
    except (digest_mod.DigestError, StaleDataError) as exc:
        print(f"mr digest: {exc}", file=sys.stderr)
        return EXIT_ERROR

    body = (
        digest_mod.render_html(digest) if args.html else digest_mod.render_text(digest)
    )
    print(body)

    try:
        outcome = digest_mod.send(digest, dry_run=args.dry_run)
    except digest_mod.DigestError as exc:
        print(f"\nmr digest: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if outcome["sent"]:
        print(f"\nsent to {', '.join(outcome['to'])} from {outcome['from']}")
    else:
        print(f"\ndry run - nothing sent. subject would be: {outcome['subject']!r}")
    return EXIT_OK


def _cmd_screens(args: argparse.Namespace) -> int:
    """Volatility screens for one trading day.

    Reads prices through the manifest and corporate_actions from Postgres,
    adjusts at query time, and prints the lists. Nothing is written: a screen
    is a view of stored data, so re-running it is always safe.
    """
    from marketradar import storage
    from marketradar.freshness import StaleDataError
    from marketradar.screens import volatility

    con = storage.connect()
    try:
        result = volatility.screen(
            con,
            as_of=args.as_of,
            top_n=args.top,
            sanity_floor=args.sanity_floor,
            adjust_dividends=args.adjust_dividends,
        )
    except (volatility.ScreenError, StaleDataError) as exc:
        print(f"mr screens: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if args.summary:
        print(f"volatility screen for {result.day.isoformat()}")
        print(f"{result.moves_screened:,} moves screened, "
              f"{result.floor_excluded:,} below the ${result.sanity_floor} floor")
        print()
    # The funnel first, always -- before any list. A short list is either
    # selective or broken, and these counts are what tells you which.
    for line in volatility.funnel(result).lines():
        print(line)
    print()
    if args.summary:
        for line in volatility.summary(result.lists):
            print(line)
        return EXIT_OK

    for line in volatility.render(result, show_empty=args.show_empty):
        print(line)
    return EXIT_OK


def _reconcile(con, filers) -> int:
    """Coverage against the Tiingo sweep universe, decomposed.

    The counting and classification live in ``entities/reconcile.py`` so they
    can be tested without a network. This function only fetches and prints.

    One download, two views of it: the filtered sweep universe answers "would
    we price this", and the unfiltered file answers "does Tiingo have it at
    all". Without the second view a symbol our own OTC filter drops looks
    identical to one Tiingo lacks, which is the difference between a config
    change and manual entity work.
    """
    from marketradar.entities import reconcile
    from marketradar.sources import tiingo

    print("\nfetching Tiingo universe for reconciliation")
    rows = tiingo.download_universe_rows()
    active = tiingo.fetch_universe(rows=rows)
    distinct = len({t.ticker for t in active})
    print(f"  {len(rows):,} rows in the file, {len(active):,} pass the sweep "
          f"filter ({distinct:,} distinct tickers; a few dual-list)")

    for line in reconcile.render(reconcile.build(filers, active, rows)):
        print(line)
    return EXIT_OK


def _not_implemented(command: str, weekend: str) -> int:
    print(
        f"mr {command}: not implemented yet (scheduled for {weekend}).",
        file=sys.stderr,
    )
    return EXIT_NOT_IMPLEMENTED


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.command is None:
        parser.print_help()
        return EXIT_OK

    if args.command == "manifest":
        return _cmd_manifest()

    if args.command == "prices":
        load_dotenv()
        try:
            return _cmd_prices(args)
        except Exception as exc:  # surfaced with a message, not a traceback
            from marketradar.freshness import StaleDataError

            label = "STALE DATA" if isinstance(exc, StaleDataError) else "error"
            print(f"mr prices: {label}: {exc}", file=sys.stderr)
            return EXIT_ERROR

    if args.command == "sec-tickers":
        load_dotenv()
        try:
            return _cmd_sec_tickers(args)
        except Exception as exc:
            from marketradar.freshness import StaleDataError

            label = "STALE DATA" if isinstance(exc, StaleDataError) else "error"
            print(f"mr sec-tickers: {label}: {exc}", file=sys.stderr)
            return EXIT_ERROR

    if args.command == "migrate":
        from marketradar import migrate

        load_dotenv()
        try:
            return migrate.run(dry_run=args.dry_run)
        except migrate.MigrationError as exc:
            print(f"mr migrate: {exc}", file=sys.stderr)
            return EXIT_ERROR

    if args.command == "selftest":
        from marketradar import selftest

        load_dotenv()
        try:
            return selftest.run(inject_staleness=args.inject_staleness)
        except selftest.SelftestError as exc:
            print(f"mr selftest: {exc}", file=sys.stderr)
            return EXIT_ERROR

    if args.command == "screens":
        load_dotenv()
        return _cmd_screens(args)

    if args.command == "dashboard":
        load_dotenv()
        return _cmd_dashboard(args)

    if args.command == "form4":
        load_dotenv()
        try:
            return _cmd_form4(args)
        except Exception as exc:
            print(f"mr form4: error: {exc}", file=sys.stderr)
            return EXIT_ERROR

    if args.command == "form5500":
        load_dotenv()
        try:
            return _cmd_form5500(args)
        except Exception as exc:
            from marketradar.freshness import StaleDataError

            label = "STALE DATA" if isinstance(exc, StaleDataError) else "error"
            print(f"mr form5500: {label}: {exc}", file=sys.stderr)
            return EXIT_ERROR

    if args.command == "xbrl":
        load_dotenv()
        try:
            return _cmd_xbrl(args)
        except StaleDataError as exc:
            print(f"mr xbrl: STALE DATA: {exc}", file=sys.stderr)
            return EXIT_ERROR
        except Exception as exc:
            print(f"mr xbrl: error: {exc}", file=sys.stderr)
            return EXIT_ERROR

    if args.command == "targets":
        load_dotenv()
        try:
            return _cmd_targets(args)
        except Exception as exc:
            print(f"mr targets: error: {exc}", file=sys.stderr)
            return EXIT_ERROR

    if args.command == "actions-audit":
        load_dotenv()
        try:
            return _cmd_actions_audit(args)
        except Exception as exc:
            print(f"mr actions-audit: error: {exc}", file=sys.stderr)
            return EXIT_ERROR

    if args.command == "outcomes":
        load_dotenv()
        try:
            return _cmd_outcomes(args)
        except Exception as exc:
            print(f"mr outcomes: error: {exc}", file=sys.stderr)
            return EXIT_ERROR

    if args.command == "deals":
        load_dotenv()
        try:
            return _cmd_deals(args)
        except Exception as exc:
            from marketradar.freshness import StaleDataError

            label = "STALE DATA" if isinstance(exc, StaleDataError) else "error"
            print(f"mr deals: {label}: {exc}", file=sys.stderr)
            return EXIT_ERROR

    if args.command == "edgar":
        load_dotenv()
        try:
            return _cmd_edgar(args)
        except Exception as exc:
            from marketradar.freshness import StaleDataError

            label = "STALE DATA" if isinstance(exc, StaleDataError) else "error"
            print(f"mr edgar: {label}: {exc}", file=sys.stderr)
            return EXIT_ERROR

    if args.command == "fred":
        load_dotenv()
        try:
            return _cmd_fred(args)
        except Exception as exc:
            from marketradar.freshness import StaleDataError

            label = "STALE DATA" if isinstance(exc, StaleDataError) else "error"
            print(f"mr fred: {label}: {exc}", file=sys.stderr)
            return EXIT_ERROR

    if args.command == "digest":
        load_dotenv()
        return _cmd_digest(args)

    pending = {
        "backfill": "Weekend 3",
    }
    return _not_implemented(args.command, pending[args.command])


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
