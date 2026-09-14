"""The automation around a deck: the prune, the disk index, and the link.

None of this renders a slide -- `tests/test_decks.py` does that. This is the
machinery that makes a nightly run survivable: forty files a night accumulate,
the dashboard has to find them without a server, and a pruned file must stop
being offered the moment it is gone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from marketradar import cli
from marketradar.dashboard import panels


def deck(root: Path, run: str, cik: str, name: str = "acme-corp",
         size: int = 1024) -> Path:
    """One .pptx-shaped file. Contents are irrelevant; the path is the subject."""
    target = root / run / f"{cik}_{name}.pptx" if run else root / f"{cik}_{name}.pptx"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"x" * size)
    return target


# --- the prune ---------------------------------------------------------


def test_the_prune_keeps_the_newest_runs_and_reports_what_it_freed(tmp_path):
    """40 decks a night at 58 KB is 2.3 MB a day and 840 MB a year.

    Measured 2026-09-13: 27 decks came to 1,557 KB, 58 KB each. So the prune is
    not optional at this cadence, and what it removed is printed rather than
    silent -- a job that quietly deletes a month of work is worse than one that
    fills a disk.
    """
    root = tmp_path / "promoted"
    for day in ("2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10"):
        deck(root, day, "0000066740", size=2048)
    freed, dropped = cli._prune_promoted(root, keep=2)
    assert dropped == ["2026-09-07", "2026-09-08"]
    assert freed == 4096
    assert sorted(p.name for p in root.iterdir()) == ["2026-09-09", "2026-09-10"]


def test_the_prune_counts_runs_and_not_days(tmp_path) -> None:
    """**By run, so a market holiday does not expire a month of decks.**

    An age test looks equivalent and is not: thirty *days* of retention over a
    stretch of long weekends is twenty-one runs, and the number a reader has in
    mind when they say "keep a month" is the number of mornings they could look
    back on. Runs are also what the directory names are, so the count and the
    listing cannot disagree.
    """
    root = tmp_path / "promoted"
    for day in ("2026-01-02", "2026-04-15", "2026-09-11"):
        deck(root, day, "0000066740")
    _freed, dropped = cli._prune_promoted(root, keep=2)
    assert dropped == ["2026-01-02"], (
        "the prune dropped by age rather than by run; the January run is the "
        "oldest of three, not the only one older than a month")


def test_the_prune_reads_the_directory_name_and_not_its_mtime(tmp_path):
    """A file system's mtime is not a fact about the session.

    A backup, a virus scanner or a copy moves it, and then the newest directory
    by mtime is whichever one was touched last. The name is the session the deck
    is a deck of, so the name is what orders them.

    Mutated to confirm it fires: sorting on `p.stat().st_mtime` instead makes
    this test drop the run it should keep, because the oldest-named directory is
    the one written last here.
    """
    root = tmp_path / "promoted"
    for day in ("2026-09-10", "2026-09-09", "2026-09-08"):
        deck(root, day, "0000066740")          # written newest-name-first
    _freed, dropped = cli._prune_promoted(root, keep=1)
    assert dropped == ["2026-09-08", "2026-09-09"]


def test_the_prune_leaves_hand_made_decks_and_anything_undated(tmp_path):
    """`--cik` decks are not a run and are never pruned.

    Somebody asked for those. The dated directories are the automated output and
    the only thing with a retention policy; a stray directory under the same root
    is left alone rather than guessed at.
    """
    root = tmp_path / "promoted"
    deck(root, "2026-09-10", "0000066740")
    deck(root, "scratch", "0000320193")
    deck(root, "", "0000001750")
    _freed, dropped = cli._prune_promoted(root, keep=1)
    assert dropped == []
    assert (root / "scratch").is_dir()
    assert (root / "0000001750_acme-corp.pptx").is_file()


def test_the_prune_on_a_missing_root_is_a_no_op(tmp_path) -> None:
    assert cli._prune_promoted(tmp_path / "nope", keep=5) == (0, [])


def test_keeping_zero_runs_deletes_nothing(tmp_path) -> None:
    """A guard, not a feature.

    `--keep-runs 0` reads like "keep nothing" and would mean "delete everything
    including tonight's", which is never what somebody typing a number wants. It
    refuses rather than obeying.
    """
    root = tmp_path / "promoted"
    deck(root, "2026-09-10", "0000066740")
    assert cli._prune_promoted(root, keep=0) == (0, [])
    assert (root / "2026-09-10").is_dir()


# --- the disk index ----------------------------------------------------


def test_the_index_keys_on_a_normalised_cik(tmp_path) -> None:
    """A deck written before the CIK fix has an unpadded filename.

    `.decks/66740_3m-co.pptx` and `.decks/promoted/.../0000066740_3m-co.pptx`
    are the same filer, and the dashboard looks it up with a padded key. Keying
    the index on anything else would make every pre-existing deck invisible.
    """
    root = tmp_path / "decks"
    deck(root, "", "66740", "3m-co")
    index = cli._deck_index(root, tmp_path / "dashboard")
    assert list(index) == ["0000066740"]


def test_the_newest_run_wins_on_an_explicit_key(tmp_path) -> None:
    """Two decks for one filer must resolve the same way every build.

    `max` on (run, filename) rather than "whichever the walk reached last": a
    directory listing's order is not a promise, and a dashboard that linked a
    different night's deck on every build would be indistinguishable from one
    that linked the right one.
    """
    root = tmp_path / "decks"
    deck(root, "2026-09-08", "0000066740")
    deck(root, "2026-09-11", "0000066740")
    deck(root, "2026-09-09", "0000066740")
    index = cli._deck_index(root, tmp_path / "dashboard")
    assert index["0000066740"]["run"] == "2026-09-11"
    assert "2026-09-11" in index["0000066740"]["href"]


def test_a_dated_run_beats_a_hand_made_deck(tmp_path) -> None:
    """"on demand" sorts below every date, so the automated one wins.

    Deliberate rather than incidental: the nightly deck is built from the current
    valuation and the hand-made one may be weeks old, and there is no date on the
    hand-made path to compare. Both are on disk and the fresher source is the one
    with a session attached to it.
    """
    root = tmp_path / "decks"
    deck(root, "", "0000066740")
    deck(root, "2026-09-11", "0000066740")
    index = cli._deck_index(root, tmp_path / "dashboard")
    assert index["0000066740"]["run"] == "2026-09-11"


def test_the_href_is_relative_to_the_page_and_not_absolute(tmp_path) -> None:
    """**Relative, so the tree can move and no home path lands in the HTML.**

    The dashboard is opened from `file://` and written to `.dashboard/index.html`,
    so a deck in `.decks/promoted/<day>/` is `../.decks/...` away. An absolute
    path would bake this machine's home directory into a file the repo rules
    already keep out of git, and would break the moment the checkout moved.

    Forward slashes on every platform: a backslash in an href is not a path
    separator to a browser.
    """
    root = tmp_path / "decks"
    deck(root, "2026-09-11", "0000066740", "three-m")
    index = cli._deck_index(root, tmp_path / "dashboard")
    href = index["0000066740"]["href"]
    assert href == "../decks/2026-09-11/0000066740_three-m.pptx"
    assert "\\" not in href
    assert not href.startswith("/")


def test_the_index_ignores_anything_that_is_not_a_deck(tmp_path) -> None:
    root = tmp_path / "decks"
    deck(root, "2026-09-11", "0000066740")
    (root / "2026-09-11" / "notes.txt").write_text("x", encoding="utf-8")
    (root / "2026-09-11" / "_scratch.pptx").write_bytes(b"x")
    index = cli._deck_index(root, tmp_path / "dashboard")
    assert list(index) == ["0000066740"]


def test_an_index_of_a_missing_directory_is_empty_not_an_error(tmp_path):
    assert cli._deck_index(tmp_path / "nope", tmp_path) == {}


# --- the link ----------------------------------------------------------


def test_a_filer_with_a_deck_on_disk_gets_a_link(tmp_path) -> None:
    """The useful action is "open it", not "here is a command to type"."""
    index = {"0000066740": {"href": "../decks/2026-09-11/0000066740_3m.pptx",
                            "run": "2026-09-11", "name": "0000066740_3m.pptx",
                            "kb": "58"}}
    html = panels.deck_button("0000066740", index)
    assert 'class="decklink"' in html
    assert 'href="../decks/2026-09-11/0000066740_3m.pptx"' in html
    assert "58 KB" in html and "2026-09-11" in html
    assert "deckbtn" not in html
    assert "data-deck-cik" not in html, (
        "a link must not carry the copy handler's hook, or clicking it would "
        "copy a command instead of opening the file")


def test_a_filer_without_one_still_gets_the_command(tmp_path) -> None:
    """**The fallback is the point, not a leftover.**

    The nightly run renders only what three sentinels promoted -- 27 of 2,569
    valuations on the measured day -- so most rows in the DCF panel will never
    have a file. A row offering nothing would read as "no deck is possible here",
    which is false: the command renders one on demand.
    """
    html = panels.deck_button("0000001750", {"0000066740": {"href": "x",
                                                            "run": "r",
                                                            "name": "n",
                                                            "kb": "1"}})
    assert 'class="deckbtn"' in html
    assert 'data-deck-cik="0000001750"' in html
    assert "uv run mr decks --cik 0000001750" in html
    assert "No deck on disk" in html


def test_the_button_says_which_of_the_two_it_is(tmp_path) -> None:
    """A link and a button that looked alike would make "did that do anything"
    a thing to remember. The title text differs and so does the class."""
    with_file = panels.deck_button("0000066740", {
        "0000066740": {"href": "h", "run": "2026-09-11", "name": "n",
                       "kb": "58"}})
    without = panels.deck_button("0000066740", {})
    assert "Open" in with_file and "Copy" not in with_file
    assert "Copy" in without and "Open" not in without


def test_no_cik_renders_no_control() -> None:
    assert panels.deck_button("", {"x": {}}) == ""


# --- the panel's own report --------------------------------------------


def test_the_deck_panel_reports_the_newest_run_not_just_a_count() -> None:
    """**This is the answer to the original objection to running nightly.**

    The argument was that a directory generated every night is indistinguishable
    from one where the generator broke last Tuesday. It is indistinguishable from
    the *directory*; it is perfectly distinguishable from the newest run's date,
    so the panel renders the date.
    """
    html = panels._deck_runs_html({
        "0000066740": {"href": "h", "run": "2026-09-11", "name": "a", "kb": "58"},
        "0000320193": {"href": "h", "run": "2026-09-11", "name": "b", "kb": "58"},
        "0000001750": {"href": "h", "run": "on demand", "name": "c", "kb": "44"},
    })
    assert "2026-09-11" in html
    assert "3 filers" in html
    assert "1 rendered by hand" in html


def test_an_empty_disk_says_which_command_fills_it() -> None:
    html = panels._deck_runs_html({})
    assert "nothing on disk" in html
    assert "mr decks --promoted" in html


# --- the command's own refusals ----------------------------------------


def test_promoted_and_cik_together_are_refused(monkeypatch, capsys) -> None:
    """A chosen filer must not land inside a dated automated run.

    The run directory is the promoted set for that session, and the prune deletes
    it. A hand-picked deck mixed in would be deleted with it, and the run would
    no longer be a record of what the sentinels found.
    """
    from marketradar import storage

    monkeypatch.setattr(storage, "connect", lambda *a, **k: None)
    monkeypatch.setattr(
        cli, "_dcf_rows",
        lambda con, out: {"rows": [{"cik": "66740", "company": "3M CO"}]})
    args = cli.build_parser().parse_args(
        ["decks", "--promoted", "--cik", "0000066740"])
    assert cli._cmd_decks(args) == cli.EXIT_ERROR
    assert "Do them separately" in capsys.readouterr().err


def test_the_decks_parser_carries_every_flag_the_scheduled_task_uses() -> None:
    """The scheduled task is the only unattended caller, so its flags are a
    contract. A rename here breaks a `.cmd` file nothing else tests."""
    args = cli.build_parser().parse_args([
        "decks", "--promoted", "--out", ".decks", "--keep-runs", "30",
        "--form4-window", "7"])
    assert args.promoted is True
    assert args.keep_runs == 30
    assert args.form4_window == 7
    assert args.date is None


def test_the_scheduled_task_runs_the_command_the_parser_accepts(repo_root):
    """**The `.cmd` file and the parser must not drift, and nothing else checks.**

    A scheduled task fails into a log file nobody is watching, so a flag renamed
    here would go unnoticed until somebody wondered why the dashboard had stopped
    linking decks. This parses the wrapper's own command line and hands it to the
    real parser.
    """
    script = repo_root / "scripts" / "run_decks.cmd"
    assert script.is_file(), "the scheduled task's wrapper is missing"
    body = script.read_text(encoding="utf-8")
    line = next(ln for ln in body.splitlines() if " decks " in ln)
    flags = line.split(" decks ", 1)[1].split(">>")[0].split()
    flags = [f for f in flags if not f.startswith("%")]
    args = cli.build_parser().parse_args(["decks", *flags])
    assert args.promoted is True, (
        f"the wrapper runs `mr decks {' '.join(flags)}`, which is not a "
        "promoted run")
