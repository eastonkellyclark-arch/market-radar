-- Market Radar — macro time series
--
-- Apply with:  uv run mr migrate
-- Idempotent, like 001 and 002.
--
-- Rates and spreads from FRED. Small and slow-moving -- a few thousand rows
-- that grow by three a day -- so this lives in Postgres and is queried
-- directly rather than going through Parquet. The Release asset is the public
-- mirror, not the query path.

create table if not exists macro_series (
    id          bigserial primary key,
    series_id   text   not null,          -- 'DGS10' | 'BAMLH0A0HYM2' | ...
    obs_date    date   not null,
    value       numeric,                  -- NULL is real: see the comment below
    source      text   not null,
    ingested_at timestamptz not null default now(),
    constraint macro_series_natural_key unique (series_id, obs_date, source)
);

create index if not exists macro_series_lookup_idx
    on macro_series (series_id, obs_date desc);

comment on table macro_series is
    'Macro time series: Treasury yields, credit spreads. One row per '
    '(series, date). Upsert on the natural key -- FRED revises recent '
    'observations, so a re-run must overwrite the value rather than '
    'duplicate the date.';

comment on column macro_series.value is
    'NULL where the source published no print for that date. FRED sends "." '
    'for market holidays, and that is not the same thing as a missing row: '
    'a NULL means we asked and there was no observation, whereas an absent '
    'row means we never asked. Queries that want a level must filter '
    'value is not null and take the most recent remaining row.';
