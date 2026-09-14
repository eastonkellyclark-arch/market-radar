"""The one representation of a CIK, for Python and for SQL.

**Four failures, so this is structural rather than a rule.** A CIK is the right key
-- SEC assigns it and never reuses it -- and it has two spellings in this codebase:
the XBRL partitions carry it unpadded (``7332``) and Postgres carries it zero-padded
to ten (``0000007332``). Those strings do not compare, and every time they have
failed to, the result was a believable number rather than an error:

- the DCF was handed **5,499 real betas and matched none of 6,431 filers**. Every
  row read ``no_beta``, which is a perfectly plausible answer because half the
  universe genuinely has no usable ticker.
- the same mismatch returned **zero** peer betas from a working peer-set query
- and joined ``shares`` to ``companies`` for **0 of 3,443** filers
- a measurement script printed **"0 of 3,744 stopped filers carry a recovered
  ticker"** -- a finding, not an error, and a believable one about a map that had
  just been built

A convention cannot be enforced by reading, so it is not a rule, it is a hope. Both
forms live here and `tests/test_repo_invariants.py` fails the build on a join that
goes around them:

- `cik_key` for Python comparisons and dict keys
- `cik_sql` for every SQL join, on **both** sides, without exception
- `cik_bare` for the two places the *unpadded* form is what is wanted

**Sixth and seventh, 2026-09-13, and together they say the rule was not finished.**
The sixth was `_deck_subject` reading `{cik_sql('cik')} = ?` -- the column through
the helper and the bind parameter raw -- which the join rule cannot see, because
there is no join and the column *is* wrapped. Four of every deck's ten pages came
out blank and one printed a capex caveat about a filer reporting $910M of capex.

The seventh was found by asking how the sixth was possible, and it is the larger
one: **the helper was not the only way to produce a CIK, in fifteen places.**
`sources/sec_trading_symbols.py` held `str(cik).strip().lstrip("0").rjust(10, "0")`
-- `cik_key`'s body, reimplemented -- and `signals/deals.py` produced *two
different* spellings four lines apart, `str(cik).lstrip("0").zfill(10)` for one
field and `str(cik).lstrip("0")` for another. Five spellings of the normaliser
across four modules, none of them wrong today and none of them a rule.

So the padded form is not the only canonical one, and pretending it was is what
left the others hand-rolled. There are exactly two, and `cik_bare` is the second:

*Padded (`cik_key`).* `0000066740`. Postgres `companies.cik`, every Python dict
key and set member, every comparison.

*Bare (`cik_bare`).* `66740`. Two uses, both forced by something outside this
codebase. EDGAR's archive paths are unpadded -- `/edgar/data/66740/<accession>/`
returns the filing and `/edgar/data/0000066740/` does not -- and the `deals.cik`
column was loaded unpadded, which `cik_sql` normalises at every join rather than
requiring a migration.

Either is fine to *store*, because `cik_sql` wraps both sides of every join. What
is not fine is a third spelling nobody named.

The no-exceptions part is deliberate and it is what makes the invariant readable.
``lpad`` is idempotent, so wrapping an already-padded column costs a function call
and nothing else -- whereas "wrap it only where the two sides might differ" requires
knowing which sides those are, which is exactly the judgement that has been wrong
four times. A rule with a carve-out for locally-consistent tables is a rule whose
carve-out gets copied into the next cross-store join.
"""

from __future__ import annotations

import re

from typing import Any, Final

#: SEC zero-pads CIKs to ten characters, which is the form Postgres holds here.
WIDTH: Final[int] = 10


def cik_key(cik: Any) -> str:
    """A CIK in one representation, so two sources can be compared on it.

    Strips any existing padding before re-padding, so an unpadded ``7332`` and a
    padded ``0000007332`` both arrive as the same string. An empty or whitespace
    input returns ``""`` rather than ten zeros -- a missing CIK must not become a
    key that something else could collide with.
    """
    digits = re.sub(r"\D", "", str(cik))
    if not digits:
        return ""
    return digits.lstrip("0").rjust(WIDTH, "0")


def cik_bare(cik: Any) -> str:
    """A CIK with leading zeros removed, for the two places that need it.

    EDGAR's archive URLs and the `deals.cik` column; see the module docstring for
    why each is unpadded. Returns ``"0"`` for a CIK that is all zeros and ``""``
    for one that carries no digits at all -- the same refusal as :func:`cik_key`,
    because an empty identifier that becomes a real-looking one is how a row ends
    up joined to the wrong company.

    **Not a convenience wrapper.** It exists so that "the unpadded form" is a
    function with a name rather than five inline spellings of `lstrip("0")`, one
    of which was reimplementing `cik_key` and another of which quietly turned an
    empty string into the CIK ``"0"``.
    """
    digits = re.sub(r"\D", "", str(cik))
    if not digits:
        return ""
    return digits.lstrip("0") or "0"


def cik_sql(column: str) -> str:
    """The SQL form of `cik_key`, for one column reference.

    Takes the column expression rather than a table alias so it reads as what it is
    at the join site: ``{cik_sql('a.cik')} = {cik_sql('b.cik')}``. Casting first
    matters -- a CIK arrives as both integer and varchar depending on the store, and
    ``lpad`` on an integer is an implicit cast whose result depends on the engine.
    """
    return f"lpad(cast({column} as varchar), {WIDTH}, '0')"
