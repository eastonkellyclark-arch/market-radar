"""Did the job run, and did it write anything.

:mod:`marketradar.freshness` asserts that a *dataset* grew. This asserts that a
*job* ran. They are different questions, and the gap between them is where this
system keeps failing.

Measured 2026-09-16: ``prices`` had been green every scheduled night for ten
sessions while the EDGAR sentinel, Form 4 clustering and the companies refresh
had not run at all -- for eight, seven and nine days. Nothing was red, because
nothing was scheduled to go red, and the digest could not see it either: its
``companies`` health line prints a row count with no max-age, so a table frozen
since 2026-09-07 rendered as ``8,005 CIKs, 10,412 ticker rows`` under HEALTH OK.

Two things are checked and the second matters as much as the first.

**Age, against the job's own schedule rather than a flat maximum.** A flat
"daily" would be red every Tuesday: ``prices`` runs Tue-Sat, so the Saturday
sentinels run is the newest heartbeat until Tuesday's completes, three days
later. So each cadence knows its own fire times and the check asks "has it run
since it was last due", which is the question a flat age cannot express.

**Rows written, because a job that ran and wrote nothing is the failure that
recurs here.** ``mr proxy`` printed "3 documents located" and wrote zero rows
three times; ``mr symbols`` could not run at all while the test beside it stayed
green; 35 declared Release locations held nothing for weeks while the code that
built them exited 0. A heartbeat that only said "I ran" would have passed all
three.

Which makes the *choice* of count the load-bearing decision, and it is not the
same quantity for every job. ``sentinels.form4`` records **filings parsed**, not
clusters found: a day with no insider cluster is an ordinary day and a day with
no Form 4s is a broken reader. Picking the wrong one gives a check that is
green in exactly the quiet direction it exists to cover. Same distinction as
``absent`` against ``unmapped``, and the same lesson as ``TOT_PARTCP_BOY_CNT``
-- read what the column counts before naming it. Each :class:`Cadence` states
its chosen quantity and why zero means broken.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Final

log = logging.getLogger(__name__)

#: How far back :meth:`Cadence.fires_before` will walk looking for a fire time.
#: Nine days covers the longest gap any cadence here has (Sunday to Sunday) plus
#: room to find the one before it.
_LOOKBACK_DAYS: Final[int] = 9


class HeartbeatError(RuntimeError):
    """A scheduled job has not run, or ran and wrote nothing."""


@dataclass(frozen=True, slots=True)
class Cadence:
    """When a job is due, and what a healthy run of it writes.

    ``weekdays`` uses Python's numbering (Monday is 0). ``minutes_utc`` are
    minutes past midnight UTC, and must be the **earliest** time the job can
    fire -- a local-time job crossing a DST boundary fires an hour earlier in
    summer, and a due time set to the winter hour would read that as late.
    """

    job: str
    label: str
    weekdays: frozenset[int]
    minutes_utc: tuple[int, ...]
    grace: timedelta
    counts: str
    zero_means: str

    def fires_before(self, now: datetime) -> Iterator[datetime]:
        """Scheduled fire times at or before ``now``, newest first."""
        day = now.astimezone(timezone.utc).date()
        for back in range(_LOOKBACK_DAYS):
            d = day - timedelta(days=back)
            if d.weekday() not in self.weekdays:
                continue
            for m in sorted(self.minutes_utc, reverse=True):
                fire = datetime.combine(
                    d, datetime.min.time(), tzinfo=timezone.utc
                ) + timedelta(minutes=m)
                if fire <= now:
                    yield fire

    def required_since(self, now: datetime) -> datetime | None:
        """The fire time a healthy heartbeat must be at or after.

        The most recent fire, once its grace has elapsed; otherwise the one
        before it. Without that fallback the check is never enforceable for a
        job whose reporting runs inside its own grace window -- which is every
        job here, because the digest is chained to the same sweep the sentinels
        are and therefore always executes mid-window.
        """
        fires = list(self.fires_before(now))
        if not fires:
            return None
        if now >= fires[0] + self.grace:
            return fires[0]
        return fires[1] if len(fires) > 1 else None


#: Every scheduled job, with the count that is never legitimately zero for it.
#:
#: Adding a scheduled job means adding it here. A job absent from this table is
#: unmonitored, and ``tests/test_heartbeat.py`` fails the build when a workflow
#: runs an ``mr`` command that no cadence covers -- so "we forgot to watch it"
#: is not reachable for anything running in Actions.
CADENCES: Final[dict[str, Cadence]] = {
    "edgar-poll": Cadence(
        job="edgar-poll",
        label="every 30 min, weekdays 12:07-23:37 UTC",
        weekdays=frozenset({0, 1, 2, 3, 4}),
        minutes_utc=tuple(h * 60 + m for h in range(12, 24) for m in (7, 37)),
        # 15s of work; the slack is for a delayed cron, which GitHub documents
        # as normal at high load.
        grace=timedelta(minutes=45),
        counts="filings returned by the poll",
        zero_means=(
            "EDGAR takes thousands of filings a day and Form 4s alone run ~900, "
            "so a poll returning nothing is a broken poll and not a quiet "
            "market. Deliberately not 'new filings stored', which is "
            "legitimately zero whenever the previous poll already had them"
        ),
    ),
    "sentinels.form4": Cadence(
        job="sentinels.form4",
        label="daily after the sweep, Tue-Sat",
        weekdays=frozenset({1, 2, 3, 4, 5}),
        minutes_utc=(3 * 60 + 30,),
        # The sweep fires at 03:30 UTC, takes ~95 min, and has started 4h47m
        # late. Ten hours covers the worst observed case with room over.
        grace=timedelta(hours=10),
        counts="Form 4 documents parsed over the 5-day window",
        zero_means=(
            "measured 2,351 over five days. NOT clusters found -- 12 in the "
            "same window, and a week with no insider cluster is an ordinary "
            "week. Counting clusters would make a broken reader and a quiet "
            "market the same number"
        ),
    ),
    "sentinels.deals": Cadence(
        job="sentinels.deals",
        label="daily after the sweep, Tue-Sat",
        weekdays=frozenset({1, 2, 3, 4, 5}),
        minutes_utc=(3 * 60 + 30,),
        grace=timedelta(hours=10),
        counts="8-K deal candidates found over the 5-day window",
        zero_means=(
            "EDGAR carries ~12,000 Item 1.01/2.01 filings a year -- roughly "
            "230 a week -- so five days never legitimately contains none. "
            "Candidates rather than deals *stored*, because the classifiers "
            "disagreeing on every candidate in a window is a real outcome "
            "and an empty read is not"
        ),
    ),
    "sentinels.sec-tickers": Cadence(
        job="sentinels.sec-tickers",
        label="weekly, Sunday 09:07 UTC",
        weekdays=frozenset({6}),
        minutes_utc=(9 * 60 + 7,),
        grace=timedelta(hours=6),
        counts="ticker rows upserted",
        zero_means=(
            "the SEC map carries ~10,400 rows and the file is never empty; "
            "zero means the fetch failed or the parse returned nothing"
        ),
    ),
    "decks": Cadence(
        job="decks",
        label="nightly 01:30 America/Chicago, local scheduled task",
        weekdays=frozenset({0, 1, 2, 3, 4, 5, 6}),
        # 06:30 UTC is 01:30 CDT. The winter fire is 07:30 UTC, an hour later,
        # and a due time set to the later of the two would read every summer
        # run as early-and-therefore-missing. Earliest possible fire, always.
        minutes_utc=(6 * 60 + 30,),
        grace=timedelta(hours=3),
        counts="decks rendered",
        zero_means=(
            "27 of 183 promoted filers carried a valuation on 2026-09-11 and "
            "27 again on 2026-09-15, so zero is a broken run rather than a "
            "quiet night. This is the check CI cannot perform -- see check()"
        ),
    ),
}


@dataclass(frozen=True, slots=True)
class Beat:
    """One recorded run."""

    job: str
    rows_written: int
    observed_at: datetime
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class Status:
    """What the digest renders for one job."""

    job: str
    ok: bool
    detail: str
    note: str = ""


def record(
    job: str,
    rows_written: int,
    *,
    detail: str | None = None,
    con: Any = None,
) -> bool:
    """Append a heartbeat. Returns False when no Postgres is configured.

    Deliberately does not raise when Postgres is absent, for the same reason
    :func:`marketradar.manifest.record_stats` does not: local dev without
    Supabase is a hard requirement. The gate is :func:`check`, which runs in the
    digest where a database is always present.

    It does raise on an unknown job name. A typo would otherwise write a row
    nothing reads, which is indistinguishable from the job never running --
    the exact failure this module exists to catch, reintroduced one level up.
    """
    if job not in CADENCES:
        raise HeartbeatError(
            f"{job!r} has no cadence. Add it to CADENCES with the count that is "
            f"never legitimately zero for it. Known: {sorted(CADENCES)}"
        )
    from marketradar import storage

    con = con if con is not None else storage.connect(enable_http=False)
    if not storage.postgres_attached(con):
        log.info("no Postgres attached; heartbeat for %s not recorded", job)
        return False

    con.execute(
        f"INSERT INTO {storage.PG_ALIAS}.job_heartbeat "
        "(job, rows_written, detail) VALUES (?, ?, ?)",
        [job, int(rows_written), detail],
    )
    return True


def latest(job: str, con: Any = None) -> Beat | None:
    """The newest heartbeat for ``job``, or None if it has never run."""
    from marketradar import storage

    con = con if con is not None else storage.connect(enable_http=False)
    if not storage.postgres_attached(con):
        return None
    row = con.execute(
        f"SELECT job, rows_written, observed_at, detail "
        f"FROM {storage.PG_ALIAS}.job_heartbeat WHERE job = ? "
        "ORDER BY observed_at DESC LIMIT 1",
        [job],
    ).fetchone()
    if row is None:
        return None
    return Beat(str(row[0]), int(row[1]), row[2], row[3])


def check(con: Any = None, *, now: datetime | None = None) -> list[Status]:
    """One :class:`Status` per scheduled job, for the digest health block.

    **The deck job is the one CI genuinely cannot see, and that is why this
    check exists at runtime rather than as a test.** It runs as a Windows
    scheduled task on one laptop: no workflow invokes it -- ``tests/
    test_decks.py`` asserts that none ever does, because a deck is vendor data
    and a public Release asset would be redistribution -- so no amount of
    static analysis in ``tests/`` can tell whether the task is registered, or
    still registered, or whether the machine was awake at 01:30. Registration
    itself is not proof: the task was created on 2026-09-16 with a command line
    that silently truncated at a space, and ``schtasks`` reported SUCCESS.

    The only thing that can answer "did the decks get made" is a row this job
    wrote after making them. The same is true in weaker form of every cloud
    job -- GitHub disables a public repo's scheduled workflows after 60 days of
    inactivity, which no test can observe either.

    Never raises. The digest must still send when a sentinel has stopped; a
    stopped sentinel is precisely what the reader needs to be told about, and a
    health probe that takes the email down with it reports nothing to nobody.
    """
    now = now or datetime.now(timezone.utc)
    out: list[Status] = []
    for job in sorted(CADENCES):
        cadence = CADENCES[job]
        try:
            beat = latest(job, con)
        except Exception as exc:  # pragma: no cover - needs a live database
            out.append(Status(job, False, f"unavailable: {exc}"[:70]))
            continue
        out.append(_status(cadence, beat, now))
    return out


def _status(cadence: Cadence, beat: Beat | None, now: datetime) -> Status:
    """Age first, then the count. Both are loud; neither is a bare number."""
    if beat is None:
        return Status(
            cadence.job, False,
            f"never run ({cadence.label})",
            note="no heartbeat has ever been written for this job",
        )

    observed = beat.observed_at
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    age = now - observed
    stamp = f"{_ago(age)} ago"

    required = cadence.required_since(now)
    if required is not None and observed < required:
        return Status(
            cadence.job, False,
            f"STALE -- last ran {stamp} ({cadence.label})",
            note=(f"due since {required:%Y-%m-%d %H:%M} UTC and has not run; "
                  f"{cadence.job} is not writing"),
        )

    # Ran on time, and wrote nothing. This is the quiet failure: on time, exit
    # zero, no output. It is louder than staleness rather than softer.
    if beat.rows_written == 0:
        return Status(
            cadence.job, False,
            f"ran {stamp} and wrote NOTHING (0 {cadence.counts})",
            note=f"zero is broken here: {cadence.zero_means}",
        )

    return Status(
        cadence.job, True,
        f"{beat.rows_written:,} {cadence.counts}, {stamp}",
    )


def _ago(delta: timedelta) -> str:
    """Compact age. The digest console is cp1252, so ASCII only."""
    secs = int(delta.total_seconds())
    if secs < 0:
        return "0m"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"
    return f"{secs // 86400}d{(secs % 86400) // 3600:02d}h"
