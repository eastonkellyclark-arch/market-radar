-- Figures read out of merger proxies, with the provenance to audit them.
--
-- One row per (accession, field), long rather than wide, for the same reason the
-- XBRL fundamentals table is: a field that could not be read is a row carrying a
-- reason, not a NULL in a wide row that nobody can interpret.
--
-- **Every row says where its number came from.** An LLM number with no
-- provenance is unauditable, and this table will be read a year from now by
-- someone deciding whether to trust it. So: the accession, the section heading
-- that was located, the character offsets of the window that was sent, the
-- verbatim quote the figure was taken from, and the provider, model and prompt
-- version that read it. A figure from `gpt-oss-120b` under prompt v1 and the same
-- figure from a 3B local model under v3 are different measurements.
--
-- **The quote is verified before the row is written.** The model is required to
-- return the exact text it took the number from; if that text is not in the
-- window it was given, the figure is rejected as `uncited` and the value is not
-- stored. That is the cheap mechanical check on the one failure mode this tier
-- has, and its rate is how trust in a provider is earned.
--
-- Reason codes keep the same discipline as `deals`:
--
--   stated       found, and the quote checks out
--   not_stated   the section exists and genuinely lacks the figure. A
--                stock-for-stock merger has no cash price per share. **Not a
--                failure** -- conflating it with one sends the next reader
--                hunting for a number nobody wrote down.
--   not_parsed   the section exists, should have it, and we could not get it
--   no_section   the heading was never found; the remedy is a locator, not a
--                prompt
--   uncited      a figure came back whose quote is not in the source text

create table if not exists proxy_figure (
    id              bigserial primary key,

    -- what was read -------------------------------------------------------
    accession       text not null,
    cik             text not null,
    form            text not null,
    field           text not null,          -- consideration_per_share | ...
    reason          text not null,
    value           numeric(28,6),
    unit            text,                   -- usd_per_share | percent | ...

    -- provenance ----------------------------------------------------------
    quote           text,                   -- verbatim, and checked
    section         text,                   -- merger_consideration | ...
    section_heading text,                   -- the text the locator matched
    section_start   integer,                -- char offsets into the document
    section_end     integer,
    document        text,                   -- the filename actually read
    provider        text,                   -- groq | cerebras | ollama
    model           text,
    prompt_version  text not null,
    note            text,                   -- the model's own reason, when absent

    extracted_at    timestamptz not null default now(),

    -- One figure per field per filing per prompt version. Including the prompt
    -- version in the key is deliberate: re-reading under a new prompt is a new
    -- measurement and must not overwrite the old one, because comparing them is
    -- how a prompt change is evaluated.
    constraint proxy_figure_key unique (accession, field, prompt_version),

    constraint proxy_figure_reason check (
        reason in ('stated', 'not_stated', 'not_parsed', 'no_section', 'uncited')
    ),
    -- A value may only exist on a row that claims to have found one, and a
    -- `stated` row must carry its citation. The table refuses an unauditable
    -- number rather than trusting the writer to be careful.
    constraint proxy_figure_value_needs_reason check (
        (reason = 'stated' and value is not null and quote is not null)
        or (reason <> 'stated' and value is null)
    )
);

create index if not exists proxy_figure_field_idx
    on proxy_figure (field, reason);

create index if not exists proxy_figure_cik_idx
    on proxy_figure (cik, accession);
