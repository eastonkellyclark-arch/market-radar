"""Whether a scheduled job ran, and whether it wrote anything.

**The deck job is the one CI cannot see, and that is the whole reason the real
check lives at runtime in the digest rather than in this file.** `mr decks
--promoted` runs as a Windows scheduled task on one laptop. No workflow invokes
it and none ever may -- ``tests/test_decks.py`` asserts that, because a deck is
vendor data and a public Release asset would be redistribution -- so nothing in
``tests/`` can observe whether the task is registered, still registered, or
whether the machine was even awake at 01:30. Registration is not proof either:
the task was created on 2026-09-16 with a command line that silently truncated
at the space in "Market Radar", and ``schtasks`` reported SUCCESS.

So what this file can test is the *logic* -- that a stale heartbeat is loud,
that a zero is louder, that the due-time arithmetic does not false-alarm every
Tuesday. Whether the deck task actually ran last night is answerable only by a
row it wrote, read by the digest. That asymmetry is deliberate and is the point
of the module.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from marketradar import heartbeat
from marketradar.heartbeat import CADENCES, Beat, Cadence, HeartbeatError, _status

REPO = Path(__file__).resolve().parents[1]


def utc(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


# --- the arithmetic a flat max-age gets wrong -----------------------------


def test_a_flat_daily_age_would_be_red_every_tuesday() -> None:
    """The case that forced fire-time arithmetic instead of a max age.

    `prices` runs Tue-Sat, so the sentinels chained to it do too. On a Tuesday
    morning the newest sentinels heartbeat is **Saturday's** -- three days and
    change old -- because Sunday and Monday have no sweep. A flat "daily, warn
    after 30h" reads that as a stopped job every single Tuesday, and a check
    that cries wolf weekly is a check that gets ignored.

    Tuesday 2026-09-15 10:22 UTC is the real digest time from that morning's
    run. Saturday 2026-09-12 is the previous scheduled day.
    """
    cadence = CADENCES["sentinels.form4"]
    now = utc(2026, 9, 15, 10, 22)

    # 76 hours old, and correct.
    saturday_run = Beat("sentinels.form4", 2351, utc(2026, 9, 12, 10, 30))
    status = _status(cadence, saturday_run, now)
    assert status.ok, (
        f"Saturday's run read as stale on Tuesday morning: {status.detail}")

    # Friday's, though, means Saturday never happened.
    friday_run = Beat("sentinels.form4", 2351, utc(2026, 9, 11, 10, 30))
    assert not _status(cadence, friday_run, now).ok


def test_required_since_falls_back_when_inside_the_grace_window() -> None:
    """The digest always runs mid-window, so the newest fire is never usable.

    Both the digest and the sentinels chain on the same sweep finishing, so
    they execute at the same time. At that moment today's fire has not had its
    grace elapse, and comparing against it would mark a healthy job stale every
    day. `required_since` therefore falls back to the previous fire.

    Removing that fallback -- `return fires[0]` unconditionally -- turns this
    red and also the Tuesday test above.
    """
    cadence = CADENCES["sentinels.form4"]
    now = utc(2026, 9, 15, 10, 22)          # Tue, inside the 10h grace on 03:30
    required = cadence.required_since(now)
    assert required == utc(2026, 9, 12, 3, 30), (
        f"expected Saturday's fire, got {required}")

    # Once the grace has elapsed, today's fire is the bar.
    later = utc(2026, 9, 15, 20, 0)
    assert cadence.required_since(later) == utc(2026, 9, 15, 3, 30)


def test_the_deck_due_time_is_the_earliest_possible_fire_not_the_latest() -> None:
    """01:30 America/Chicago is 06:30 UTC in summer and 07:30 in winter.

    The cadence must use the *earlier* of the two. Set it to 07:30 and every
    summer run -- which lands about 06:35 UTC -- reads as older than its own due
    time and is reported missing, nightly, for eight months of the year.

    Mutation: change `minutes_utc` for "decks" to 7*60+30 and this goes red.
    """
    cadence = CADENCES["decks"]
    assert cadence.minutes_utc == (6 * 60 + 30,), (
        "the deck due time must be 06:30 UTC (01:30 CDT), the earliest fire")

    summer_run = Beat("decks", 27, utc(2026, 9, 17, 6, 35))
    now = utc(2026, 9, 17, 12, 0)           # past the 3h grace
    assert _status(cadence, summer_run, now).ok


# --- the two failures the table exists for --------------------------------


def test_a_job_that_ran_on_time_and_wrote_nothing_is_not_ok() -> None:
    """The quiet failure: on time, exit 0, no output.

    This is `mr proxy` printing "3 documents located" and writing zero rows
    three times. A heartbeat that recorded only a timestamp would be green
    here, which is the entire reason `rows_written` is a column.
    """
    cadence = CADENCES["sentinels.form4"]
    now = utc(2026, 9, 15, 20, 0)
    fresh_but_empty = Beat("sentinels.form4", 0, utc(2026, 9, 15, 10, 30))
    status = _status(cadence, fresh_but_empty, now)
    assert not status.ok
    assert "NOTHING" in status.detail
    assert cadence.zero_means[:20] in status.note, (
        "the reason zero is broken for *this* job must travel with the failure;"
        " a bare 'wrote 0' leaves the reader to guess whether that is normal")


def test_a_job_that_never_ran_is_not_ok() -> None:
    """No row at all. Distinct from a stale row, and the note says which."""
    status = _status(CADENCES["decks"], None, utc(2026, 9, 17, 12, 0))
    assert not status.ok
    assert "never run" in status.detail


def test_recording_an_unknown_job_raises() -> None:
    """A typo would write a row nothing reads.

    Which is indistinguishable from the job never running -- the exact failure
    this module exists to catch, reintroduced one level up. So it raises rather
    than inserting.
    """
    with pytest.raises(HeartbeatError, match="has no cadence"):
        heartbeat.record("sentinels.form-4", 100)


# --- coverage: nothing scheduled is unmonitored ---------------------------

#: Commands a workflow runs that deliberately have no heartbeat, and why. Each
#: is already observable by a different mechanism; a heartbeat would be a second
#: thing to maintain that says nothing new.
NOT_HEARTBEATED = {
    "prices": (
        "assert_fresh + dataset_stats already cover it, and a red sweep sends "
        "no digest -- operating.md: 'no email is itself a signal'"
    ),
    "digest": "the digest is the report; its non-arrival is the signal",
    "manifest": "a gate inside the digest job -- its failure turns the digest red",
    "fred": "continue-on-error by design; a stale macro line renders as stale",
}

#: `uv run mr <command>` -> the cadence it must have.
JOB_FOR_COMMAND = {
    "edgar": "edgar-poll",
    "form4": "sentinels.form4",
    "deals": "sentinels.deals",
    "sec-tickers": "sentinels.sec-tickers",
}


def test_every_command_a_workflow_runs_is_watched_or_exempted() -> None:
    """A scheduled job nobody watches is the thing this module exists to stop.

    Walks the workflows rather than a list in this file, so adding a job to
    Actions without adding a cadence fails the build. It cannot see the deck
    task -- see the module docstring -- which is why the deck cadence is
    asserted separately below.

    Mutation: delete "edgar" from JOB_FOR_COMMAND and this reports edgar-poll
    as unwatched; delete the `edgar-poll` entry from CADENCES and it does the
    same from the other direction.
    """
    workflows = sorted((REPO / ".github" / "workflows").glob("*.yml"))
    assert workflows, "no workflows found; this check would pass vacuously"

    commands: set[str] = set()
    for path in workflows:
        if path.name == "tests.yml":
            continue
        commands |= set(re.findall(r"uv run mr ([a-z0-9-]+)", path.read_text(encoding="utf-8")))
    assert commands, "no `uv run mr` commands found; check the regex"

    unwatched = []
    for cmd in sorted(commands):
        if cmd in NOT_HEARTBEATED:
            continue
        job = JOB_FOR_COMMAND.get(cmd)
        if job is None:
            unwatched.append(f"`mr {cmd}` runs in a workflow with no cadence and "
                             "no entry in NOT_HEARTBEATED")
        elif job not in CADENCES:
            unwatched.append(f"`mr {cmd}` maps to {job!r}, which is not in CADENCES")
    assert not unwatched, "\n".join(unwatched)


def test_the_local_deck_task_has_a_cadence_no_workflow_can_supply() -> None:
    """The one job whose schedule lives outside the repo entirely.

    No workflow runs `mr decks`, so the coverage walk above cannot reach it and
    never will. Its cadence is asserted by name here instead -- the only place
    in the suite that knows the deck job is supposed to run at all.
    """
    assert "decks" in CADENCES
    cadence = CADENCES["decks"]
    assert cadence.weekdays == frozenset(range(7)), "the deck task runs nightly"
    assert "local scheduled task" in cadence.label, (
        "the label is what the digest prints beside a stale deck job; it has to "
        "say where the schedule lives, because the reader's next move is "
        "`schtasks` on a laptop rather than anything in Actions")


def test_every_cadence_states_what_zero_means_for_it() -> None:
    """`counts` and `zero_means` are not documentation, they are rendered.

    `_status` puts `counts` in the detail line and `zero_means` in the note, so
    an empty one ships a health failure that says "wrote NOTHING (0 )" and
    leaves the reader no way to tell a broken job from a quiet one.
    """
    for job, cadence in CADENCES.items():
        assert cadence.counts.strip(), f"{job} does not say what it counts"
        assert len(cadence.zero_means) > 40, (
            f"{job} does not say why zero means broken for it")
        assert cadence.minutes_utc, f"{job} has no fire times"
        assert cadence.weekdays, f"{job} has no scheduled days"


def test_the_cadences_match_the_crons_the_workflows_actually_declare() -> None:
    """Drift between the schedule and the thing watching it.

    A cron changed in YAML without the cadence following would move the due
    times under the check: it would keep passing, against the wrong schedule.
    Only the fields a cron can express are compared -- the deck task has no
    cron to read, which is the asymmetry again.

    Mutation: change edgar-poll's cron to `7 12-23 * * 1-5` (hourly) and this
    goes red on the minutes.
    """
    text = (REPO / ".github" / "workflows" / "edgar-poll.yml").read_text(encoding="utf-8")
    crons = re.findall(r'cron: "(.+?)"', text)
    assert crons == ["7,37 12-23 * * 1-5"], f"edgar-poll cron changed: {crons}"

    cadence = CADENCES["edgar-poll"]
    expected = tuple(h * 60 + m for h in range(12, 24) for m in (7, 37))
    assert cadence.minutes_utc == expected
    assert cadence.weekdays == frozenset({0, 1, 2, 3, 4})

    text = (REPO / ".github" / "workflows" / "sentinels.yml").read_text(encoding="utf-8")
    assert '- cron: "7 9 * * 0"' in text, "the weekly cron moved"
    weekly = CADENCES["sentinels.sec-tickers"]
    assert weekly.weekdays == frozenset({6}) and weekly.minutes_utc == (9 * 60 + 7,)


def test_every_cadence_has_a_record_call_and_every_call_has_a_cadence() -> None:
    """The two halves must match, and they fail in opposite directions.

    A cadence with no `heartbeat.record` behind it reports "never run" forever
    -- loud, but a false alarm that trains the reader to ignore the block. A
    `record` call whose job is not in CADENCES raises at runtime, which is
    better, but only on the night the job next runs.

    Mutations: delete the `heartbeat.record("decks", len(made), ...)` call from
    cli.py and this reports decks as declared-but-never-written; add a call with
    a typo'd name and it reports the reverse.
    """
    src = REPO / "src" / "marketradar"
    called: set[str] = set()
    for path in sorted(src.rglob("*.py")):
        if path.name == "heartbeat.py":
            continue
        called |= set(re.findall(r'heartbeat\.record\(\s*"([^"]+)"',
                                 path.read_text(encoding="utf-8")))
    assert called, "no heartbeat.record calls found in src/; check the regex"

    declared = set(CADENCES)
    missing_call = declared - called
    assert not missing_call, (
        f"declared in CADENCES but nothing writes them: {sorted(missing_call)} -- "
        "the digest will report these as 'never run' every night, forever")
    unknown_job = called - declared
    assert not unknown_job, (
        f"heartbeat.record called with no cadence: {sorted(unknown_job)} -- "
        "record() raises on these at runtime, on the night the job next runs")
