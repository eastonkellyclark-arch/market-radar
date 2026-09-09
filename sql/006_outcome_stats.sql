-- Market Radar — forward-return study results
--
-- Apply with:  uv run mr migrate
-- Idempotent, like 001-005.
--
-- The study itself is a join between an event date and eleven years of price
-- history: twenty million bars read from R2, which takes minutes. The
-- dashboard must not do that to render a panel, and the digest must not do it
-- at 03:30. So `mr outcomes` computes and this table holds the answer.
--
-- Summary rows only, never per-event rows. The per-event table would be a
-- data file by another name, and the manifest rule sends those to R2. What is
-- worth keeping in Postgres is the conclusion and enough shape to know
-- whether to believe it -- which is why n_suspect is a column and not a
-- footnote.

create table if not exists outcome_stats (
    id            bigserial primary key,
    study         text    not null,      -- 'form4_cluster' | 'deal_8k'
    slice         text    not null,      -- 'all' | a group label
    horizon       integer not null,      -- trading sessions after the anchor
    n             integer not null,
    n_suspect     integer not null,
    median_ret    numeric,
    mean_ret      numeric,
    median_excess numeric,
    mean_excess   numeric,
    win_rate      numeric,
    median_run_up numeric,
    events        integer,               -- population before pricing
    priced        integer,               -- how many produced any return
    benchmark     text    not null,
    computed_at   timestamptz not null default now(),
    constraint outcome_stats_key unique (study, slice, horizon)
);

create index if not exists outcome_stats_lookup_idx
    on outcome_stats (study, horizon);

comment on table outcome_stats is
    'Forward returns after an event, at 1/5/30 trading sessions. Pure SQL: '
    'no LLM, no embeddings, and it can therefore precede every expensive '
    'thing rather than justify it afterwards.';

comment on column outcome_stats.horizon is
    'Trading sessions after the anchor, not calendar days. Horizon 1 is the '
    'event session itself. Thirty calendar days spans a different number of '
    'sessions depending on where the holidays fall, and returns measured '
    'over a varying window are not comparable across events.';

comment on column outcome_stats.median_excess is
    'Median return less the benchmark over the same two dates. The median '
    'leads because event-study distributions are not normal -- one 900% '
    'takeout moves a mean of ten thousand events and says nothing about the '
    'next one. mean_excess sits beside it so a large gap is visible.';

comment on column outcome_stats.n_suspect is
    'Events excluded as suspected unrecorded splits: an absolute return past '
    '300% with no corporate action on record. This is not a rounding detail. '
    'corporate_actions holds 365 splits across eleven years and 2,947 '
    'tickers, which is far short of reality -- AYTU''s 1-for-20 reverse '
    'split of 2023-01-06 is simply absent, and its absence reads as +1,751%. '
    'Thirteen such events moved the mean excess return of one study from '
    '+5% to +944%. They are excluded from the statistics and counted here so '
    'the exclusion is never invisible.';

comment on column outcome_stats.priced is
    'How many of `events` produced any return at all. The gap is not noise: '
    'a delisted acquisition target has no +30-session close, and the deal '
    'closing is precisely why its history ends. The drops correlate with the '
    'outcome, so the ratio has to be read alongside the return.';
