-- Where to read in a proxy, and the few tables worth storing from it.
--
-- **The locator ships; the extraction is declined.** Measured 2026-09-12 over 20
-- consecutive DEFM14A filings, hand-checked against a reading of each document:
-- per-figure accuracy was 62% across 45 cells, and the population argument decides
-- it regardless -- proxies exist only for companies being acquired, so ~500
-- documents against 2,564 valued filers improves a growth input for under 20% of
-- valuations, and never for the filers whose growth constant is most wrong.
-- See docs/build-spec.md, "Proxy extraction: measured 2026-09-12, declined".
--
-- What survives is the part that works. A DEFM14A is ~1.26 million characters and
-- the part worth reading is a few thousand; deterministic heading patterns find it
-- every time, free, with no model involved. So this table is a reading aid: one
-- row per (accession, section) saying where to open the document and why that
-- window was chosen.

create table if not exists proxy_section (
    id              bigserial primary key,

    accession       text not null,
    cik             text not null,
    form            text not null,
    -- merger_consideration | premium_statement | prospective_financial |
    -- fairness_opinion
    section         text not null,

    -- Where to open the file. Character offsets into the visible text of the
    -- main document, which is what `proxy.fetch_document` returns.
    char_start      integer not null,
    char_end        integer not null,
    -- The text the locator matched, so a reader can see *why* this window and not
    -- one of the other places the heading appears.
    heading         text,
    -- How many currency amounts, percentages and bare ratios the window holds,
    -- and how many places the heading matched at all. Selection is by figure
    -- density rather than position: in a real proxy the same heading matches in
    -- the table of contents, the body, the tax discussion and the appended merger
    -- agreement, so "first" lands in the contents and "last" in the annex.
    figures         integer,
    candidates      integer,

    located_at      timestamptz not null default now(),

    constraint proxy_section_key unique (accession, section),
    constraint proxy_section_span check (char_end > char_start),
    constraint proxy_section_name check (
        section in ('merger_consideration', 'premium_statement',
                    'prospective_financial', 'fairness_opinion')
    )
);

create index if not exists proxy_section_cik_idx on proxy_section (cik, section);

-- Management projections, stored **only when the table passes its own
-- arithmetic**.
--
-- This is the one extracted field that survived the decline, and it survived for a
-- specific reason: a projections table is a labelled multi-year grid, so a wrong
-- one is catchable without knowing the right answer. Years must run consecutively,
-- EBITDA must sit below revenue, a margin must be plausible, a series must not
-- jump tenfold between adjacent years, capex must not exceed revenue, EBIT must
-- not exceed EBITDA. A table failing any of those is `incoherent` -- a stronger
-- statement than `not_parsed`, and the only reason code in this system that needs
-- no ground truth.
--
-- Measured: 5 of 15 takeouts produced a coherent table (33%), and **76 of 76
-- values in those five appear verbatim in their filings**. High precision on low
-- yield, where the consideration was low accuracy on high yield.
--
-- `source` is not decoration. Every row here is `extracted` -- read by a model and
-- gated by arithmetic -- and must never be confused with a figure a filer stated
-- and we parsed deterministically. A consumer joining this to XBRL is joining a
-- forecast to a fact.
create table if not exists proxy_projection (
    id              bigserial primary key,

    accession       text not null,
    cik             text not null,
    fiscal_year     integer not null,

    revenue         numeric(28,4),
    ebitda          numeric(28,4),
    ebit            numeric(28,4),
    net_income      numeric(28,4),
    free_cash_flow  numeric(28,4),
    capex           numeric(28,4),

    -- As the filing prints them. **Never converted**: a units guess is how a
    -- $1.2B projection becomes $1.2M.
    units           text,
    -- "Management Case", "Sensitivity Case", "Base Case". Proxies routinely carry
    -- more than one and blending them would be the midpoint mistake again, so the
    -- case is named and one is stored.
    scenario        text,

    -- Always 'extracted' here. A column rather than a comment because the
    -- distinction is the thing a consumer most needs and most easily forgets.
    source          text not null default 'extracted',

    -- provenance, as everywhere else a model produced a number
    quote           text,
    section_start   integer,
    provider        text,
    model           text,
    prompt_version  text not null,
    extracted_at    timestamptz not null default now(),

    constraint proxy_projection_key
        unique (accession, fiscal_year, scenario, prompt_version),
    constraint proxy_projection_source check (source in ('extracted')),
    -- The arithmetic, enforced at the table and not only in Python. A row that
    -- reaches here has passed the self-check; these are the subset of those checks
    -- expressible per row, so the table cannot be written into an incoherent state
    -- by a future caller that skips `check_projections`.
    constraint proxy_projection_ebitda_below_revenue check (
        revenue is null or ebitda is null or revenue <= 0 or ebitda <= revenue
    ),
    constraint proxy_projection_ebit_below_ebitda check (
        ebitda is null or ebit is null or ebit <= ebitda
    ),
    constraint proxy_projection_capex_below_revenue check (
        revenue is null or capex is null or revenue <= 0
        or abs(capex) <= revenue
    ),
    -- A projection with no measure on it is not a projection.
    constraint proxy_projection_has_a_measure check (
        revenue is not null or ebitda is not null or ebit is not null
        or net_income is not null or free_cash_flow is not null
        or capex is not null
    )
);

create index if not exists proxy_projection_cik_idx
    on proxy_projection (cik, fiscal_year);

-- Compound growth across each stored series, which is the number a DCF would want
-- and deliberately does *not* get: the growth constant stays, because this covers
-- only companies being acquired. Exposed as a view so the arithmetic is in one
-- place if that ever changes.
-- Written with window functions rather than `min_by`/`max_by`: those are DuckDB
-- and this view lives in Postgres. The first draft used them and the migration
-- failed -- the analytical engine and the row store do not share a dialect, and a
-- view is the one place in this codebase where that matters.
--
-- Endpoints are taken over rows where the measure is *positive*, because a CAGR
-- through zero or a negative base is not a growth rate. A series that is negative
-- at either end returns NULL rather than a number, which is the same rule as the
-- derived consideration scalar: no value beats an invented one.
create or replace view proxy_projection_growth as
with ranked as (
    select accession, cik, scenario, fiscal_year, revenue, ebitda,
           row_number() over (partition by accession, scenario
                              order by fiscal_year)                  as rn_asc,
           row_number() over (partition by accession, scenario
                              order by fiscal_year desc)             as rn_desc,
           row_number() over (partition by accession, scenario
                              order by case when revenue > 0 then 0 else 1 end,
                                       fiscal_year)                  as rev_first_rn,
           row_number() over (partition by accession, scenario
                              order by case when revenue > 0 then 0 else 1 end,
                                       fiscal_year desc)             as rev_last_rn,
           row_number() over (partition by accession, scenario
                              order by case when ebitda > 0 then 0 else 1 end,
                                       fiscal_year)                  as eb_first_rn,
           row_number() over (partition by accession, scenario
                              order by case when ebitda > 0 then 0 else 1 end,
                                       fiscal_year desc)             as eb_last_rn
    from proxy_projection
),
bounds as (
    select accession, cik, scenario,
           count(*)                                                  as years,
           min(fiscal_year)                                           as first_year,
           max(fiscal_year)                                           as last_year,
           max(case when rev_first_rn = 1 and revenue > 0
                    then revenue end)                                 as revenue_first,
           max(case when rev_last_rn  = 1 and revenue > 0
                    then revenue end)                                 as revenue_last,
           max(case when rev_first_rn = 1 and revenue > 0
                    then fiscal_year end)                             as rev_first_year,
           max(case when rev_last_rn  = 1 and revenue > 0
                    then fiscal_year end)                             as rev_last_year,
           max(case when eb_first_rn = 1 and ebitda > 0
                    then ebitda end)                                  as ebitda_first,
           max(case when eb_last_rn  = 1 and ebitda > 0
                    then ebitda end)                                  as ebitda_last,
           max(case when eb_first_rn = 1 and ebitda > 0
                    then fiscal_year end)                             as eb_first_year,
           max(case when eb_last_rn  = 1 and ebitda > 0
                    then fiscal_year end)                             as eb_last_year
    from ranked
    group by accession, cik, scenario
)
select accession, cik, scenario, years, first_year, last_year,
       case when revenue_first > 0 and rev_last_year > rev_first_year
            then power(revenue_last / revenue_first,
                       1.0 / (rev_last_year - rev_first_year)) - 1
       end                                                            as revenue_cagr,
       case when ebitda_first > 0 and eb_last_year > eb_first_year
            then power(ebitda_last / ebitda_first,
                       1.0 / (eb_last_year - eb_first_year)) - 1
       end                                                            as ebitda_cagr
from bounds;
