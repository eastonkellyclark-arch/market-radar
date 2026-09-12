"""Quarterly Financial Statement Data Set zips, cached and resumable.

One download per calendar quarter, about 100 MB each, and ~44 of them to match
the eleven years of price history. So: cached on disk by quarter, skipped when
already present, and never re-downloaded to satisfy a re-run of the loader.

**Bulk, not per-company.** The alternative is ``companyfacts.zip`` or 8,000
per-CIK calls, and both are worse for the same reason given in CLAUDE.md:
prefer one download to thousands of requests, and prefer as-filed values to
today's restated view.

Only ``sub``, ``num`` and ``pre`` are extracted. ``tag.txt`` is the taxonomy's
own label and documentation table -- 18 MB per quarter of text the map does not
read, since which tag means what is a decision recorded in ``tag_map.py``
rather than something to look up at load time.
"""

from __future__ import annotations

import logging
import os
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import httpx

from marketradar import manifest
from marketradar.sources.xbrl import tag_map

log = logging.getLogger(__name__)

SOURCE: Final[str] = "sec_financial_statements"

#: SEC blocks traffic with no descriptive User-Agent and asks for a real
#: contact address. Same variable and same rule as sources/sec_company_tickers.
ENV_USER_AGENT: Final[str] = "MR_SEC_USER_AGENT"

#: Where the zips and their extracted tables live. Gitignored: this is ~2 GB of
#: bulk government data and the repo holds code and SQL only.
DEFAULT_CACHE: Final[Path] = Path(".cache") / "xbrl"

#: The three tables the loader reads.
TABLES: Final[tuple[str, ...]] = ("sub", "num", "pre")

REQUEST_TIMEOUT: Final[float] = 300.0

#: ``2024q1``. Checked rather than trusted, because it is interpolated into a
#: URL and into filenames, and an unvalidated partition name is how a path
#: traversal or a 404-shaped mystery gets in.
QUARTER: Final[re.Pattern[str]] = re.compile(r"^(19|20)\d{2}q[1-4]$")


class XbrlFetchError(RuntimeError):
    """A quarter could not be downloaded or does not look like a data set."""


@dataclass(frozen=True, slots=True)
class Quarter:
    """One fetched quarter, and where its tables landed."""

    quarter: str
    zip_path: Path
    tables: dict[str, Path]
    downloaded: bool


def user_agent() -> str:
    """The contact address SEC requires, or a refusal that says why."""
    ua = os.environ.get(ENV_USER_AGENT, "").strip()
    if not ua:
        raise XbrlFetchError(
            f"{ENV_USER_AGENT} is not set to a real contact address. SEC blocks "
            "traffic without a descriptive User-Agent."
        )
    if "@" not in ua:
        raise XbrlFetchError(
            f"{ENV_USER_AGENT}={ua!r} has no email address in it. SEC asks for "
            "a real contact, not a product name."
        )
    return ua


def quarters(start: str, end: str) -> list[str]:
    """Every quarter from ``start`` to ``end`` inclusive, e.g. 2019q1..2024q1."""
    for name in (start, end):
        if not QUARTER.match(name):
            raise XbrlFetchError(f"{name!r} is not a quarter like '2024q1'")
    lo, hi = _ordinal(start), _ordinal(end)
    if hi < lo:
        raise XbrlFetchError(f"{end} is before {start}")
    return [_name(n) for n in range(lo, hi + 1)]


def _ordinal(quarter: str) -> int:
    year, q = int(quarter[:4]), int(quarter[-1])
    return year * 4 + (q - 1)


def _name(ordinal: int) -> str:
    return f"{ordinal // 4}q{ordinal % 4 + 1}"


def fetch(
    quarter: str,
    *,
    cache: Path | None = None,
    client: httpx.Client | None = None,
    tables: tuple[str, ...] = TABLES,
) -> Quarter:
    """Download one quarter if it is not already cached, then extract it.

    Idempotent by design rather than by luck: a present zip with a plausible
    size is not downloaded again, and extraction is skipped when the tables are
    already on disk. Re-running the loader over a decade of quarters costs
    nothing the second time, which is what makes a backfill resumable after the
    inevitable interruption.

    ``tables`` exists because the three are wildly different sizes. ``sub.txt``
    is 1.8 MB and answers who filed; ``num.txt`` is 490 MB and answers what they
    reported. The filer universe needs only the first, so unpacking all three
    for it would cost 15 GB across the range to read 54 MB.
    """
    if not QUARTER.match(quarter):
        raise XbrlFetchError(f"{quarter!r} is not a quarter like '2024q1'")
    root = (cache or DEFAULT_CACHE)
    work = root / "work"
    root.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)

    zip_path = root / f"{quarter}.zip"
    paths = {name: work / f"{quarter}_{name}.txt" for name in tables}
    missing = [name for name, path in paths.items()
               if not path.exists() or path.stat().st_size == 0]

    # The extracts are checked **before** the zip, so a quarter whose tables are
    # already unpacked needs neither the zip nor the network. That ordering is
    # the point of keeping sub.txt after a load: the filer universe rebuilds from
    # 1.8 MB a quarter with the 110 MB zips deleted, and the other way round it
    # would quietly re-download 3 GB to read files that were already there.
    downloaded = False
    if missing:
        if not zip_path.exists() or zip_path.stat().st_size < 1_000_000:
            _download(quarter, zip_path, client=client)
            downloaded = True
        _extract(zip_path, quarter, work, missing)

    for name, path in paths.items():
        if not path.exists() or path.stat().st_size == 0:
            raise XbrlFetchError(
                f"{quarter}: {name}.txt is missing or empty after extraction. "
                f"Delete {zip_path} and re-run -- a truncated download is the "
                "usual cause."
            )
    return Quarter(quarter=quarter, zip_path=zip_path, tables=paths,
                   downloaded=downloaded)


def _download(quarter: str, target: Path, *, client: httpx.Client | None) -> None:
    url = manifest.get(SOURCE, "quarterly").location.format(quarter=quarter)
    headers = {"User-Agent": user_agent(), "Accept-Encoding": "gzip, deflate"}
    # Streamed to a partial file and renamed on success, so an interrupted
    # download cannot leave something that looks cached. The size guard in
    # fetch() is the second line of that defence, not the first.
    partial = target.with_suffix(".zip.part")
    close = client is None
    client = client or httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=True)
    try:
        with client.stream("GET", url, headers=headers) as resp:
            if resp.status_code == 403:
                raise XbrlFetchError(
                    "SEC returned 403. This is almost always the User-Agent: it "
                    "must be descriptive and carry a real contact address. "
                    f"Current {ENV_USER_AGENT}="
                    f"{os.environ.get(ENV_USER_AGENT, '')!r}"
                )
            if resp.status_code == 404:
                raise XbrlFetchError(
                    f"SEC has no data set for {quarter} (404). The newest "
                    "quarter appears about a month after it closes."
                )
            resp.raise_for_status()
            with partial.open("wb") as out:
                for chunk in resp.iter_bytes(1 << 20):
                    out.write(chunk)
    except httpx.HTTPError as exc:
        partial.unlink(missing_ok=True)
        raise XbrlFetchError(f"Could not fetch {quarter} from SEC: {exc}") from exc
    finally:
        if close:
            client.close()
    partial.replace(target)
    log.info("%s: downloaded %.0f MB", quarter, target.stat().st_size / 1e6)


def _extract(zip_path: Path, quarter: str, work: Path, names: list[str]) -> None:
    try:
        with zipfile.ZipFile(zip_path) as archive:
            held = set(archive.namelist())
            for name in names:
                member = f"{name}.txt"
                if member not in held:
                    raise XbrlFetchError(
                        f"{quarter}: {member} is not in the zip, which holds "
                        f"{sorted(held)}. The data set's shape has changed."
                    )
                target = work / f"{quarter}_{name}.txt"
                with archive.open(member) as src, target.open("wb") as out:
                    # Chunked: num.txt is ~490 MB uncompressed and there is no
                    # reason for it to pass through memory.
                    while chunk := src.read(1 << 20):
                        out.write(chunk)
    except zipfile.BadZipFile as exc:
        raise XbrlFetchError(
            f"{quarter}: {zip_path} is not a readable zip. Delete it and "
            "re-run; a partial download is the usual cause."
        ) from exc


def prune(
    quarter: str,
    *,
    cache: Path | None = None,
    keep: tuple[str, ...] = ("sub",),
    drop_zip: bool = False,
) -> int:
    """Delete a quarter's bulk extracts. Returns bytes freed.

    A 30-quarter backfill extracts about 18 GB of tab-separated text, and
    ``num.txt`` alone is ~490 MB a quarter. None of it is needed once the
    partition parquet is written, which is about 0.3 MB. So the extracts are
    scratch and get cleaned up between quarters; peak footprint becomes one
    quarter rather than all of them.

    ``sub.txt`` is **kept by default** and the other two are not, because the
    sizes are not comparable: 1.8 MB against 580 MB a quarter. Keeping every
    quarter's submissions table costs 54 MB across the range and is what the
    point-in-time filer universe reads, so it is cheaper to keep than to
    re-extract and far cheaper than re-downloading.

    ``drop_zip`` is off by default but is the right setting once a quarter has
    loaded: the zip is a ~110 MB convenience that saves a re-download if the tag
    map changes, and the tag map changing is a ~20 minute re-fetch rather than a
    problem. 4.3 GB of zips against 8 MB of partitions is a bad trade on a disk
    with 53 GB free. See docs/build-spec.md.
    """
    root = cache or DEFAULT_CACHE
    freed = 0
    for name in TABLES:
        if name in keep:
            continue
        path = root / "work" / f"{quarter}_{name}.txt"
        if path.exists():
            freed += path.stat().st_size
            path.unlink()
    if drop_zip:
        if quarter == tag_map.REFERENCE_QUARTER:
            # **Refused, loudly, rather than done quietly.** Every
            # `coverage_2024q1` figure in the tag map is re-measured against this
            # quarter by `test_the_map_reproduces_its_own_measurement`, and that
            # test skips when the zip is gone. On 2026-09-12 a `--drop-zips` run
            # deleted it and the test went from passing to skipped in the same
            # commit that added a concept -- having caught a real 0.9-point error
            # in that concept's figure minutes earlier.
            #
            # A flag that removes a guard has to say so. This is ~110 MB kept
            # deliberately, which is the cheapest part of the whole cache.
            raise XbrlFetchError(
                f"refusing to drop {quarter}.zip: it is the reference quarter "
                f"every coverage figure in tag_map is measured against, and "
                f"dropping it downgrades test_the_map_reproduces_its_own_"
                f"measurement from a check to a skip. Keep the 110 MB, or change "
                f"tag_map.REFERENCE_QUARTER first and re-measure the map."
            )
        zip_path = root / f"{quarter}.zip"
        if zip_path.exists():
            freed += zip_path.stat().st_size
            zip_path.unlink()
    if freed:
        log.info("%s: pruned %.0f MB", quarter, freed / 1e6)
    return freed
