-- Market Radar — 8-K deal extraction
--
-- Apply with:  uv run mr migrate
-- Idempotent, like 001-004.
--
-- Item 1.01 is "Entry into a Material Definitive Agreement" and covers every
-- material contract a registrant signs. Measured over 949 8-Ks filed
-- 2026-08-31 to 2026-09-04: Item 1.01 appears on 171 (18.0%), and about 16%
-- of those are actually M&A. The rest are credit facilities (31%), equity
-- raises (16%), commercial agreements, securitisations, and leases. So this
-- table is the *output of a classifier*, not a transcription of an item, and
-- it stores how confident that classifier was.
--
-- Two independent signals, kept apart on purpose:
--
--   exhibit_signal  an EX-2.x exhibit is attached. Reg S-K 601(b)(2) reserves
--                   exhibit 2 for a "plan of acquisition, reorganization,
--                   arrangement, liquidation or succession", so the filer has
--                   already done the classification. Primary.
--   text_signal     the agreement named in the prose. Secondary.
--
-- Over the measured week the two agreed on 20 filings, the exhibit fired
-- alone on 2, and the text fired alone on 9 -- of which three were private
-- placements that borrow the words "purchase agreement". Storing both and
-- their agreement makes the disagreements a queue to read rather than a
-- silent error, which is why there is no single boolean "is_ma" here.

create table if not exists deals (
    id              bigserial primary key,
    accession       text not null,
    cik             text not null,
    company         text,
    filed_date      date not null,
    event_date      date,
    items           text not null,          -- '1.01,9.01' as filed

    -- classification -------------------------------------------------
    exhibit_signal    boolean not null,
    text_signal       text,                 -- m_and_a | debt | equity_raise | ...
    classifiers_agree boolean not null,
    deal_type         text not null,

    -- consideration --------------------------------------------------
    consideration   text not null,          -- cash | stock | mixed | not_stated
    value_usd       numeric(20,2),
    value_basis     text not null,
    value_text      text,                   -- the phrase the figure came from

    -- parties --------------------------------------------------------
    filer_role      text not null,          -- acquirer | seller | party | not_stated
    counterparty    text,
    acquirer        text,
    target          text,
    party_basis     text not null,

    target_financials text not null,

    source          text not null,
    url             text,
    ingested_at     timestamptz not null default now(),

    constraint deals_accession_key unique (accession),

    -- The reason code is what makes a NULL value safe. Every missing figure
    -- carries a stated reason, so nothing can be read as zero, and "the
    -- filing did not say" is never conflated with "we failed to parse it".
    constraint deals_value_basis check (
        value_basis in ('stated_8k', 'stated_exhibit', 'not_stated', 'not_parsed')
    ),
    constraint deals_value_accounted check (
        (value_usd is not null) = (value_basis in ('stated_8k', 'stated_exhibit'))
    ),
    constraint deals_consideration check (
        consideration in ('cash', 'stock', 'mixed', 'not_stated')
    ),
    constraint deals_target_financials check (
        target_financials in
            ('rule_305_promised', 'figures_in_filing', 'none_disclosed')
    ),
    constraint deals_party_basis check (
        party_basis in ('derived_from_role', 'counterparty_only', 'not_parsed')
    ),
    constraint deals_deal_type check (
        deal_type in ('operating', 'spac', 'securitization', 'unclassified')
    )
);

create index if not exists deals_filed_idx on deals (filed_date desc);
create index if not exists deals_cik_idx on deals (cik, filed_date desc);
create index if not exists deals_review_idx on deals (filed_date desc)
    where not classifiers_agree;
create index if not exists deals_type_idx on deals (deal_type, filed_date desc);

comment on table deals is
    'Candidate M&A events extracted from 8-K Items 1.01 and 2.01. A row is a '
    'filing that fired at least one classifier, not a confirmed deal -- read '
    'classifiers_agree before treating one as fact.';

comment on column deals.value_usd is
    'Announced consideration in USD, or NULL. A NULL here is never zero and '
    'never unexplained: value_basis says whether the filing stated no figure '
    '(not_stated) or stated one we could not parse (not_parsed), and a check '
    'constraint makes the two columns impossible to disagree. Measured over '
    'the 2026-08-31 week, 68% of M&A Item 1.01 filings state a figure in the '
    'body and 77% state one somewhere including exhibits.';

comment on column deals.deal_type is
    'operating | spac | securitization | unclassified. SPAC business '
    'combinations are about a quarter of the M&A set and have no operating '
    'acquirer, no target financials, and no computable multiples. Pooling '
    'them with operating-company deals skews any outcome analysis, so they '
    'are separated at the table rather than filtered at each query.';

comment on column deals.target_financials is
    'rule_305_promised | figures_in_filing | none_disclosed. A public buyer '
    'taking a private target files audited target financials by amendment '
    'only when the target is significant under Rule 3-05, and the 8-K says so '
    'explicitly. Measured: only 9.7% of M&A Item 1.01 filings promise them, '
    'so the multiples this table could theoretically support usually cannot '
    'be computed. That is a fact about the disclosure regime, not a gap in '
    'the parser.';

comment on column deals.exhibit_signal is
    'An EX-2.x exhibit is attached. The primary classifier: Reg S-K 601(b)(2) '
    'reserves exhibit 2 for a plan of acquisition, so the filer has already '
    'classified the filing and we are reading their answer rather than '
    'guessing at prose.';

comment on column deals.classifiers_agree is
    'Whether exhibit_signal and text_signal both say M&A. False is the review '
    'queue, not an error -- both single-signal cases are real: agreements '
    'promised "by amendment" carry no exhibit yet, and private placements '
    'borrow the words "purchase agreement".';
