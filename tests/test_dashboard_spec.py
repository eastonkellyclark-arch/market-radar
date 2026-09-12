"""A panel's state, checked against the two things that can contradict it.

**Why this file exists.** Two panels carried a state that was wrong on
2026-09-10, and they were wrong in two different directions:

  news    declared ``weekend="Weekend 3"`` and chipped *not built*, a day
          after news had been measured and declined. The spec table in
          docs/build-spec.md said ``declined``. Doc right, code wrong.
  ticker  declared ``waiting_on="10-year backfill"`` and resolved *not
          built*, while detail.py drew a working chart that jsdom was
          clicking through. The spec table said ``not built`` too. Doc and
          code agreed with each other and both disagreed with the page.

So one check is not enough, and neither of these is caught by a test that
reads a string and asserts the same string back:

:func:`test_the_spec_table_and_the_panel_map_agree` parses the table and
compares it to the resolved panels. That catches `news` -- the case where the
doc was updated and the code was not, which is the usual direction.

:func:`test_a_panel_off_the_roadmap_renders_nothing` compares a panel's state
to whether it draws anything. That catches `ticker` -- the case where doc and
code agree and reality has moved past both. It is the inverse of
``test_a_live_panel_always_has_a_body`` in test_dashboard_shell.py, and it
exists for the same reason: the state and the body have independent sources,
so the only way to trust either is to make them answer for each other.

**The table is not the source of truth**, and this file is careful not to make
it one. ``shell.PANELS`` and the probes are; the shell renders from them and
must never read markdown to do it. The table is a doc that describes them, and
a doc that nothing compares to the code is a doc that is eventually wrong.

The join is on the panel **id**, never on the title. Same rule as everywhere
else here: a name-based join does not error, it silently matches the wrong row
-- and a markdown table hand-edited over months is exactly the string humans
typed.
"""

from __future__ import annotations

import re
from pathlib import Path

from conftest import (MATURE_ROW, MATURE_STATS, PRIVATE_ROW, PRIVATE_STATS,
                      loaded_context, panel_slot)
from marketradar.dashboard import shell

SPEC = Path(__file__).resolve().parent.parent / "docs" / "build-spec.md"

#: The table header, verbatim. Matched rather than searched for loosely, so a
#: renamed column fails here with a clear message instead of silently parsing
#: zero rows -- a table this test could not find would make every assertion
#: below vacuously true, which is the shape of the bug it is checking for.
HEADER = "| section | panel | id | state | notes |"

def resolved() -> dict[str, tuple[str, str]]:
    """``{panel id: (state, detail)}`` on a fully loaded system."""
    ctx = loaded_context()
    return {p.id: p.resolve(ctx) for p in shell.PANELS}


# --- the spec table -----------------------------------------------------


def spec_rows() -> dict[str, dict[str, str]]:
    """The panel table from docs/build-spec.md, keyed on panel id."""
    text = SPEC.read_text(encoding="utf-8")
    assert HEADER in text, (
        f"the panel table header is no longer {HEADER!r} in {SPEC.name}. "
        "This test parses that table; a renamed column has to be renamed here "
        "too, or the check silently stops checking."
    )
    body = text.split(HEADER, 1)[1].splitlines()
    rows: dict[str, dict[str, str]] = {}
    for line in body:
        line = line.strip()
        if not line:
            continue
        if not line.startswith("|"):
            break
        if set(line) <= set("|- "):   # the |---|---| alignment rule
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        assert len(cells) == 5, f"malformed table row: {line}"
        section, title, pid, state, _notes = cells
        pid = pid.strip("`")
        assert pid not in rows, f"{pid} is in the table twice"
        rows[pid] = {"section": section, "title": title, "state": state}
    assert rows, "parsed no rows out of the panel table"
    return rows


def test_the_spec_table_lists_every_panel_and_no_others() -> None:
    """The doc is a map of the build order, so a panel missing from it is a
    panel the roadmap does not mention, and a row with no panel is a promise
    with nothing behind it."""
    table, panels = spec_rows(), {p.id for p in shell.PANELS}
    assert set(table) - panels == set(), (
        "docs/build-spec.md lists panels that do not exist: "
        f"{sorted(set(table) - panels)}"
    )
    assert panels - set(table) == set(), (
        "these panels are not in the build-spec table: "
        f"{sorted(panels - set(table))}"
    )


def test_the_spec_table_and_the_panel_map_agree() -> None:
    """The check that would have caught `news`.

    It said "planned for Weekend 3" in the code and ``declined`` in the doc,
    for a day, because the decision was written down in one place and the
    shell went on advertising a weekend that was never coming.

    Compared on a fully loaded context, because that is what the table says it
    describes. Title and section too: the sidebar groups by section, so a
    panel in the wrong one is in the wrong place on the page as well as in the
    doc.
    """
    table, states = spec_rows(), resolved()
    drift = []
    for panel in shell.PANELS:
        row, (state, _detail) = table[panel.id], states[panel.id]
        # The doc uses an em dash where the code is ASCII-only -- the title
        # reaches a cp1252 Windows console. Normalised, not asserted away.
        title = panel.title.replace(" -- ", " — ")
        if row["state"] != state:
            drift.append(
                f"{panel.id}: the table says {row['state']!r}, the panel "
                f"resolves {state!r}"
            )
        if row["section"] != panel.section:
            drift.append(
                f"{panel.id}: the table files it under {row['section']!r}, "
                f"the panel says {panel.section!r}"
            )
        if row["title"] != title:
            drift.append(
                f"{panel.id}: the table calls it {row['title']!r}, "
                f"the panel {title!r}"
            )
    assert not drift, (
        "docs/build-spec.md and shell.PANELS disagree:\n  "
        + "\n  ".join(drift)
        + "\n\nThe code is the source of truth -- the shell renders from "
        "PANELS and the probes, and never reads the doc. So fix whichever is "
        "wrong, but both have to say it."
    )


def test_every_state_in_the_table_is_one_the_shell_has() -> None:
    """A state the doc invented would otherwise read as drift in every panel
    carrying it, which buries the actual mistake."""
    for pid, row in spec_rows().items():
        assert row["state"] in shell.STATES, (
            f"{pid} is documented as {row['state']!r}, which is not one of "
            f"{shell.STATES}"
        )


# --- state against what the panel actually draws ------------------------


def test_a_panel_off_the_roadmap_renders_nothing() -> None:
    """The check that would have caught `ticker`.

    It resolved *not built* and said "Weekend 2.5 (U3)" while ``render``
    handed it ``detail.panel_html()`` and the page drew a chart into it. The
    doc agreed with the code, so comparing those two would not have found it
    -- only comparing the state to what the panel puts on screen does.

    Restricted to the two roadmap states on purpose. A *waiting* panel does
    render a body: every renderer owns its empty case and names the command
    that fills it, which is a different and deliberate thing.
    """
    ctx = loaded_context()
    page = shell.render(
        ctx,
        details={"AAA": {"s": [[0, 1.0]], "r": [], "a": [], "g": []}},
        private=[PRIVATE_ROW], private_stats=PRIVATE_STATS,
        mature=[MATURE_ROW], mature_stats=MATURE_STATS,
    )
    states = {p.id: p.resolve(ctx)[0] for p in shell.PANELS}
    offenders = []
    for panel in shell.PANELS:
        if states[panel.id] not in (shell.NOT_BUILT, shell.DECLINED):
            continue
        if panel_slot(page, panel.id).strip():
            offenders.append(f"{panel.id}: chipped {states[panel.id]!r} "
                             "while rendering a body")
    assert not offenders, (
        "Panels claiming to be off the roadmap while drawing something:\n  "
        + "\n  ".join(offenders)
        + "\n\nEither it is built and the probe has not noticed, or it is not "
        "and something is rendering into it anyway."
    )


def test_the_ticker_panel_is_live_and_draws(tmp_path) -> None:
    """The specific case, pinned.

    Three answers in three weeks -- waiting on the backfill, then not built
    pending U3, then live -- is the argument for the general checks above. This
    one keeps the third from quietly becoming a fourth.
    """
    ctx = loaded_context()
    state, detail = shell._probe_ticker_detail(ctx)
    assert state == shell.LIVE, detail
    assert "11 years" in detail
    page = shell.render(
        ctx, details={"AAA": {"s": [[0, 1.0]], "r": [], "a": [], "g": []}})
    assert 'id="tk-chart"' in panel_slot(page, "ticker")


def test_the_news_panel_carries_the_measurement_not_a_weekend() -> None:
    """`declined` is a decision and has to read as one.

    A panel that says "planned for Weekend 3" about work that was measured and
    dropped is the shell reporting a roadmap the project does not have.
    """
    news = next(p for p in shell.PANELS if p.id == "news")
    state, detail = news.resolve(loaded_context())
    assert state == shell.DECLINED
    assert not news.weekend, "a declined panel must not also promise a weekend"
    # The measurement, because the number is the part that makes it a decision
    # rather than an opinion, and the reopening condition is the part that
    # keeps it from being permanent.
    assert "13.4%" in detail
    assert "lead time" in detail


def test_declined_is_not_the_same_chip_as_not_built() -> None:
    """Two different facts, and the page has to show the difference without
    relying on colour -- they share muted ink deliberately."""
    assert shell.DECLINED in shell.STATES
    assert shell.STATE_STYLE[shell.DECLINED][1] != \
        shell.STATE_STYLE[shell.NOT_BUILT][1]
    page = shell.render(loaded_context())
    assert 'data-state="declined"' in page
    assert f">{shell.DECLINED}<" in page or f"{shell.DECLINED}</span>" in page


# --- the field that made this possible ----------------------------------


def test_no_panel_declares_a_state_reason_nothing_reads() -> None:
    """``waiting_on`` was the mechanism, not the accident.

    Three panels carried a hand-written reason string in that field and
    ``resolve`` never looked at it -- the probe supplied the detail. A field no
    consumer reads cannot be contradicted by anything, so it went stale
    silently and stayed stale until someone read it by eye.

    So: every field on ``Panel`` has to be read somewhere in the module. The
    test is crude on purpose -- a name that appears only in its own
    declaration is the signature of the whole problem.
    """
    import inspect

    source = inspect.getsource(shell)
    for name in shell.Panel.__dataclass_fields__:
        uses = len(re.findall(rf"\.{re.escape(name)}\b", source))
        assert uses, (
            f"Panel.{name} is declared and never read in shell.py. A field "
            "nothing consumes is a comment that looks like code -- which is "
            "exactly how waiting_on held three stale strings for a month. "
            "Either read it or delete it."
        )
    # Named rather than grepped: the module docstring discusses `waiting_on`
    # deliberately, and a test that tripped over the note explaining the ban
    # would be the same mistake as the banned-SQL-function test matching its
    # own comment.
    assert "waiting_on" not in shell.Panel.__dataclass_fields__, (
        "waiting_on is back as a field. Whatever it now holds, the probe "
        "already decides both the state and the reason -- see the module "
        "docstring."
    )


# --- the third kind of drift ---------------------------------------------


def test_a_panel_cannot_say_not_built_while_its_engine_runs() -> None:
    """**The drift neither existing check can see.**

    ``test_the_spec_table_and_the_panel_map_agree`` compares the doc to the panel
    map. The probes compare the panel map to the data. Nothing compared the panel
    map to the *code* -- so ``multiples`` and ``decks`` advertised "not built --
    Beyond" while ``screens/deal_multiples.py`` was returning rows and
    ``decks.py`` had rendered three decks that were read and commented on.

    An engine that runs while its panel says "not built" is worse than a missing
    panel: a reader looking at the shell concludes the work does not exist, which
    is the precise failure the four-state map was built to prevent.
    """
    import importlib.util

    drift = []
    for panel in shell.PANELS:
        state, _detail = panel.resolve(loaded_context())
        if state != shell.NOT_BUILT or not panel.engine:
            continue
        if importlib.util.find_spec(panel.engine) is not None:
            drift.append(f"{panel.id}: resolves {state!r} but {panel.engine} "
                         "imports")
    assert not drift, (
        "a panel claims not to exist while its engine does:\n  "
        + "\n  ".join(drift)
        + "\n\nEither wire a probe so the panel reports its real state, or drop "
        "the engine declaration if the module is not what backs it."
    )


def test_every_live_panel_declares_the_module_that_backs_it() -> None:
    """Otherwise the check above passes vacuously.

    A panel with no ``engine`` is exempt from the drift check, so an undeclared
    engine is a hole in it rather than a neutral omission. ``declined`` panels are
    genuinely exempt: news has no module by decision.
    """
    missing = [p.id for p in shell.PANELS if not p.engine and not p.declined]
    assert not missing, (
        f"these panels declare no engine, so the drift check cannot see them: "
        f"{missing}"
    )


def test_every_declared_engine_actually_imports() -> None:
    """A typo in an engine path would silently exempt that panel from the drift
    check -- the declaration would be there and never resolve."""
    import importlib.util

    broken = [(p.id, p.engine) for p in shell.PANELS
              if p.engine and importlib.util.find_spec(p.engine) is None]
    assert not broken, f"engine paths that do not resolve: {broken}"
