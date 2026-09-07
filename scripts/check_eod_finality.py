"""Is Tiingo's EOD bar final by the time the cron fires?

The cron runs at 03:30 UTC, which is 23:30 ET during EDT and 22:30 ET during
EST. If Tiingo is still revising that session's bars at that hour, the sweep
succeeds with *wrong* data and ``assert_fresh`` will not catch it — the data
is fresh, it is just not final. Freshness and correctness are different
properties and only one of them is asserted.

Usage:

    uv run python scripts/check_eod_finality.py --snapshot   # capture now
    uv run python scripts/check_eod_finality.py --compare    # re-fetch, diff

Run ``--snapshot`` shortly after the cron would fire (≈23:30 ET), then
``--compare`` the next morning. Any field that moves is a revision, and the
size of the move tells you whether the cron needs to be later.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx

from marketradar import manifest
from marketradar.cli import load_dotenv
from marketradar.sources.tiingo import _token

#: Liquid names across the bands the screens care about, plus two ETFs whose
#: bars are constructed rather than traded and so settle differently.
TICKERS = ["AAPL", "SPY", "TSLA", "F", "SIRI", "PLUG", "IWM", "QQQ"]

SNAPSHOT = Path(".checkpoints") / "eod_finality_snapshot.json"
FIELDS = ("open", "high", "low", "close", "volume", "divCash", "splitFactor")


def fetch(ticker: str, days: int = 6) -> dict | None:
    """Most recent bar Tiingo has for this ticker."""
    base = manifest.get("tiingo_api", "base").location
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)
    resp = httpx.get(
        f"{base}/tiingo/daily/{ticker}/prices",
        params={"startDate": start.isoformat(), "endDate": end.isoformat()},
        headers={"Authorization": f"Token {_token()}"},
        timeout=30.0,
    )
    resp.raise_for_status()
    bars = resp.json() or []
    return bars[-1] if bars else None


def capture() -> dict:
    at = datetime.now(timezone.utc)
    out: dict = {"captured_at": at.isoformat(timespec="seconds"), "bars": {}}
    for t in TICKERS:
        bar = fetch(t)
        if not bar:
            print(f"  {t:<6} no data")
            continue
        out["bars"][t] = {k: bar.get(k) for k in FIELDS} | {"date": bar.get("date")}
        print(f"  {t:<6} {bar.get('date','')[:10]}  close={bar.get('close')}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--snapshot", action="store_true")
    ap.add_argument("--compare", action="store_true")
    args = ap.parse_args(argv)
    load_dotenv()

    if args.snapshot:
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        data = capture()
        SNAPSHOT.write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(f"\nsnapshot written: {SNAPSHOT}  ({data['captured_at']} UTC)")
        return 0

    if args.compare:
        if not SNAPSHOT.is_file():
            print("no snapshot to compare against; run --snapshot first")
            return 1
        before = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        print(f"snapshot taken {before['captured_at']} UTC")
        print(f"comparing at   {datetime.now(timezone.utc).isoformat(timespec='seconds')} UTC\n")

        revised = 0
        for ticker, old in before["bars"].items():
            new = fetch(ticker)
            if not new:
                print(f"  {ticker:<6} vanished from the feed")
                revised += 1
                continue
            if (new.get("date") or "")[:10] != (old.get("date") or "")[:10]:
                print(f"  {ticker:<6} newer session now present "
                      f"({old.get('date','')[:10]} -> {new.get('date','')[:10]})")
                continue
            diffs = [
                (f, old.get(f), new.get(f)) for f in FIELDS if old.get(f) != new.get(f)
            ]
            if diffs:
                revised += 1
                print(f"  {ticker:<6} REVISED  {(new.get('date') or '')[:10]}")
                for f, o, n in diffs:
                    delta = ""
                    try:
                        if o is not None and n is not None and o:
                            delta = f"  ({(Decimal(str(n))/Decimal(str(o))-1)*100:+.4f}%)"
                    except Exception:
                        pass
                    print(f"           {f:<12} {o!r} -> {n!r}{delta}")
            else:
                print(f"  {ticker:<6} identical  {(new.get('date') or '')[:10]}")

        print()
        if revised:
            print(f"VERDICT: {revised}/{len(before['bars'])} bars changed after the "
                  "snapshot. The cron fires too early — the sweep would have "
                  "stored provisional data, and assert_fresh would not have "
                  "noticed because the data was fresh, just wrong.")
            return 1
        print(f"VERDICT: all {len(before['bars'])} bars identical. Tiingo's EOD is "
              "final by the snapshot hour; the cron timing is safe.")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
