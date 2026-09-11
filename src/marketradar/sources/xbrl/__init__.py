"""XBRL fundamentals from the SEC Financial Statement Data Sets.

Three parts, split because they are edited by different people at different
times: :mod:`tag_map` is the hand-maintained map and is the file a human opens
when coverage drifts, :mod:`download` gets the quarterly zips, and
:mod:`resolve` turns one quarter into rows and a coverage report.

The modules are ``download`` and ``resolve`` rather than ``fetch`` and
``load``, for one small reason that cost real time twice: this package exports
``fetch`` and ``load`` as *functions*, per the one-module-per-source
convention, and a submodule of the same name shadows the function it sits
next to -- so ``from marketradar.sources.xbrl import fetch`` hands back
whichever of the two was bound last, with an AttributeError several frames
later. Different things get different names.
"""

from marketradar.sources.xbrl.download import fetch
from marketradar.sources.xbrl.resolve import build, load
from marketradar.sources.xbrl.tag_map import (
    CONCEPTS,
    STATUSES,
    classify_sic,
    era_for,
)

__all__ = [
    "CONCEPTS",
    "STATUSES",
    "build",
    "classify_sic",
    "era_for",
    "fetch",
    "load",
]
