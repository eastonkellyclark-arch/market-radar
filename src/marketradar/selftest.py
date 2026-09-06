"""End-to-end pipeline proof using synthetic data and no vendor.

This is the smallest thing that exercises every layer:

    generate -> Parquet -> upload -> manifest + dataset_stats
             -> (separate process) resolve -> query over HTTP -> assert_fresh

It touches no vendor API, so it runs on a Sunday, runs while Tiingo is down,
and ran before the ticker list existed. That is what makes it safe to run on
every commit forever.

Two properties matter more than the coverage:

*It exercises both backends.* A ``prices_*`` dataset goes to R2 (private,
vendor-shaped) and a government dataset goes to a GitHub Release (public).
The licensing split in CLAUDE.md is proven by an actual round trip rather
than asserted by a unit test.

*It must be able to fail.* ``--inject-staleness`` publishes data with a
max-date well outside the freshness window and the run must exit non-zero.
An assertion nobody has watched fail is a comment, and this project exists
because something once exited green on empty data.

The read-back deliberately runs in a **separate process** (see
``mr selftest``'s use of :func:`verify_in_subprocess`), so nothing can pass
on state cached in the writing process's memory.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import duckdb

from marketradar import manifest, storage
from marketradar.freshness import StaleDataError, assert_fresh, utc_today

SELFTEST_PRICES = "selftest_prices_raw"
SELFTEST_GOV = "selftest_gov_reference"

ROWS = 5_000
MIN_ROWS = 1_000

#: Far enough past the 4-day window that no holiday can excuse it.
STALENESS_INJECTION_DAYS = 45


class SelftestError(RuntimeError):
    """The selftest could not run to a conclusion (setup problem, not a
    freshness failure). Distinct from StaleDataError, which is a *result*."""


@dataclass(frozen=True, slots=True)
class Target:
    dataset: str
    partition: str
    location: str
    backend: str


def _r2_configured() -> bool:
    return all(
        os.environ.get(name)
        for name in (storage.ENV_R2_ACCOUNT, storage.ENV_R2_KEY_ID, storage.ENV_R2_SECRET)
    )


#: Tag the selftest publishes its public asset under. Re-used across runs.
RELEASE_TAG = "selftest-fixture"


def _gh() -> str:
    """Locate the gh CLI. Not on PATH inside a uv-managed subprocess."""
    import shutil

    found = shutil.which("gh") or shutil.which(
        "gh", path=r"C:\Program Files\GitHub CLI"
    )
    if not found:
        raise SelftestError(
            "gh CLI not found. It is required to publish the public-domain "
            "half of the selftest to a GitHub Release."
        )
    return found


def build_targets(run_id: str) -> list[Target]:
    """Where this run will publish. Exercises both backends for real.

    The two halves are not interchangeable, and that is the point. Vendor-
    shaped data goes to R2 over an authenticated ``r2://`` URI; public-domain
    data goes to a GitHub Release and is read back over anonymous HTTPS. If
    the licensing split in CLAUDE.md ever stops holding, this breaks.
    """
    bucket = os.environ.get("MR_R2_BUCKET", "market-radar")
    repo = os.environ.get("MR_GITHUB_REPO")
    if not repo:
        raise SelftestError(
            "MR_GITHUB_REPO is unset. The selftest publishes its public half "
            "to a real GitHub Release."
        )
    asset = "selftest_gov_reference.parquet"
    return [
        Target(
            dataset=SELFTEST_PRICES,
            partition="2026",
            location=f"r2://{bucket}/selftest/{run_id}/prices.parquet",
            backend="r2",
        ),
        Target(
            dataset=SELFTEST_GOV,
            partition="all",
            location=f"https://github.com/{repo}/releases/download/{RELEASE_TAG}/{asset}",
            backend="github_release",
        ),
    ]


def generate(
    con: duckdb.DuckDBPyConnection, *, stale: bool, today: date | None = None
) -> duckdb.DuckDBPyRelation:
    """Synthetic OHLCV, DECIMAL(18,6), spanning the sub-penny band.

    The cheapest row is $0.000200 — unrepresentable in integer cents, which
    is precisely why prices are DECIMAL. If the precision path silently
    degrades to float or truncates, this data shows it.
    """
    today = today or utc_today()
    newest = today - timedelta(days=STALENESS_INJECTION_DAYS if stale else 0)

    con.execute(
        """
        CREATE OR REPLACE TABLE selftest_rows AS
        SELECT
            'T' || lpad((i % 250)::VARCHAR, 4, '0')                AS ticker,
            (CAST($newest AS DATE) - (i // 250)::INTEGER)          AS date,
            v.px                                                   AS open,
            (v.px * 1.05)::DECIMAL(18,6)                           AS high,
            (v.px * 0.95)::DECIMAL(18,6)                           AS low,
            v.px                                                   AS close,
            (1000 + i * 7)::BIGINT                                 AS volume,
            CASE i % 3 WHEN 0 THEN 'nasdaq' WHEN 1 THEN 'nyse' ELSE 'nysemkt' END
                                                                   AS exchange,
            'stock'                                                AS security_type,
            'selftest'                                             AS source
        FROM range($rows) AS r(i),
        LATERAL (
            SELECT (CASE i % 4
                        WHEN 0 THEN 0.000200      -- sub-penny: the whole point
                        WHEN 1 THEN 0.850000
                        WHEN 2 THEN 4.250000
                        ELSE        42.100000
                    END + (i % 97) * 0.000001)::DECIMAL(18,6) AS px
        ) AS v
        """,
        {"newest": newest, "rows": ROWS},
    )
    return con.table("selftest_rows")


def publish(
    con: duckdb.DuckDBPyConnection, rel: duckdb.DuckDBPyRelation, target: Target
) -> None:
    """Write the relation to the target location, per backend."""
    rel.query("rel", "SELECT * FROM rel").to_view("to_publish", replace=True)

    if target.backend == "r2":
        try:
            con.execute(f"COPY to_publish TO '{target.location}' (FORMAT parquet)")
        except duckdb.Error as exc:
            raise SelftestError(
                f"Could not publish {target.dataset} to R2: {exc}"
            ) from exc
        return

    if target.backend == "github_release":
        _publish_release(con, target)
        return

    raise SelftestError(f"selftest cannot publish to backend {target.backend!r}")


def _publish_release(con: duckdb.DuckDBPyConnection, target: Target) -> None:
    """Write locally, then upload as a GitHub Release asset.

    Deliberately a real upload and a real anonymous HTTPS read-back. A
    manifest row claiming ``github_release`` while pointing somewhere else
    would pass every check and prove nothing.
    """
    repo = os.environ["MR_GITHUB_REPO"]
    asset = target.location.rsplit("/", 1)[-1]
    gh = _gh()

    with tempfile.TemporaryDirectory(prefix="mr-release-") as tmp:
        local = Path(tmp) / asset
        con.execute(f"COPY to_publish TO '{local.as_posix()}' (FORMAT parquet)")

        seen = subprocess.run(
            [gh, "release", "view", RELEASE_TAG, "--repo", repo],
            capture_output=True, text=True,
        )
        if seen.returncode != 0:
            created = subprocess.run(
                [gh, "release", "create", RELEASE_TAG, "--repo", repo,
                 "--title", "selftest fixture",
                 "--notes", "Synthetic data published by `mr selftest`. "
                            "Not real market data. Safe to delete.",
                 "--prerelease"],
                capture_output=True, text=True,
            )
            if created.returncode != 0:
                raise SelftestError(
                    f"Could not create release {RELEASE_TAG}: {created.stderr.strip()[:300]}"
                )

        up = subprocess.run(
            [gh, "release", "upload", RELEASE_TAG, str(local),
             "--repo", repo, "--clobber"],
            capture_output=True, text=True,
        )
        if up.returncode != 0:
            raise SelftestError(
                f"Could not upload {asset} to release {RELEASE_TAG}: "
                f"{up.stderr.strip()[:300]}"
            )


def write_manifest(path: Path, targets: list[Target]) -> None:
    lines = []
    for t in targets:
        lines.append(f"[{t.dataset}]")
        lines.append(
            f'{t.partition} = {{ location = "{t.location}", backend = "{t.backend}" }}'
        )
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def verify_in_subprocess(manifest_path: Path, targets: list[Target]) -> int:
    """Re-resolve and assert freshness in a *fresh* interpreter.

    Nothing may pass on cached state from the writing process. The child gets
    only the manifest path and the environment.
    """
    payload = json.dumps(
        [{"dataset": t.dataset, "partition": t.partition} for t in targets]
    )
    env = dict(os.environ, **{manifest.OVERRIDE_ENV: str(manifest_path)})
    proc = subprocess.run(
        [sys.executable, "-m", "marketradar.selftest", "--verify", payload],
        env=env,
        capture_output=True,
        text=True,
    )
    sys.stdout.write(proc.stdout)
    if proc.stderr.strip():
        sys.stderr.write(proc.stderr)
    return proc.returncode


def _verify(payload: str) -> int:
    """Child-process entry point. Resolves via manifest and asserts."""
    con = storage.connect()
    for spec in json.loads(payload):
        dataset, partition = spec["dataset"], spec["partition"]
        ref = manifest.get(dataset, partition)
        rel = storage.read_ref(ref, con=con)

        observed = assert_fresh(
            dataset,
            rel,
            partition=partition,
            min_rows=MIN_ROWS,
            expect_cols=("ticker", "close", "volume"),
        )
        print(
            f"  [child pid {os.getpid()}] {dataset}/{partition} "
            f"backend={ref.backend:<15} rows={observed.row_count:,} "
            f"max_date={observed.max_date}  FRESH"
        )

        sub_penny = rel.query(
            "rel", "SELECT min(close) AS c, typeof(min(close)) AS t FROM rel"
        ).fetchone()
        print(f"      min(close)={sub_penny[0]}  type={sub_penny[1]}")
        if str(sub_penny[1]).upper() != "DECIMAL(18,6)":
            raise SelftestError(f"precision lost: close came back as {sub_penny[1]}")
    return 0


def run(*, inject_staleness: bool = False) -> int:
    """Full round trip. Returns a process exit code."""
    started = time.time()
    run_id = f"{utc_today().isoformat()}-{os.getpid()}"

    print("=" * 70)
    print(f"mr selftest  run_id={run_id}")
    print(f"  inject_staleness = {inject_staleness}")
    print("=" * 70)

    if not _r2_configured():
        raise SelftestError(
            "R2 is not configured. Set "
            f"{storage.ENV_R2_ACCOUNT}, {storage.ENV_R2_KEY_ID}, and "
            f"{storage.ENV_R2_SECRET}. The selftest publishes for real."
        )

    targets = build_targets(run_id)
    con = storage.connect()

    print("\n1. generate synthetic OHLCV (no vendor call)")
    rel = generate(con, stale=inject_staleness)
    n, newest = rel.query("rel", "SELECT count(*), max(date) FROM rel").fetchone()
    print(f"   {n:,} rows, DECIMAL(18,6), newest date {newest}")

    print("\n2. publish to both backends")
    for t in targets:
        publish(con, rel, t)
        flag = "private" if t.backend in manifest.PRIVATE_BACKENDS else "public "
        print(f"   {t.dataset}/{t.partition:<5} -> {t.backend:<15} [{flag}] OK")

    tmpdir = Path(tempfile.mkdtemp(prefix="mr-selftest-"))
    manifest_path = tmpdir / "manifest.toml"
    write_manifest(manifest_path, targets)
    print(f"\n3. manifest written: {manifest_path}")

    print("\n4. record dataset_stats")
    observation = manifest.Freshness(
        dataset=SELFTEST_PRICES, partition="2026", row_count=n, max_date=newest
    )
    persisted = manifest.record_stats(observation, con=con)
    if persisted:
        print("   written to Postgres")
    else:
        print(
            f"   skipped — {storage.ENV_PG_DSN} is unset. Stats are diagnostics; "
            "the correctness gate is assert_fresh, which needs no database."
        )

    print("\n5. verify in a SEPARATE process (resolve -> HTTP -> assert_fresh)")
    rc = verify_in_subprocess(manifest_path, targets)

    print("\n" + "=" * 70)
    elapsed = time.time() - started
    if rc == 0:
        if inject_staleness:
            print(f"FAIL: stale data was published and the check PASSED it. ({elapsed:.1f}s)")
            print("The freshness assertion is not doing its job.")
            return 1
        print(f"PASS: full round trip green in {elapsed:.1f}s")
        return 0

    if inject_staleness:
        print(f"PASS: stale data was correctly REJECTED in {elapsed:.1f}s")
        print("(non-zero child exit is the expected result here)")
        return 1  # the run itself must still be red
    print(f"FAIL: round trip failed in {elapsed:.1f}s")
    return rc


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--verify":
        try:
            return _verify(argv[1])
        except StaleDataError as exc:
            print(f"  [child] StaleDataError: {exc}", file=sys.stderr)
            return 3
    return run(inject_staleness="--inject-staleness" in argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
