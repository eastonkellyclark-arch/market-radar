"""What each stage of a screen removed, in the order it removed it.

**A short list is either selective or broken, and the counts are the only
thing that tells you which.** This module exists because of five defects
found in the Form 5500 work on 2026-09-10, four of which were the same
mistake -- the list contained something other than what the screen claimed to
measure -- and every one of which survived a green test suite. The one that
was caught cheaply was caught by a funnel line: a filter meant to remove a
few thousand union trusts removed 800,287 sponsors, and *the surviving list
looked more plausible than the correct one*. Old colleges, firemen's relief
funds, nothing visibly wrong. Only the stage count said otherwise.

That is the general shape of the risk. A screen's output is a small list, and
a small list looks the same whether the filters are working or one of them is
silently emptying the population. Tests do not catch it, because a test
encodes what the author believed about the data and this class of defect *is*
a gap in that belief. What catches it is printing how many rows survived each
stage, every run, next to the result.

So: every screen ends with a funnel, the same way every loader ends with a
freshness assertion. The parallel is exact. ``assert_fresh`` refuses to let a
job exit green on empty data; this refuses to let a screen report a plausible
list without saying what it discarded to get there.

Two things are checked rather than merely printed, because they are the two
that are always wrong:

``emptied``
    A stage that took a non-zero population to zero. The screen has no output
    and the reason is one named filter, which is worth saying out loud rather
    than leaving a reader to infer from a blank list.
``collapsed``
    A stage that removed more than :data:`COLLAPSE_SHARE` of what reached it.
    Not an error -- several honest stages do this, and the Form 5500 screen's
    own trend filter is one -- but it is where a broken filter hides, so it
    is marked and the reader decides.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final, Iterator

#: A stage removing more than this share of its input is marked for reading.
#: Not a threshold for failing: it is a prompt to check, set where a filter
#: stops being a filter and starts being the whole answer.
COLLAPSE_SHARE: Final[float] = 0.90


@dataclass(frozen=True, slots=True)
class Stage:
    """One filter, and how many rows were still standing after it."""

    name: str
    remaining: int
    #: Why this stage exists, in a few words. Printed beside the count,
    #: because a number with no reason is the thing nobody checks.
    why: str = ""


@dataclass(frozen=True, slots=True)
class Funnel:
    """An ordered population count, from everything to what was reported."""

    screen: str
    stages: list[Stage] = field(default_factory=list)

    @property
    def start(self) -> int:
        return self.stages[0].remaining if self.stages else 0

    @property
    def end(self) -> int:
        return self.stages[-1].remaining if self.stages else 0

    def removed(self, i: int) -> int:
        """How many rows stage ``i`` took out."""
        if i == 0:
            return 0
        return max(0, self.stages[i - 1].remaining - self.stages[i].remaining)

    def share_removed(self, i: int) -> float:
        prior = self.stages[i - 1].remaining if i else 0
        return self.removed(i) / prior if prior else 0.0

    @property
    def emptied(self) -> Stage | None:
        """The stage that took a non-empty population to nothing."""
        for i, stage in enumerate(self.stages):
            if i and stage.remaining == 0 and self.stages[i - 1].remaining:
                return stage
        return None

    @property
    def collapsed(self) -> list[Stage]:
        """Stages that removed nearly everything reaching them."""
        return [s for i, s in enumerate(self.stages)
                if i and self.share_removed(i) > COLLAPSE_SHARE]

    def lines(self) -> Iterator[str]:
        """The funnel as text, one stage per line. For the CLI and the log."""
        yield f"{self.screen}: population by stage"
        width = max((len(s.name) for s in self.stages), default=0)
        for i, stage in enumerate(self.stages):
            bit = f"  {stage.name:<{width}}  {stage.remaining:>10,}"
            if i:
                bit += f"  -{self.removed(i):>10,}"
                bit += f"  {self.share_removed(i):>5.1%}"
                if self.share_removed(i) > COLLAPSE_SHARE:
                    bit += "  <-- removed nearly everything; check it"
            if stage.why:
                bit += f"   {stage.why}"
            yield bit
        if self.emptied:
            yield (f"  EMPTY: '{self.emptied.name}' removed every remaining "
                   "row. The list is not short, it is gone.")

    def as_dict(self) -> dict[str, Any]:
        """For the dashboard, which renders its own layout."""
        return {
            "screen": self.screen,
            "stages": [
                {"name": s.name, "remaining": s.remaining, "why": s.why,
                 "removed": self.removed(i),
                 "share": self.share_removed(i),
                 "collapsed": bool(i) and self.share_removed(i) > COLLAPSE_SHARE}
                for i, s in enumerate(self.stages)
            ],
            "emptied": self.emptied.name if self.emptied else None,
        }


def build(screen: str, *stages: tuple[str, int] | tuple[str, int, str]) -> Funnel:
    """``build("volatility", ("bars", 20_000), ("moves", 8_000, "why"))``."""
    return Funnel(screen=screen, stages=[
        Stage(s[0], int(s[1]), s[2] if len(s) > 2 else "") for s in stages
    ])
