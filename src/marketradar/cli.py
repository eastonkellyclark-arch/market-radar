"""``mr`` — every job runs standalone from here.

If something only works inside a GitHub Action, it is built wrong: the Action
should call the same subcommand you would call by hand.

Unimplemented subcommands exit non-zero rather than doing nothing quietly.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

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

    target = shell.write(args.out, ctx=ctx, digest=digest)
    counts = shell.summary(ctx)

    print(f"wrote {target}")
    print(f"  {counts[shell.LIVE]} live, {counts[shell.WAITING]} waiting, "
          f"{counts[shell.NOT_BUILT]} not built")
    for panel in shell.PANELS:
        state, detail = panel.resolve(ctx)
        mark = {shell.LIVE: "+", shell.WAITING: "~", shell.NOT_BUILT: "."}[state]
        print(f"  {mark} {panel.title:<24} {state:<10} {detail[:52]}")

    if not args.no_open and shell.open_in_browser(target):
        print("opened in your browser")
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
