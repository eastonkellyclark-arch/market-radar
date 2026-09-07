-- Market Radar — ticker mapping
--
-- Apply with:  uv run mr migrate
-- Idempotent, like 001.
--
-- A company can have several tickers at once (share classes: BRK.A/BRK.B,
-- GOOG/GOOGL) and a ticker can belong to different companies at different
-- times (recycled after delisting). `companies.ticker` is a single column and
-- can represent neither, so the real mapping lives here.
--
-- The pair is deliberately NOT unique on ticker alone. Anything that makes
-- ticker unique re-introduces exactly the bug the schema comment in 001 warns
-- about.

create table if not exists company_tickers (
    id          bigserial primary key,
    company_id  bigint not null references companies (id) on delete cascade,
    ticker      text   not null,
    source      text   not null,        -- 'sec_company_tickers' | 'tiingo' | ...
    first_seen  date   not null default current_date,
    last_seen   date   not null default current_date,
    constraint company_tickers_natural_key unique (company_id, ticker, source)
);

-- Not unique: several companies can legitimately share a ticker string across
-- time, and one company holds several at once.
create index if not exists company_tickers_ticker_idx
    on company_tickers (ticker);

create index if not exists company_tickers_company_idx
    on company_tickers (company_id);

comment on table company_tickers is
    'Ticker <-> company mapping, many-to-many over time. Resolve a ticker '
    'through here and expect a LIST back, not one row: share classes give a '
    'company several tickers at once, and recycling gives a ticker several '
    'companies across time. Join on company_id or companies.cik, never on '
    'the ticker string.';

comment on column company_tickers.last_seen is
    'Last date this pair appeared in the source file. A pair that stops '
    'appearing is not deleted -- it is history, and the forward-return joins '
    'need it.';
