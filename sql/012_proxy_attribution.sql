-- What the figure is *of*, and the reason code for getting that wrong.
--
-- `proxy_figure` verified that a number was in the document. It had no column for
-- the question the hand-check showed actually matters.
--
-- Measured 2026-09-11 over 20 real DEFM14A filings: **every wrong figure was
-- genuinely present in the text and quoted correctly.** Not one was invented.
--
--   Farmer Brothers   an exchange ratio of 1.0, quoting "each share of common
--                     stock of *Merger Sub* ... shall automatically be
--                     converted" -- boilerplate merger mechanics read as what
--                     target holders receive. The deal is all cash and has no
--                     ratio at all.
--   Royal Gold        a $2.00 price, quoting "C$2.00 in cash per common share" --
--                     a *different* deal inside the same document, in Canadian
--                     dollars, in a filing where Royal Gold is the buyer.
--   Comerica          a 7% premium, quoting "premium of 7.0% and 75th percentile
--                     premium of 22" -- a quartile from a table of premiums paid
--                     in other transactions.
--
-- A citation proves the number was read rather than invented. It says nothing
-- about what the number is of, and that is the error that survives it. So three
-- columns and one reason code:
--
--   attributed_to    the entity the model says receives the figure, in the
--                    document's own words
--   attribution_ok   whether that agrees with the filer. NULL is "not checked",
--                    which is a different fact from "checked and fine" -- the
--                    same distinction as `absent` against `unmapped`.
--   reference        what a premium is measured against: a closing price, a
--                    20-day VWAP, an unaffected price. Stored rather than checked
--                    because there is nothing to check it against, and it is what
--                    makes two premiums comparable instead of two numbers. One
--                    filing quotes 208.5%, 231% and 84.9% for the same deal.
--
--   misattributed    present, correctly quoted, and of something else. Kept
--                    distinct from `uncited` because the two say opposite things
--                    about the provider: an uncited figure means the model
--                    produced text that is not in the document, a misattributed
--                    one means it read the document correctly and answered a
--                    different question. The remedy is a prompt in one case and a
--                    locator in the other.
--
-- `not_a_takeout` is added in the same statement because it was missing: the
-- population gate predates this table's constraint and a gated document's row
-- could not be written at all.

alter table proxy_figure add column if not exists attributed_to  text;
alter table proxy_figure add column if not exists attribution_ok boolean;
alter table proxy_figure add column if not exists reference      text;

alter table proxy_figure drop constraint if exists proxy_figure_reason;
alter table proxy_figure add constraint proxy_figure_reason check (
    reason in ('stated', 'not_stated', 'not_parsed', 'no_section', 'uncited',
               'not_a_takeout', 'misattributed')
);

-- A `stated` row now needs its attribution as well as its quote. Unchecked
-- (NULL) is permitted -- there is no filer name on every path -- but a row that
-- was checked and failed cannot also be `stated`, which is what makes the
-- rejection structural rather than a convention in the writer.
alter table proxy_figure drop constraint if exists proxy_figure_attribution;
alter table proxy_figure add constraint proxy_figure_attribution check (
    reason <> 'stated' or attribution_ok is not false
);

create index if not exists proxy_figure_attribution_idx
    on proxy_figure (field, attribution_ok);
