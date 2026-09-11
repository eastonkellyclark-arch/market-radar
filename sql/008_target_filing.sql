-- Target-side merger filings: the disclosures the company being bought makes.
--
-- `deals` reads 8-K Items 1.01 and 2.01 and nothing else, which is the
-- acquirer-and-divestor side of the market. CLAUDE.md's stated M&A set has seven
-- form types and six of them were never swept, so "we cannot see the target
-- side" has been half a data limitation and half a missing sweep.
--
-- Measured 2026-09-10 over 120 sampled filers in the deal-multiples population:
--
--   DEFM14A    27%   the merger proxy -- banker's fairness opinion, comps,
--                    precedent transactions, DCF, premiums paid, and management
--                    projections. Filed by the target.
--   S-4 / S-4A 33%   stock-merger registration, filed by the acquirer, carrying
--                    the target's financial statements
--   PREM14A    18%   the preliminary version of the same proxy
--   SC TO-T    10%   third-party tender offer
--   SC TO-I     8%   issuer tender offer
--   SC 13E3     4%   going-private
--   64% have at least one of them.
--
-- **DEF 14A and DEFA14A are deliberately not here.** They appear on 96% and 93%
-- of filers because they are the routine annual-meeting proxy and its
-- supplements. Sweeping them would multiply the population by twenty and add no
-- merger disclosure, which is the trap in reading a form-frequency table.
--
-- This holds *discovery* only: which target-side filings exist, keyed on the
-- accession SEC assigns. It costs no extra requests -- the daily index already
-- lists every form -- and it is deliberately separate from extraction. A DEFM14A
-- is 1.26 million characters of text whose valuable content is in HTML tables
-- formatted per investment bank; pulling numbers out of it is a located-section
-- plus cheap-LLM job, not a regex, and it is a separate table when it exists.

create table if not exists target_filing (
    id              bigserial primary key,
    accession       text not null,
    cik             text not null,          -- the filer, which for a proxy is
                                            -- the company being bought
    company         text,
    form            text not null,          -- DEFM14A | PREM14A | S-4 | ...
    filed_date      date not null,

    -- Where the document is, so extraction needs no re-discovery.
    url             text,

    source          text not null default 'edgar_daily_index',
    ingested_at     timestamptz not null default now(),

    -- Accession, per the identifier rule: SEC assigns it and never reuses it.
    -- Keying on (cik, form, filed_date) would collapse an amended proxy filed
    -- the same day as its original, which is two documents.
    constraint target_filing_accession_key unique (accession)
);

-- The join deal_multiples needs: given a target CIK, what did it disclose.
create index if not exists target_filing_cik_idx
    on target_filing (cik, filed_date desc);

-- And the reverse: all proxies of one form type in a window, for coverage
-- reporting.
create index if not exists target_filing_form_idx
    on target_filing (form, filed_date desc);
