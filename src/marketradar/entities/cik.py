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


def cik_sql(column: str) -> str:
    """The SQL form of `cik_key`, for one column reference.

    Takes the column expression rather than a table alias so it reads as what it is
    at the join site: ``{cik_sql('a.cik')} = {cik_sql('b.cik')}``. Casting first
    matters -- a CIK arrives as both integer and varchar depending on the store, and
    ``lpad`` on an integer is an implicit cast whose result depends on the engine.
    """
    return f"lpad(cast({column} as varchar), {WIDTH}, '0')"
