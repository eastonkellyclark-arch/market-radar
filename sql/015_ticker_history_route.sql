-- The route that read a symbol, on the row.
--
-- **Fixing a defect, not adding a nicety.** `load` wrote a module constant for
-- every row, so the 138 symbols the prose route had recovered were all stored as
-- `dei:TradingSymbol`. The two are not interchangeable evidence -- the tag is
-- unambiguous and exists only after the 2019 cover-page mandate, the prose is a
-- regex measured at 7 of 8 over the decade before it -- and telling them apart is
-- the entire reason the route is computed. It was computed, carried through
-- `collapse`, and then dropped at the write.
--
-- Same rule as the provider and model on every LLM-produced row, and the resolved
-- tag on every fundamentals row: provenance that does not reach the row does not
-- exist. The table was rebuilt rather than backfilled, because which route found a
-- given row is not recoverable after the fact.
--
-- The default is removed rather than repointed. A default is what let a row inherit
-- a label it had not earned; with none, a writer that forgets the column gets a
-- not-null violation instead of a plausible wrong answer.

alter table company_ticker_history
    alter column source drop default;

-- An unrecognised route fails here rather than being stored. The Python side raises
-- too, in `source_of`, and both are deliberate: the check catches any future writer
-- that does not go through it.
alter table company_ticker_history
    drop constraint if exists cth_source;
alter table company_ticker_history
    add constraint cth_source check (source in (
        'dei:TradingSymbol',
        'cover-page prose',
        'dei:TradingSymbol and cover-page prose'
    ));

-- Recovery by route, which is the question the cliff makes worth asking: the tag
-- answers the years after the mandate and the prose answers the decade before it,
-- so a count that does not split them hides which half of the map is which.
create or replace view ticker_history_by_route as
select h.source,
       count(*)                                    as rows,
       count(distinct h.cik)                       as ciks,
       min(h.first_seen)                           as earliest,
       max(h.last_seen)                            as latest,
       count(*) filter (where h.filings = 1)       as single_filing_rows
from company_ticker_history h
group by h.source;
