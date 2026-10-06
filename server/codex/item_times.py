"""Per-item timestamps for Codex history — the only source of per-block times.

`thread/read` — the replay source the Codex design fixes (D8) — hands back items
with NO time of their own (codex-cli 0.153.2: a commandExecution carries
`durationMs` but no start). The live `item/started` / `item/completed`
notifications DO carry exact millisecond times, but they exist only in flight.
This module journals those times as they pass through the driver, in a
ViberPit-owned sidecar the replay adapter reads back, so a reloaded Codex session
shows when each block actually happened instead of its turn's start.

Layout: ``<root>/<thread id>.jsonl`` where *root* is the account's
``codex-item-times`` directory (derived by the orchestrator from the account's
config dir — never from ``CODEX_HOME``, which is Codex private state and stays
untouched). One JSON object per line:

    {"i": "<item id>", "s": <started ms>}
    {"i": "<item id>", "c": <completed ms>}

Append-only, so a crash mid-write costs at most one torn trailing line, which the
reader skips. Host-local by design: the same daemon that owns the app-server (and
so is the only one that can `thread/read` the thread) writes and reads it, so a
replay never looks for a file another host wrote. A thread with no sidecar (history
from before this landed, or a transferred session) simply replays with the
coarser fallbacks in ``server/codex/replay.py``.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("server.codex.item_times")

ITEM_TIMES_DIRNAME = "codex-item-times"

# A thread id names a file, so it must be a plain token: no separators, no dots
# that could spell a traversal, nothing the app-server would never mint anyway
# (thread ids are UUIDv7 in practice).
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def thread_sidecar_path(root: Path, thread_id: str, suffix: str) -> Path | None:
    """A per-thread sidecar ``<root>/<thread id><suffix>``, or None for an id unsafe
    to name a file with (never raises — an odd id means "no sidecar", not a failure).
    THE id rule for every ViberPit-owned per-thread file under the account's
    sidecar root (this journal, ``thread_surface``)."""
    if not _SAFE_ID.match(thread_id):
        return None
    return root / f"{thread_id}{suffix}"


def item_times_path(root: Path, thread_id: str) -> Path | None:
    """The item-times sidecar for *thread_id* under *root* (see ``thread_sidecar_path``)."""
    return thread_sidecar_path(root, thread_id, ".jsonl")


@dataclass(frozen=True, slots=True)
class ItemTimes:
    """Item id → epoch-ms, one map per lifecycle edge. Immutable once loaded."""

    started: Mapping[str, int] = field(default_factory=dict)
    completed: Mapping[str, int] = field(default_factory=dict)


EMPTY_ITEM_TIMES = ItemTimes()


def load_item_times(path: Path | None) -> ItemTimes:
    """Read a sidecar. Missing, unreadable, or partly corrupt files all degrade to
    whatever could be read (an absent file is simply empty) — a replay must never
    fail over its timestamps. Runs synchronously; call it off the loop."""
    if path is None:
        return EMPTY_ITEM_TIMES
    started: dict[str, int] = {}
    completed: dict[str, int] = {}
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    o = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(o, dict) or not isinstance(o.get("i"), str):
                    continue
                s, c = o.get("s"), o.get("c")
                if isinstance(s, int) and not isinstance(s, bool):
                    started[o["i"]] = s
                if isinstance(c, int) and not isinstance(c, bool):
                    completed[o["i"]] = c
    except OSError:
        return ItemTimes(started=started, completed=completed)
    return ItemTimes(started=started, completed=completed)


class ItemTimesWriter:
    """Appends item lifecycle times to the per-thread sidecar under *root*.

    Best-effort: a write failure is logged once per thread and that thread is
    then skipped, so a full disk or a bad permission costs timestamps, never a
    session. Each record is one ``write`` of one line on an ``O_APPEND`` handle,
    so concurrent appends (the driver records from a worker thread) can
    interleave lines but never tear one.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._failed: set[str] = set()

    @property
    def root(self) -> Path:
        return self._root

    def record_started(self, thread_id: str, item_id: str, started_at_ms: int) -> None:
        self._append(thread_id, {"i": item_id, "s": int(started_at_ms)})

    def record_completed(self, thread_id: str, item_id: str, completed_at_ms: int) -> None:
        self._append(thread_id, {"i": item_id, "c": int(completed_at_ms)})

    def _append(self, thread_id: str, record: dict) -> None:
        if thread_id in self._failed:
            return
        path = item_times_path(self._root, thread_id)
        if path is None:
            self._failed.add(thread_id)
            log.warning("codex.item_times: refusing to journal thread id %r", thread_id)
            return
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, separators=(",", ":")) + "\n")
        except OSError as exc:
            self._failed.add(thread_id)
            log.warning("codex.item_times: journal failed for thread %s at %s: %s — "
                        "timestamps for this thread will fall back to turn start",
                        thread_id, path, exc)


def uuid7_ms(value: str) -> float | None:
    """The creation time embedded in a UUIDv7 *value*, as epoch milliseconds, or
    None for anything that is not one.

    The app-server mints thread, turn and userMessage ids as UUIDv7 (verified on
    codex-cli 0.153.2: `01a0a0d7-f021-7343-…` decodes to the second the turn's
    `startedAt` reports). Agent messages (`msg_…`), reasoning (`rs_…`) and
    command executions (`exec-<uuid4>`) are not, and answer None.
    """
    try:
        u = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return None
    if u.version != 7:
        return None
    return float((u.int >> 80) & ((1 << 48) - 1))
