"""Per-chunk checkpoints so a long sweep survives being interrupted.

A whole-market Tiingo sweep is ~12,000 requests against a 10,000/hour cap,
so it cannot fit in one rate-limit window and will take over an hour. Over
that span it *will* be interrupted — a 429 storm, a dropped connection, an
Actions timeout, a laptop lid. Losing 9,000 completed requests because the
9,001st failed is not an acceptable failure mode, and at this quota it is
not a recoverable one either.

So: fixed-size chunks over a deterministically ordered work list, a
checkpoint written after each chunk completes, and resume as the default on
re-invocation. Restarting is possible but must be asked for explicitly.

The checkpoint file is written atomically (temp file plus ``os.replace``),
because the most likely moment to be killed is the moment you are writing
the file that says what you have done.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CHECKPOINT_DIR = Path(".checkpoints")
FORMAT_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Checkpoint:
    """Tracks which chunks of a run are already done.

    Attributes:
        path: where this checkpoint persists.
        run_id: identifies the logical run. A different run_id is a
            different sweep and shares nothing.
        total_chunks: how many chunks the work list divides into. A change
            here invalidates the checkpoint, because chunk N no longer means
            the same thing.
        completed: chunk index -> what that chunk produced.
    """

    path: Path
    run_id: str
    total_chunks: int
    completed: dict[str, dict[str, Any]] = field(default_factory=dict)

    # -- persistence -------------------------------------------------------

    @classmethod
    def load_or_create(
        cls, name: str, run_id: str, total_chunks: int, *, directory: Path | None = None
    ) -> Checkpoint:
        """Resume an existing checkpoint, or start a fresh one.

        An existing checkpoint is discarded when it does not match this run:
        a different run_id, a different chunk count, or an unreadable file.
        Resuming against a changed work list would silently skip real work.
        """
        directory = directory or CHECKPOINT_DIR
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{name}.json"

        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = None
            if (
                isinstance(data, dict)
                and data.get("format_version") == FORMAT_VERSION
                and data.get("run_id") == run_id
                and data.get("total_chunks") == total_chunks
            ):
                return cls(
                    path=path,
                    run_id=run_id,
                    total_chunks=total_chunks,
                    completed=data.get("completed", {}),
                )

        return cls(path=path, run_id=run_id, total_chunks=total_chunks)

    def save(self) -> None:
        """Write atomically. A half-written checkpoint is worse than none."""
        payload = {
            "format_version": FORMAT_VERSION,
            "run_id": self.run_id,
            "total_chunks": self.total_chunks,
            "updated_at": _now(),
            "completed": self.completed,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def clear(self) -> None:
        self.completed = {}
        self.path.unlink(missing_ok=True)

    # -- progress ----------------------------------------------------------

    def is_done(self, index: int) -> bool:
        return str(index) in self.completed

    def mark_done(self, index: int, **detail: Any) -> None:
        """Record a finished chunk and persist immediately.

        Saving on every chunk rather than periodically is deliberate: the
        cost is one small write per chunk, and the alternative is choosing
        how much work you are willing to lose.
        """
        self.completed[str(index)] = {"at": _now(), **detail}
        self.save()

    def pending(self) -> list[int]:
        return [i for i in range(self.total_chunks) if not self.is_done(i)]

    @property
    def done_count(self) -> int:
        return len(self.completed)

    def detail(self, index: int) -> dict[str, Any] | None:
        return self.completed.get(str(index))

    def sum_of(self, key: str) -> int:
        return sum(int(d.get(key, 0)) for d in self.completed.values())

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"Checkpoint(run_id={self.run_id!r}, "
            f"{self.done_count}/{self.total_chunks} chunks done)"
        )


def chunked(items: list[Any], size: int) -> list[list[Any]]:
    """Split a work list into fixed-size chunks.

    Chunk boundaries must be reproducible across runs, so callers pass an
    already-sorted list. If the ordering changes, chunk N means something
    different and the checkpoint is invalid — which is why total_chunks is
    part of the checkpoint's identity.
    """
    if size < 1:
        raise ValueError("chunk size must be >= 1")
    return [items[i : i + size] for i in range(0, len(items), size)]
