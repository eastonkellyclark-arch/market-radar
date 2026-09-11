-- `division_sale` as a deal type.
--
-- Dividing a business unit's price by its parent's whole revenue is a category
-- error -- part of a company over all of it -- and it fails in the direction
-- that hides: the multiple comes out small, and a screen ranking cheap deals
-- first ranks its own mistakes first.
--
-- Measured 2026-09-10: of 2,112 priced filings with a pre-deal annual report,
-- 1,778 (84%) filed another 10-K afterwards, so the thing sold was not the
-- filer.
--
-- It is a *type* and not a flag because every consumer has to exclude it, and a
-- flag is the thing that gets forgotten. The check constraint is what made that
-- true rather than aspirational: the first sweep to classify one was refused by
-- the database, which is the constraint doing its job.

alter table deals drop constraint if exists deals_deal_type;

alter table deals add constraint deals_deal_type check (
    deal_type in ('operating', 'division_sale', 'spac', 'securitization',
                  'unclassified')
);
