"""``mr`` — every job runs standalone from here.

If something only works inside a GitHub Action, it is built wrong: the Action
should call the same subcommand you would call by hand.

Unimplemented subcommands exit non-zero rather than doing nothing quietly.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from marketradar import __version__

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOT_IMPLEMENTED = 2


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

    p_prices = sub.add_parser("prices", help="load EOD prices for one date")
    p_prices.add_argument("--date", type=_iso_date, help="trading date (default: latest)")
    p_prices.add_argument(
        "--restart",
        action="store_true",
        help="discard checkpoints and re-sweep from scratch",
    )

    sub.add_parser("screens", help="rebuild screens from local data")

    p_digest = sub.add_parser("digest", help="render the daily email")
    p_digest.add_argument("--dry-run", action="store_true", help="render, do not send")

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

    pending = {
        "prices": "Weekend 1 (T9)",
        "selftest": "Weekend 1 (T6)",
        "screens": "Weekend 2",
        "digest": "Weekend 2",
        "backfill": "Weekend 3",
    }
    return _not_implemented(args.command, pending[args.command])


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
