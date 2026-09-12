-- A point-in-time CIK-to-ticker map, recovered from filing cover pages.
--
-- **Why this exists.** `company_tickers.json` is a *current* snapshot, and SEC's
-- submissions JSON returns an empty `tickers` array for a delisted company --
-- checked against Twitter, Activision, VMware and Seagen, all blank. So the
-- symbol a company traded under while it existed is not available from any
-- current-state source, and an acquisition target is by definition a company that
-- stopped existing.
--
-- Measured 2026-09-12: of 2,265 filers whose 10-K history has ended, **2,141
-- (94.3%) have no ticker at all** and only 85 have a ticker without prices. The
-- ticker map, not the price history, is the binding constraint on every question
-- that starts "what happened to the companies that were acquired" -- by a factor
-- of 25.
--
-- **What recovers it.** `dei:TradingSymbol` is tagged on the cover page of every
-- filing since the cover-page XBRL mandate. It returns TWTR, ATVI, VMW and SGEN
-- for the four above. Free, deterministic, keyed on the CIK that SEC assigns and
-- never reuses.
--
-- **Why one row per (cik, ticker) and not one per cik.** Both directions are
-- many-to-one over time and collapsing either loses the thing that makes the map
-- worth having:
--
--   a CIK changes ticker   -- a rename, a re-listing, a share-class change
--   a ticker changes CIK   -- recycling. 356 active symbols carry two different
--                             companies inside a ten-year pull, and SGEN itself
--                             is the example: a different issuer took the symbol
--                             after Seagen was acquired in 2023.
--
-- A recovered ticker that overwrote a recycled one would put us exactly back where
-- we started, with a map that looks complete and silently resolves the wrong
-- company. So the key carries both, and every lookup carries a date.

create table if not exists company_ticker_history (
    id          bigserial primary key,

    -- The identifier SEC assigns and never reuses. Zero-padded to ten, which is
    -- how Postgres stores it everywhere else here -- the XBRL partitions carry it
    -- unpadded and the two strings do not compare.
    cik         text not null,
    ticker      text not null,
    -- The exchange as the cover page states it, where it states one.
    exchange    text,

    -- **Bounds, not a lifetime.** The earliest and latest *filing date* on which
    -- this symbol was observed for this CIK. The symbol was in use before the
    -- first filing that mentions it and usually after the last, so the range
    -- understates in both directions -- the same rule as Form 5500's
    -- PLAN_EFF_DATE, which gives the oldest plan a sponsor still files rather than
    -- the company's age. Renders as "seen between", never "traded from".
    first_seen  date not null,
    last_seen   date not null,
    -- How many filings carried it. One observation is a point, not a range, and a
    -- consumer should be able to tell those apart.
    filings     integer not null default 1,

    source      text not null default 'dei:TradingSymbol',
    updated_at  timestamptz not null default now(),

    constraint cth_key unique (cik, ticker),
    constraint cth_range check (last_seen >= first_seen),
    constraint cth_filings check (filings >= 1),
    -- A symbol is 1-6 characters plus the class suffixes SEC uses. Checked
    -- because this column is joined against price data, and a malformed symbol
    -- would silently match nothing rather than erroring.
    constraint cth_symbol check (ticker ~ '^[A-Z][A-Z0-9.\-]{0,8}$')
);

create index if not exists cth_ticker_idx on company_ticker_history (ticker, first_seen);
create index if not exists cth_cik_idx on company_ticker_history (cik, first_seen);

-- Which company held a symbol on a given date. **This is the query the recycling
-- problem exists for**, and it is a view rather than a convention so that no
-- consumer has to remember to bound by date.
--
-- Overlapping ranges are possible and are not hidden: two rows can both cover a
-- date when the observation windows straddle a handover, and `cik_count` says so
-- rather than one of them being picked arbitrarily. A caller seeing cik_count > 1
-- for a symbol has an ambiguity to resolve, not an answer to trust -- which is the
-- review-queue rule applied to a join instead of to a name.
create or replace view ticker_owner as
select h.ticker,
       h.cik,
       h.first_seen,
       h.last_seen,
       h.filings,
       count(*) over (partition by h.ticker) as cik_count
from company_ticker_history h;

-- The symbols a price vendor would be asked for: recovered, and absent from the
-- current map. This is the list that turns a quote into a real quote.
create or replace view ticker_history_recovered as
select h.cik, h.ticker, h.exchange, h.first_seen, h.last_seen, h.filings
from company_ticker_history h
left join companies c
       on lpad(cast(c.cik as varchar), 10, '0') = h.cik
left join company_tickers t
       on t.company_id = c.id and t.ticker = h.ticker
where t.ticker is null;
