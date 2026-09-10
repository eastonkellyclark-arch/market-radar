"""The funnel: what each stage of a screen removed, and the two checks on it.

This module is the generalisation of five defects found on 2026-09-10, and
the tests here are about the one property that matters: a screen's output is
a short list, and a short list looks identical whether the filters are
working or one of them has silently emptied the population.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from marketradar.screens import funnel as fn

SRC = Path(__file__).resolve().parents[1] / "src" / "marketradar"


def test_a_stage_reports_what_it_removed_not_only_what_survived() -> None:
    f = fn.build("t", ("all", 1000), ("filtered", 400), ("ranked", 20))
    assert f.start == 1000 and f.end == 20
    assert f.removed(1) == 600
    assert f.share_removed(1) == pytest.approx(0.6)
    assert f.removed(2) == 380


def test_a_stage_that_empties_the_population_is_named() -> None:
    """The failure this exists for. A filter meant to remove a few thousand
    union trusts removed 800,287 sponsors, and the surviving list looked
    *more* plausible than the correct one -- old colleges and firemen's
    relief funds, nothing visibly wrong."""
    f = fn.build("t", ("all", 900_000), ("an employer", 0), ("ranked", 0))
    assert f.emptied is not None
    assert f.emptied.name == "an employer"
    assert "it is gone" in "\n".join(f.lines())


def test_an_empty_input_is_not_reported_as_an_emptied_stage() -> None:
    """Nothing in, nothing out is not a filter failing."""
    f = fn.build("t", ("all", 0), ("filtered", 0))
    assert f.emptied is None


def test_a_stage_that_removes_nearly_everything_is_marked_not_failed() -> None:
    """Several honest stages do this. It is a prompt to look, not an error --
    the screen still returns its list."""
    f = fn.build("t", ("all", 1_000_000), ("private", 50_000))
    assert [s.name for s in f.collapsed] == ["private"]
    assert "check it" in "\n".join(f.lines())
    f2 = fn.build("t", ("all", 1000), ("most", 800))
    assert f2.collapsed == []


def test_the_reason_for_a_stage_is_printed_beside_its_count() -> None:
    """A number with no reason is the number nobody checks."""
    f = fn.build("t", ("all", 10), ("gated", 5, "the $5M dollar-volume gate"))
    assert "the $5M dollar-volume gate" in "\n".join(f.lines())


def test_the_funnel_survives_a_single_stage() -> None:
    f = fn.build("t", ("all", 10))
    assert f.start == f.end == 10
    assert f.emptied is None and f.collapsed == []
    assert list(f.lines())


def test_a_stage_that_grows_the_population_reports_zero_removed() -> None:
    """A later stage larger than an earlier one is a bug in the caller, but
    it must not produce a negative count that reads as a filter adding rows."""
    f = fn.build("t", ("all", 10), ("more", 40))
    assert f.removed(1) == 0
    assert f.share_removed(1) == 0.0


# --- the invariant ------------------------------------------------------


def _screen_modules() -> list[Path]:
    """Screens, and the cluster rule, which is a screen living elsewhere."""
    out = [p for p in (SRC / "screens").glob("*.py")
           if p.name not in ("__init__.py", "funnel.py")]
    out.append(SRC / "signals" / "form4.py")
    return sorted(p for p in out if p.exists())


def test_every_screen_reports_a_funnel() -> None:
    """The parallel to ``assert_fresh`` on every loader, and for the same
    reason one step later: a loader must not exit green on empty data, and a
    screen must not report a plausible list without saying what it discarded
    to get there.

    Parsed from the AST rather than grepped, so a mention in a docstring does
    not count as adoption.
    """
    delinquent = []
    for module in _screen_modules():
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        builds = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "build"
            and isinstance(node.func.value, ast.Name)
            and "funnel" in node.func.value.id
            for node in ast.walk(tree)
        )
        if not builds:
            delinquent.append(module.name)

    assert not delinquent, (
        f"These screens report no funnel: {', '.join(delinquent)}. A short "
        "list is either selective or broken, and the stage counts are what "
        "tells you which -- see marketradar.screens.funnel. Four of the five "
        "Form 5500 defects were 'the list contained something other than "
        "what the screen claimed to measure', and every one survived a green "
        "suite."
    )
