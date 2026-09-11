-- What one share receives, in a shape that can hold what deals actually say.
--
-- `proxy_figure.consideration_per_share` was a single number, and measured over
-- 15 real takeout proxies it is wrong for 4 of them:
--
--   Enviri            "not to be less than $14.50 per share and not to exceed
--                     $16.50" -- a collar, so there is no single price
--   CoreCard          a *ratio* collar, 0.2783 to 0.3142 depending on where the
--                     acquirer's stock prints
--   Veeco             0.265 acquirer shares **and** $10.15 in cash -- a mix, and
--                     neither half is the consideration
--   Norfolk Southern  one share **and** $88.82 in cash, same shape
--   FONAR             $19.00 for Common and Class B, $6.34 for Class C -- two
--                     classes, two prices
--
-- **A collar recorded as its upper bound is a wrong number that looks right.**
-- The model returned 0.3142 for CoreCard, which is one end of its collar; scored
-- against a scalar it reads as an error, and stored as a scalar it would read as
-- a fact. Neither is what the filing says.
--
-- So: one row per (accession, share class, component). A point value has
-- low = high. A collar has low < high. A mix is two rows for the same class. Two
-- classes are two sets of rows. The shape of the deal is the shape of the data.
--
-- **The scalar is derived and never stored.** `scalar_consideration` below
-- returns a number only when there is exactly one class, exactly one component,
-- that component is cash, and low = high. Every other deal gets NULL and a
-- reason, because the alternative is a column that silently means something
-- different per row -- which is the failure this whole file exists to avoid.

create table if not exists proxy_consideration (
    id              bigserial primary key,

    accession       text not null,
    cik             text not null,
    -- 'common' where a filing has one class. FONAR needs three.
    share_class     text not null default 'common',
    -- 'cash' or 'acquirer_shares'. A mix is one row of each.
    component       text not null,

    -- low = high for a point value; low < high for a collar. Both NULL unless
    -- the reason is 'stated'.
    low             numeric(28,6),
    high            numeric(28,6),
    -- 'USD', 'CAD', ... for cash; NULL for a share ratio, which has no currency.
    -- Recorded because a proxy can state a price in a currency that is not the
    -- reporting one: Royal Gold's document contains "C$2.00 in cash per common
    -- share" belonging to a different deal inside it.
    currency        text,

    reason          text not null,

    -- provenance, as for proxy_figure -------------------------------------
    quote           text,
    section         text,
    section_heading text,
    section_start   integer,
    section_end     integer,
    provider        text,
    model           text,
    prompt_version  text not null,

    -- What the model says the figure is *of*: the entity whose shares receive
    -- it, the currency, the share class. The citation check proves a number was
    -- read rather than invented and says nothing about what it is of, which is
    -- the error that matters -- a merger sub's conversion read as target
    -- consideration, another deal's C$2.00, a comparables percentile read as
    -- this deal's premium.
    attributed_to   text,
    attribution_ok  boolean,

    extracted_at    timestamptz not null default now(),

    constraint proxy_consideration_key
        unique (accession, share_class, component, prompt_version),
    constraint proxy_consideration_component
        check (component in ('cash', 'acquirer_shares')),
    constraint proxy_consideration_reason
        check (reason in ('stated', 'not_stated', 'not_parsed', 'no_section',
                          'uncited', 'not_a_takeout', 'misattributed')),
    -- A value only on a row claiming to have found one, a citation with it, and
    -- a range that is the right way round. The table refuses an unauditable or
    -- incoherent number rather than trusting the writer.
    constraint proxy_consideration_value_needs_reason check (
        (reason = 'stated' and low is not null and high is not null
         and high >= low and quote is not null)
        or (reason <> 'stated' and low is null and high is null)
    ),
    -- A share ratio has no currency and cash must name one.
    constraint proxy_consideration_currency check (
        (component = 'cash' and (reason <> 'stated' or currency is not null))
        or (component = 'acquirer_shares' and currency is null)
    )
);

create index if not exists proxy_consideration_accession_idx
    on proxy_consideration (accession, share_class);

-- The scalar, derived. NULL unless the deal genuinely has one, with the reason
-- it does not alongside, so a consumer can tell a collar from a mix from an
-- unread filing rather than seeing three NULLs that mean different things.
create or replace view proxy_consideration_scalar as
with per_filing as (
    select accession,
           cik,
           count(distinct share_class)                               as classes,
           count(*) filter (where reason = 'stated')                 as stated,
           count(*) filter (where reason = 'stated'
                            and component = 'acquirer_shares')       as share_legs,
           count(*) filter (where reason = 'stated'
                            and component = 'cash')                  as cash_legs,
           count(*) filter (where reason = 'stated' and low <> high) as collars,
           min(currency) filter (where component = 'cash')           as currency,
           max(low) filter (where component = 'cash')                as cash_low,
           max(high) filter (where component = 'cash')               as cash_high
    from proxy_consideration
    group by accession, cik
)
select accession,
       cik,
       case
           when stated = 0                then null
           when classes > 1               then null
           when share_legs > 0 and cash_legs > 0 then null
           when share_legs > 0            then null
           when collars > 0               then null
           else cash_low
       end                                                 as usd_per_share,
       case
           when stated = 0                then 'not_read'
           when classes > 1               then 'per_class'
           when share_legs > 0 and cash_legs > 0 then 'mixed'
           when share_legs > 0            then 'shares_only'
           when collars > 0               then 'collar'
           else 'scalar'
       end                                                 as shape,
       currency,
       cash_low,
       cash_high
from per_filing;
