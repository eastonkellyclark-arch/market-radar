-- Market Radar — accession number as the natural key for filing signals
--
-- Apply with:  uv run mr migrate
-- Idempotent, like 001-003.
--
-- 001 keyed signals on (kind, source, company_id, occurred_at). For a filing
-- that is wrong, and wrong in the worst possible direction: two Form 4s from
-- two different insiders at the same company, filed the same moment, collide
-- and the second is rejected. Verified against the live table -- the first
-- insert succeeded and the second was refused by signals_natural_key.
--
-- Those filings are not a duplicate. They are a cluster, which is the single
-- signal Weekend 3 exists to detect. The old key would have silently deleted
-- half of every one.
--
-- The accession number is what actually identifies an SEC filing: assigned by
-- EDGAR, unique forever, and present on every document. An amendment (4/A)
-- carries its own accession, so superseding is a question for the form4
-- logic, not for this constraint.

alter table signals add column if not exists accession text;

comment on column signals.accession is
    'EDGAR accession number, e.g. 0001234567-26-000123. The natural key for '
    'anything that came from a filing. NULL for signals with no filing '
    'behind them -- vol_screen, news -- which keep the timestamp-based key.';

-- Replace the unconditional key with one that only applies where there is no
-- accession to key on. 001 recreates the original on every `mr migrate`, so
-- this drop runs after it each time and the partial version is what survives.
drop index if exists signals_natural_key;

create unique index if not exists signals_natural_key
    on signals (kind, source, coalesce(company_id, 0), occurred_at)
    where accession is null;

create unique index if not exists signals_accession_key
    on signals (kind, accession)
    where accession is not null;

-- Keyed on (kind, accession) rather than accession alone: one filing can
-- legitimately produce signals of different kinds. An 8-K that is both a
-- deal filing and a news event is two rows about one document, and neither
-- should evict the other.

create index if not exists signals_accession_idx
    on signals (accession)
    where accession is not null;
