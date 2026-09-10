-- Market Radar — the entity review queue
--
-- Apply with:  uv run mr migrate
-- Idempotent, like 001-006.
--
-- **This queue is ~23,000 rows, not 800,000, and that is the whole point.**
--
-- The original plan was to fuzzy match ~800k Form 5500 sponsor names into a
-- permanent review queue. Measured 2026-09-10, that is the wrong shape:
--
--   * EIN is present on 100% of 1,023,597 filings, so a sponsor that is also
--     an SEC filer resolves exactly, with no matching and nothing to review.
--   * 812,933 sponsors (94.7%) match nothing, and that is the source working
--     rather than failing -- they are private companies, which is the entire
--     reason to read Form 5500. There is nothing to resolve them *against*.
--   * What is genuinely ambiguous is the residue: a sponsor whose *name*
--     matches an SEC filer while its EIN does not. 23,406 of those, and each
--     one is either a subsidiary, a renamed entity, or a coincidence.
--
-- Normalized-name matching scores 44.2% precision against EIN ground truth --
-- wrong more often than right. So nothing here is auto-merged at a
-- similarity threshold; a row is a *question*, and resolution requires a
-- human-confirmable record. See the identifier rule in CLAUDE.md.

create table if not exists entity_review (
    id           bigserial primary key,
    ein          text    not null,
    plan_year    integer not null,
    sponsor_name text    not null,
    -- What the name matched, and how. Never used as a join: recorded so a
    -- human can see the claim being made.
    matched_cik  text,
    matched_name text,
    match_basis  text    not null,      -- 'exact_name' | 'normalized_name'
    -- How many SEC filers the name matched. Above one, the name is not
    -- discriminating: 11 filers share the normalized key 'energy'.
    candidates   integer not null default 1,
    naics        text,
    state        text,
    participants integer,
    status       text    not null default 'pending',
    decided_at   timestamptz,
    note         text,
    ingested_at  timestamptz not null default now(),

    constraint entity_review_key unique (ein, plan_year, match_basis),
    constraint entity_review_status check (
        status in ('pending', 'confirmed', 'rejected')
    ),
    constraint entity_review_basis check (
        match_basis in ('exact_name', 'normalized_name')
    ),
    -- A decision has a timestamp; a pending row does not pretend to.
    constraint entity_review_decided check (
        (status = 'pending') = (decided_at is null)
    )
);

create index if not exists entity_review_pending_idx
    on entity_review (plan_year desc, candidates, ein)
    where status = 'pending';

create index if not exists entity_review_ein_idx on entity_review (ein);

comment on table entity_review is
    'Form 5500 sponsors whose name matched an SEC filer while their EIN did '
    'not. ~23,000 rows for plan year 2024. Not the private population: those '
    '812,933 sponsors match nothing by design and need no review.';

comment on column entity_review.match_basis is
    'exact_name | normalized_name. Recorded so the strength of the claim is '
    'visible -- normalized-name matching has 44.2% precision against EIN, so '
    'a normalized_name row is closer to a coin flip than to a finding.';

comment on column entity_review.candidates is
    'SEC filers the name matched. Above one the name is not discriminating '
    'at all, so these sort last: 11 filers share the normalized key '
    '''energy'' and 9 share ''capital''.';

comment on column entity_review.status is
    'pending | confirmed | rejected. There is deliberately no auto-confirm '
    'threshold. A matcher that is wrong more than half the time is worse '
    'than no matcher, because the errors are invisible.';
