-- Market Radar — initial schema
--
-- Apply with:  uv run mr migrate --apply
-- Idempotent: every object uses IF NOT EXISTS, so re-running is a no-op.
--
-- Design notes that are easy to get wrong later:
--
--   * ticker is NOT unique and is never a join key across time. Share
--     classes mean one company has several (BRK.A, BRK.B), and tickers are
--     recycled after delisting, so the same string can mean two different
--     companies in two different years. Join on companies.id, or on cik for
--     public filers. Nothing else.
--
--   * Every table that ingests repeatedly carries a natural-key unique
--     constraint. "Jobs are idempotent" is not a convention you can hold in
--     your head; it has to be the database's problem.
--
--   * No embeddings table and no halfvec. Similarity ranking is a
--     refinement inside an already-filtered result set, so the row count
--     will be small and the storage optimisation is premature. Add it when
--     something actually needs it.


-- ==========================================================================
-- companies — the entity spine
-- ==========================================================================
create table if not exists companies (
    id          bigserial primary key,
    cik         text,
    ticker      text,
    name        text not null,
    normalized  text not null,        -- lowercased, suffix-stripped, for fuzzy match
    sic         text,
    naics       text,
    ein         text,
    is_public   boolean not null default false,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

-- CIK identifies a public filer uniquely. Private companies have none, and
-- Postgres permits many NULLs in a unique index, which is what we want.
create unique index if not exists companies_cik_key
    on companies (cik) where cik is not null;

-- Deliberately NOT unique: share classes and recycled tickers.
create index if not exists companies_ticker_idx     on companies (ticker);
create index if not exists companies_normalized_idx on companies (normalized);
create index if not exists companies_naics_idx      on companies (naics) where naics is not null;
create index if not exists companies_ein_idx        on companies (ein)   where ein   is not null;

comment on column companies.ticker is
    'Not unique and not stable over time. Never join on this across dates — '
    'share classes and recycled tickers. Use id, or cik for public filers.';


-- ==========================================================================
-- signals — everything the sentinels notice
-- ==========================================================================
create table if not exists signals (
    id          bigserial primary key,
    company_id  bigint references companies (id) on delete cascade,
    kind        text not null,      -- form4 | deal_filing | news | vol_screen | form5500
    source      text not null,
    occurred_at timestamptz not null,
    payload     jsonb not null,
    url         text,
    created_at  timestamptz not null default now()
);

-- Idempotency. Re-running yesterday's EDGAR poll must not duplicate rows.
-- company_id is nullable (a signal can arrive before the entity resolves),
-- and NULLs are distinct in a normal unique index, so coalesce to 0.
create unique index if not exists signals_natural_key
    on signals (kind, source, coalesce(company_id, 0), occurred_at);

create index if not exists signals_company_time_idx on signals (company_id, occurred_at desc);
create index if not exists signals_kind_time_idx    on signals (kind, occurred_at desc);


-- ==========================================================================
-- job_queue — Tier 2 work, drained inside the daily budget
-- ==========================================================================
create table if not exists job_queue (
    id         bigserial primary key,
    task       text not null,
    args       jsonb not null default '{}'::jsonb,
    priority   int  not null default 100,      -- lower runs first
    status     text not null default 'pending',
    attempts   int  not null default 0,
    locked_at  timestamptz,                    -- set on claim; stale locks reclaimable
    last_error text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint job_queue_status_check
        check (status in ('pending', 'running', 'done', 'failed'))
);

-- One pending copy of a given task+args. Enqueuing the same work twice
-- while it is still waiting is a no-op, but the same task may legitimately
-- be re-run later, so completed rows are exempt.
create unique index if not exists job_queue_pending_key
    on job_queue (task, args) where status = 'pending';

create index if not exists job_queue_drain_idx
    on job_queue (status, priority, created_at);

-- Finds locks orphaned by a killed worker.
create index if not exists job_queue_locked_idx
    on job_queue (locked_at) where status = 'running';


-- ==========================================================================
-- corporate_actions — the other half of raw price storage
-- ==========================================================================
-- Parquet holds unadjusted OHLCV; adjustment is a query-time join against
-- this table. That keeps history immutable and append-only, which is what
-- year-partitioning requires, and makes a bad adjustment a fixable bug
-- rather than a full re-download.
create table if not exists corporate_actions (
    id           bigserial primary key,
    ticker       text not null,
    ex_date      date not null,
    split_factor numeric(18, 8) not null default 1,   -- Tiingo splitFactor
    div_cash     numeric(18, 8) not null default 0,   -- Tiingo divCash
    source       text not null,
    ingested_at  timestamptz not null default now(),
    constraint corporate_actions_natural_key unique (ticker, ex_date, source),
    constraint corporate_actions_split_positive check (split_factor > 0)
);

create index if not exists corporate_actions_ticker_date_idx
    on corporate_actions (ticker, ex_date);

comment on table corporate_actions is
    'Keyed by ticker rather than company_id on purpose: actions are ingested '
    'from the price feed before entity resolution has necessarily run.';


-- ==========================================================================
-- dataset_stats — freshness observations
-- ==========================================================================
-- Appended, never overwritten, so "when did prices last actually grow?" is
-- a query rather than a guess. This is diagnostics: the correctness gate is
-- freshness.assert_fresh, which reads the data itself and needs no database.
create table if not exists dataset_stats (
    id          bigserial primary key,
    dataset     text   not null,
    partition   text   not null,
    row_count   bigint not null,
    max_date    date,
    observed_at timestamptz not null default now()
);

create index if not exists dataset_stats_lookup_idx
    on dataset_stats (dataset, partition, observed_at desc);
