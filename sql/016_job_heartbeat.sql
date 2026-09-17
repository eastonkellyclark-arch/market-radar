-- Market Radar — scheduled-job heartbeats
--
-- Apply with:  uv run mr migrate
-- Idempotent, like 001-015.
--
-- `dataset_stats` answers "did the data grow"; this answers "did the job run,
-- and did it do anything". They are different questions and the gap between
-- them is where this system keeps failing.
--
-- Measured 2026-09-16: prices had run green every scheduled night for ten
-- sessions, and in the same window the EDGAR sentinel, Form 4 clustering and
-- the companies refresh had not run at all -- for eight, seven and nine days
-- respectively. Nothing was red, because nothing was scheduled to go red. The
-- digest's health block could not see it either: its `companies` line prints a
-- row count with no max-age, so a table frozen since 2026-09-07 rendered as
-- `8,005 CIKs, 10,412 ticker rows` under HEALTH OK.
--
-- Append-only, same as `dataset_stats` and for the same reason: "when did this
-- job last actually write something" has to be a query rather than a guess,
-- and a table holding only the newest row cannot distinguish a job that has
-- run fifty times from one that ran once and stopped.

create table if not exists job_heartbeat (
    id           bigserial   primary key,
    job          text        not null,   -- 'edgar-poll' | 'sentinels.form4' | ...
    -- THE COUNT THAT IS NEVER LEGITIMATELY ZERO, and it is not the same
    -- quantity for every job. This column is the whole point of the table and
    -- naming it wrong makes the check useless in the quiet direction.
    --
    -- A heartbeat that only says "I ran" would have passed every one of the
    -- three failures that motivated this: `mr proxy` printed "3 documents
    -- located" and wrote zero rows three times; `mr symbols` could not run at
    -- all while its test stayed green; and 35 declared Release locations held
    -- nothing for weeks while the code that built them exited 0.
    --
    -- So each job records the count its *own* output is measured in, chosen so
    -- that zero means broken rather than quiet. Form 4 records filings parsed
    -- and NOT clusters found, because a day with no insider cluster is an
    -- ordinary day and a day with no Form 4s is a broken reader. Same
    -- distinction as `absent` against `unmapped`, and as `lapsed` against
    -- `declining`. See CADENCES in src/marketradar/heartbeat.py, which states
    -- the chosen quantity per job next to why it cannot be zero.
    rows_written bigint      not null,
    -- Everything the count above deliberately excludes: clusters found, decks
    -- skipped for want of a valuation, filings already seen. Free text, for
    -- reading rather than for asserting on.
    detail       text,
    observed_at  timestamptz not null default now()
);

create index if not exists job_heartbeat_lookup_idx
    on job_heartbeat (job, observed_at desc);
