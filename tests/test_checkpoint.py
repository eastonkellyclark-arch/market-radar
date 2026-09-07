"""Checkpoints are the reason a killed sweep is recoverable. Test them hard."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from marketradar.checkpoint import Checkpoint, chunked


def _cp(tmp_path: Path, run_id: str = "r1", total: int = 5) -> Checkpoint:
    return Checkpoint.load_or_create("t", run_id, total, directory=tmp_path)


# --- chunking -------------------------------------------------------------

def test_chunked_splits_evenly() -> None:
    assert chunked([1, 2, 3, 4], 2) == [[1, 2], [3, 4]]


def test_chunked_keeps_a_short_final_chunk() -> None:
    assert chunked([1, 2, 3], 2) == [[1, 2], [3]]


def test_chunked_of_empty_is_empty() -> None:
    assert chunked([], 10) == []


def test_chunked_rejects_zero_size() -> None:
    with pytest.raises(ValueError):
        chunked([1], 0)


def test_chunk_boundaries_are_reproducible() -> None:
    """Chunk N must mean the same thing on the resume run."""
    items = sorted(f"T{i:04d}" for i in range(250))
    assert chunked(items, 40) == chunked(items, 40)


# --- progress -------------------------------------------------------------

def test_new_checkpoint_has_everything_pending(tmp_path: Path) -> None:
    cp = _cp(tmp_path)
    assert cp.pending() == [0, 1, 2, 3, 4]
    assert cp.done_count == 0


def test_mark_done_persists_immediately(tmp_path: Path) -> None:
    """Saving per chunk is the point: the alternative is choosing how much
    work you are willing to lose."""
    cp = _cp(tmp_path)
    cp.mark_done(0, rows=100)
    assert cp.path.is_file()

    reloaded = _cp(tmp_path)
    assert reloaded.is_done(0)
    assert reloaded.pending() == [1, 2, 3, 4]
    assert reloaded.detail(0)["rows"] == 100


def test_resume_skips_completed_chunks(tmp_path: Path) -> None:
    cp = _cp(tmp_path)
    for i in (0, 1, 2):
        cp.mark_done(i, rows=10, attempted=50)

    resumed = _cp(tmp_path)
    assert resumed.done_count == 3
    assert resumed.pending() == [3, 4]
    assert resumed.sum_of("rows") == 30
    assert resumed.sum_of("attempted") == 150


def test_out_of_order_completion_is_tracked_correctly(tmp_path: Path) -> None:
    cp = _cp(tmp_path)
    cp.mark_done(4)
    cp.mark_done(1)
    assert _cp(tmp_path).pending() == [0, 2, 3]


# --- invalidation ---------------------------------------------------------

def test_different_run_id_discards_the_checkpoint(tmp_path: Path) -> None:
    cp = _cp(tmp_path, run_id="r1")
    cp.mark_done(0)
    assert _cp(tmp_path, run_id="r2").done_count == 0


def test_changed_chunk_count_discards_the_checkpoint(tmp_path: Path) -> None:
    """The work list changed, so chunk N no longer means the same thing.

    Resuming here would silently skip real tickers.
    """
    cp = _cp(tmp_path, total=5)
    cp.mark_done(0)
    assert _cp(tmp_path, total=6).done_count == 0


def test_corrupt_checkpoint_is_discarded_not_fatal(tmp_path: Path) -> None:
    """A half-written file from a kill must not wedge the next run."""
    cp = _cp(tmp_path)
    cp.mark_done(0)
    cp.path.write_text("{ this is not json", encoding="utf-8")
    assert _cp(tmp_path).done_count == 0


def test_unknown_format_version_is_discarded(tmp_path: Path) -> None:
    cp = _cp(tmp_path)
    cp.mark_done(0)
    data = json.loads(cp.path.read_text(encoding="utf-8"))
    data["format_version"] = 999
    cp.path.write_text(json.dumps(data), encoding="utf-8")
    assert _cp(tmp_path).done_count == 0


def test_clear_removes_the_file(tmp_path: Path) -> None:
    cp = _cp(tmp_path)
    cp.mark_done(0)
    cp.clear()
    assert not cp.path.exists()
    assert _cp(tmp_path).done_count == 0


# --- atomicity ------------------------------------------------------------

def test_save_leaves_no_temp_files_behind(tmp_path: Path) -> None:
    cp = _cp(tmp_path)
    for i in range(5):
        cp.mark_done(i)
    assert not list(tmp_path.glob("*.tmp"))


def test_existing_checkpoint_is_not_truncated_on_rewrite(tmp_path: Path) -> None:
    """os.replace is atomic: a reader never sees a half-written file."""
    cp = _cp(tmp_path)
    cp.mark_done(0, rows=1)
    first = cp.path.read_text(encoding="utf-8")
    cp.mark_done(1, rows=2)
    second = cp.path.read_text(encoding="utf-8")
    assert json.loads(first)["completed"].keys() < json.loads(second)["completed"].keys()
