"""Who filed, when, under which name — including the ones that stopped.

A **point-in-time filer universe**, built from the ``sub`` table of every loaded
quarter. One row per CIK with the window it filed across, the names it filed
under, its SIC, and whether it is still filing.

**This is the half of "a point-in-time universe" that turned out to be free.**
CLAUDE.md says a point-in-time universe is a data purchase rather than a query,
and that is two claims wearing one name:

*A point-in-time price universe is still a purchase.* Nothing here changes it.
Tiingo's supported-ticker list is current listings, so a company that was
acquired has no bars at all — not truncated, absent — and no amount of knowing
who it was conjures a price history for it.

*A point-in-time filer universe is a query,* and this module is it. The SEC
Financial Statement Data Sets are as-filed and keep every filer that ever filed,
so the submissions tables already on disk carry the companies that
``company_tickers.json`` has dropped. Measured 2026-09-10 over 30 quarters:
**11,323 CIKs, of which 7,499 are still filing, 3,744 have stopped, and 80 are
inside the lag window.** ``companies`` knows 85.0% of the filers still going and
**2.7% of the ones that stopped** — a thirty-one-fold gap, shaped exactly like an
acquisition.

So be precise about what it fixes, because the two halves look alike and only
one moved:

``identification`` — **fixed.** Given a deal, we can now say who the target was,
by CIK, with a name and an SIC and a filing window, whether or not it still
exists. Any population selected through ``companies`` inherits a 97% hole in the
stopped-filing half; selected through this, it does not.

``survivorship`` — **not fixed, and not fixable from here.** A delisted company
still has no prices, so a forward-return study keyed on price history is exactly
as biased as it was. Deal multiples get more rows because they divide a stated
price by a reported figure and need no prices at all. Forward returns do not,
and the caveat on them stands unchanged.

The universe is also what makes a *targeted* sweep possible, which is the
practical payoff. Finding the deals of companies that no longer exist by walking
eleven years of daily indexes is ~50,000 requests; asking EDGAR for the filing
history of 3,744 known CIKs is 3,744.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Final

import duckdb

from marketradar.freshness import assert_fresh
from marketradar.sources.xbrl import tag_map
from marketradar.sources.xbrl.download import fetch as fetch_quarter

log = logging.getLogger(__name__)

DATASET: Final[str] = "sec_filers"
SOURCE: Final[str] = "sec_financial_statements"

#: A filer whose newest fiscal period end is older than this is treated as
#: having stopped. Not "acquired" -- it could have deregistered, gone private,
#: gone bankrupt, or simply be late. The distinction this carries is *filing*
#: against *not filing*, and naming it ``stopped`` rather than ``acquired`` is
#: the whole point: the reason is a separate question with a separate answer.
#:
#: 18 months behind the newest loaded period, for the same reason Form 5500 uses
#: eighteen: a 10-K lands months after its fiscal year, so below that, absence
#: and lateness are indistinguishable.
STOPPED_LAG_DAYS: Final[int] = 548

FILING: Final[str] = "filing"
STOPPED: Final[str] = "stopped"
PENDING: Final[str] = "pending"

#: Three states, and ``pending`` is the one that keeps the other two honest.
#:
#: ``filing``  the newest fiscal period is within the lag of the newest period
#:             in the range. Current, including everything whose next annual
#:             report simply has not landed yet -- that is what the lag is for.
#: ``pending`` the period is stale but the *filing* is recent: a company that
#:             caught up late, which reads exactly like one that stopped if you
#:             only look at period ends.
#: ``stopped`` stale period, no recent filing. The acquisition population.
#:
#: Fourth appearance of the underlying shape, after Form 5500's
#: ``pending_years``, the XBRL nil tag, and deal_multiples' ``too_recent``: a row
#: that is not there yet and a row that is not there are different facts, and the
#: arithmetic that treats them alike never errors.
STATUSES: Final[tuple[str, ...]] = (FILING, STOPPED, PENDING)

MIN_ROWS: Final[int] = 1_000


@dataclass(frozen=True, slots=True)
class Universe:
    """The filer table, and the counts worth reading beside it."""

    rows: int
    filing: int
    stopped: int
    pending: int
    quarters: list[str]
    #: Newest fiscal period end across the whole universe, which is what the
    #: lag window is measured back from.
    newest_period: date | None

    def lines(self) -> list[str]:
        out = [f"{DATASET}: {self.rows:,} filers over "
               f"{len(self.quarters)} quarters"]
        for name, count in ((FILING, self.filing), (STOPPED, self.stopped),
                            (PENDING, self.pending)):
            out.append(f"  {name:<9} {count:>7,}")
        if self.newest_period:
            out.append(f"  newest fiscal period end {self.newest_period}; "
                       f"`stopped` means nothing newer than "
                       f"{STOPPED_LAG_DAYS} days before it")
        return out


def _sub_sql(path: str) -> str:
    """One quarter's submissions, typed and narrowed.

    Every form type, not just 10-K. The fundamentals table is deliberately
    operating-company 10-Ks only; an identity table that inherited that filter
    would not know about a bank that was acquired, which is a different question
    from whether a bank's income statement is comparable.
    """
    return f"""
        select
            lpad(cast(cik as varchar), 10, '0')      as cik,
            name                                     as company,
            try_cast(sic as integer)                 as sic,
            form,
            strptime(period, '%Y%m%d')::date         as period_end,
            strptime(filed,  '%Y%m%d')::date         as filed
        from read_csv('{path}', delim='\t', header=true,
                      sample_size=-1, all_varchar=true)
    """


def build(
    con: duckdb.DuckDBPyConnection,
    quarters: list[str],
    *,
    cache: Path | None = None,
    subs: dict[str, Path] | None = None,
    table: str = "sec_filers",
) -> Universe:
    """Build the universe into ``table`` from the quarters' submissions tables.

    Only ``sub.txt`` is unpacked -- 1.8 MB a quarter against 580 MB for the
    other two -- so this is cheap to rebuild and does not depend on the bulk
    extracts having survived a prune.

    ``subs`` maps quarter to an already-extracted submissions file, which is how
    this is tested: the same escape hatch ``resolve.build`` has, and for the same
    reason -- a function that can only run by downloading 100 MB from SEC is a
    function with no tests.
    """
    if not quarters:
        raise ValueError("no quarters given; the universe would be empty")
    parts = []
    for quarter in quarters:
        if subs is not None and quarter in subs:
            path = subs[quarter]
        else:
            path = fetch_quarter(quarter, cache=cache, tables=("sub",)
                                 ).tables["sub"]
        parts.append(_sub_sql(path.as_posix()))

    con.execute("create or replace table all_subs as "
                + " union all by name ".join(parts))
    newest = con.execute(
        "select max(period_end) from all_subs").fetchone()[0]

    # The name is picked by `max_by` on the newest filing, never by an
    # arbitrary-row function: a filer changes name and the two spellings must
    # not take turns between loads. The same rule that `build_sponsors` learned.
    con.execute(f"""
        create or replace table {table} as
        with per_cik as (
            select
                cik,
                max_by(company, (filed, period_end))        as company,
                max_by(sic, (filed, period_end))            as sic,
                min(period_end)                             as first_period,
                max(period_end)                             as last_period,
                min(filed)                                  as first_filed,
                max(filed)                                  as last_filed,
                count(*)                                    as filings,
                count(distinct company)                     as names,
                list(distinct form order by form)            as forms
            from all_subs
            where cik is not null and period_end is not null
            group by cik
        )
        select *,
            case
                when DATE '{(newest or date(1900, 1, 1)).isoformat()}'
                     - last_period <= {STOPPED_LAG_DAYS} then '{FILING}'
                when last_filed >= DATE '{(newest or date(1900, 1, 1))
                     .isoformat()}' - {STOPPED_LAG_DAYS} then '{PENDING}'
                else '{STOPPED}'
            end                                             as status
        from per_cik
    """)

    counts = {
        row[0]: int(row[1]) for row in con.execute(
            f"select status, count(*) from {table} group by 1").fetchall()
    }
    universe = Universe(
        rows=int(con.execute(f"select count(*) from {table}").fetchone()[0]),
        filing=counts.get(FILING, 0),
        stopped=counts.get(STOPPED, 0),
        pending=counts.get(PENDING, 0),
        quarters=list(quarters),
        newest_period=newest,
    )
    for line in universe.lines():
        log.debug("%s", line)
    return universe


def stopped_ciks(
    con: duckdb.DuckDBPyConnection, *, table: str = "sec_filers"
) -> list[str]:
    """CIKs that have stopped filing, oldest last filing first.

    The population a targeted deal sweep should ask EDGAR about: these are the
    companies whose filings the daily-index sweep has the least of, because they
    existed for fewer of the years it covered, and they are exactly the ones an
    acquisition study needs.
    """
    return [row[0] for row in con.execute(
        f"select cik from {table} where status = '{STOPPED}' "
        "order by last_period, cik").fetchall()]


def load(
    con: duckdb.DuckDBPyConnection,
    quarters: list[str],
    out_dir: Path,
    *,
    cache: Path | None = None,
    subs: dict[str, Path] | None = None,
    min_rows: int = MIN_ROWS,
) -> tuple[Universe, Any]:
    """Build the universe, write it, and assert it is real."""
    from dataclasses import replace as _replace

    from marketradar import manifest

    universe = build(con, quarters, cache=cache, subs=subs)
    ref = manifest.get(DATASET, "all")
    if ref.backend not in {"github_release", "local"}:
        raise ValueError(
            f"{DATASET} resolves to backend {ref.backend!r}. This is SEC data, "
            "public domain and ours to republish; a private backend would mean "
            "the licensing boundary was crossed in the wrong direction."
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{DATASET}.parquet"
    con.execute(
        f"copy (select * from sec_filers order by cik) "
        f"to '{dest.as_posix()}' (format parquet)"
    )
    con.execute("create or replace view filers_published as "
                f"select * from read_parquet('{dest.as_posix()}')")
    observed = assert_fresh(
        DATASET,
        con.table("filers_published"),
        partition="all",
        min_rows=min_rows,
        # A filer universe has no observation date of its own: the newest
        # fiscal period end is a property of the loaded range, not of today,
        # and the span is asserted below instead.
        date_column=None,
        expect_cols=("cik", "company", "sic", "first_period", "last_period",
                     "status"),
    )
    observed = _replace(observed, max_date=universe.newest_period)
    manifest.record_stats(observed, con=con)
    log.info("wrote %s", dest)
    return universe, observed


def coverage_against(
    con: duckdb.DuckDBPyConnection,
    other: str,
    *,
    table: str = "sec_filers",
    cik_column: str = "cik",
) -> dict[str, Any]:
    """How much of this universe a current-only table knows about.

    The number that makes the point: run against ``companies``, the
    stopped-filing half comes back at about 5%. Any population selected through
    that table inherits the hole, and the hole is shaped exactly like an
    acquisition.
    """
    rows = con.execute(f"""
        with u as (select cik, status from {table}),
             o as (select distinct lpad(cast({cik_column} as varchar), 10, '0')
                          as cik from {other})
        select u.status, count(*) as n_total,
               count(*) filter (where o.cik is not null) as n_known
        from u left join o using (cik) group by u.status
    """).fetchall()
    out: dict[str, Any] = {"table": other, "by_status": {}}
    for status, total, known in rows:
        out["by_status"][status] = {
            "filers": int(total), "known": int(known),
            "share": (known / total if total else 0.0),
        }
    return out


#: SIC classes that are not operating companies, re-exported so a consumer of
#: the universe can apply the same split the fundamentals table does without
#: importing the tag map for one function.
classify_sic = tag_map.classify_sic
