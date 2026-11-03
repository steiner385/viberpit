"""Rewind to here (D-9): the refusal vocabulary and the CLI control-request adapter
shared by the Claude driver, the orchestrator, the daemon RPC and the API route.

The CLI surfaces are internal (constitution I) and verified against claude 2.1.317 in
``docs/superpowers/specs/2026-11-03-d9-rewind-verification.md``:

- ``rewind_files {user_message_id, dry_run?}`` answers ``{canRewind, error?,
  filesChanged?, insertions?, deletions?, skippedLinks?}``. A ``canRewind:false``
  without ``dry_run`` comes back as a control *error* carrying the same text.
- ``rewind_conversation {target_message_uuid, last_seen_user_message_uuid?}`` answers
  ``{rewound:true, targetMessageUuid, prefillText, precedingAssistantUuid}`` or
  ``{rewound:false, error, reason}``.

Every refusal raises :class:`RewindError` with one of ``REWIND_CODES``; it carries
``error_data`` (as D-8's ``ForkError`` does) so the code survives the RPC envelope
and the daemon logs the refusal at INFO. ``server/tests/test_rewind_cli_contract.py``
re-checks the wire against the installed binary.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from viberpit import paths
from viberpit.fs_beneath import (SymlinkInPath, open_beneath, replace_file_beneath,
                                 unlink_file_beneath)

log = logging.getLogger(__name__)

#: Restore the files to the target prompt's checkpoint, then cut the conversation.
MODE_FILES_AND_CONVERSATION = "files_and_conversation"
#: Cut the conversation only (no checkpoint for the target, or the user's choice).
MODE_CONVERSATION = "conversation"
MODES = frozenset({MODE_FILES_AND_CONVERSATION, MODE_CONVERSATION})

#: The kill switch is off, the engine cannot rewind, or the session has no live
#: client (or the SDK client lacks the control channel).
REWIND_UNAVAILABLE = "REWIND_UNAVAILABLE"
#: A turn is running, a permission card or question is pending, or the CLI has
#: prompts queued.
REWIND_BUSY = "REWIND_BUSY"
#: The prompt has no file checkpoint (sent before checkpointing was on, or the
#: process runs without it). The conversation can still be rewound.
REWIND_NO_CHECKPOINT = "REWIND_NO_CHECKPOINT"
#: Later turns exist that the caller has not seen, or the session changed under
#: the rewind.
REWIND_STALE_TARGET = "REWIND_STALE_TARGET"
#: The target is not on the session's active conversation chain.
REWIND_TARGET_NOT_FOUND = "REWIND_TARGET_NOT_FOUND"
#: Cutting at the target would split a tool call from its result.
REWIND_SPLITS_TOOL_CALL = "REWIND_SPLITS_TOOL_CALL"
#: The target is not a user prompt.
REWIND_NOT_USER_MESSAGE = "REWIND_NOT_USER_MESSAGE"
#: The undo backup would exceed its size cap.
REWIND_TOO_LARGE = "REWIND_TOO_LARGE"
#: The rewind is no longer undoable (a newer rewind, or a prompt since).
REWIND_UNDO_EXPIRED = "REWIND_UNDO_EXPIRED"
#: Anything else the CLI refused or failed with.
REWIND_FAILED = "REWIND_FAILED"
REWIND_CODES = frozenset({REWIND_UNAVAILABLE, REWIND_BUSY, REWIND_NO_CHECKPOINT,
                          REWIND_STALE_TARGET, REWIND_TARGET_NOT_FOUND,
                          REWIND_SPLITS_TOOL_CALL, REWIND_NOT_USER_MESSAGE,
                          REWIND_TOO_LARGE, REWIND_UNDO_EXPIRED, REWIND_FAILED})

#: Every ``reason`` the 2.1.317 ``rewind_conversation`` handler can send (its ``bw``
#: table) → our code. ``expires_at`` and ``refuse_if_usage_limited`` are never sent,
#: so ``expired`` / ``usage_limited`` are unreachable; they map to FAILED.
CLI_REASON_CODES: dict[str, str] = {
    "turn_running": REWIND_BUSY,
    "commands_queued": REWIND_BUSY,
    "prompt_pending": REWIND_BUSY,
    "stale_target": REWIND_STALE_TARGET,
    "unseen_later_turn": REWIND_STALE_TARGET,
    "state_changed": REWIND_STALE_TARGET,
    "target_not_found": REWIND_TARGET_NOT_FOUND,
    "target_splits_tool_call": REWIND_SPLITS_TOOL_CALL,
    "delivered_poll_events_in_range": REWIND_SPLITS_TOOL_CALL,
    "poll_tool_result_target": REWIND_NOT_USER_MESSAGE,
    "persist_failed": REWIND_FAILED,
    "usage_limited": REWIND_FAILED,
    "expired": REWIND_FAILED,
}

#: ``rewind_files`` error text → code. "Not enabled" means the process was spawned
#: without checkpointing, so for the caller it is the same as no checkpoint: the
#: rewind is offered as conversation-only.
_ERROR_TEXT_CODES: tuple[tuple[str, str], ...] = (
    ("No file checkpoint found", REWIND_NO_CHECKPOINT),
    ("File rewinding is not enabled", REWIND_NO_CHECKPOINT),
    ("is not a user message", REWIND_NOT_USER_MESSAGE),
)

#: ``error_data["reason"]`` of a ``REWIND_FAILED`` for a state-changing request whose
#: reply never came (timeout, transport failure): it may have been applied, so the
#: caller must restore from its undo backup.
REWIND_OUTCOME_UNKNOWN = "outcome_unknown"

#: Seconds to wait for one rewind control reply (the SDK's own default).
REWIND_CONTROL_TIMEOUT_S = 60.0


class RewindError(Exception):
    """An expected rewind refusal; ``code`` is one of ``REWIND_CODES``."""

    def __init__(self, code: str, message: str | None = None, *,
                 reason: str | None = None) -> None:
        if code not in REWIND_CODES:
            raise ValueError(f"unknown rewind code {code!r}")
        super().__init__(message or code)
        self.code = code
        self.reason = reason
        self.error_data: dict[str, str] = {"code": code}
        if reason is not None:
            self.error_data["reason"] = reason

    @property
    def outcome_unknown(self) -> bool:
        """A state-changing request whose reply never came: it may have been applied."""
        return self.code == REWIND_FAILED and self.reason == REWIND_OUTCOME_UNKNOWN


#: Namespace for ``rewind_id_for`` (a fixed, arbitrary uuid4).
_REWIND_ID_NS = uuid.UUID("c7a1d2e4-5b3f-4e8a-9d61-0f2b7c4e8a19")


def rewind_id_for(session_id: str, request_id: str) -> str:
    """The rewind id for request *request_id* on the session — the same on every retry
    of that request, so a repeated request finds the rewind it made (D-8's
    ``fork_child_id`` rule)."""
    return str(uuid.uuid5(_REWIND_ID_NS, f"{session_id}:{request_id}"))


def map_cli_error(resp: Mapping[str, Any]) -> RewindError:
    """The :class:`RewindError` for a CLI refusal: a ``rewind_conversation`` reply with
    ``rewound:false``, a ``rewind_files`` reply with ``canRewind:false``, or
    ``{"error": text}`` for a control error. Unknown shapes are ``REWIND_FAILED``."""
    reason = resp.get("reason")
    error = resp.get("error")
    text = error if isinstance(error, str) else ""
    if isinstance(reason, str) and reason:
        return RewindError(CLI_REASON_CODES.get(reason, REWIND_FAILED), text or reason,
                           reason=reason)
    for needle, code in _ERROR_TEXT_CODES:
        if needle in text:
            return RewindError(code, text)
    return RewindError(REWIND_FAILED, text or None)


def files_request(user_message_id: str, *, dry_run: bool) -> dict[str, Any]:
    """The ``rewind_files`` control request. The real run is byte-for-byte the SDK's
    own ``rewind_files()`` payload; only the dry run adds ``dry_run``."""
    req: dict[str, Any] = {"subtype": "rewind_files", "user_message_id": user_message_id}
    if dry_run:
        req["dry_run"] = True
    return req


def conversation_request(target_uuid: str, *, last_seen: str | None) -> dict[str, Any]:
    """The ``rewind_conversation`` control request. Without ``last_seen`` the CLI
    refuses any target that has later turns (``stale_target``)."""
    req: dict[str, Any] = {"subtype": "rewind_conversation", "target_message_uuid": target_uuid}
    if last_seen is not None:
        req["last_seen_user_message_uuid"] = last_seen
    return req


@dataclass(frozen=True)
class RewindPreview:
    """A ``rewind_files`` dry run. ``files`` are as the CLI reports them (absolute);
    the CLI gives only aggregate line counts."""
    files: tuple[str, ...]
    insertions: int
    deletions: int
    checkpoint_available: bool

    @classmethod
    def from_cli(cls, resp: Mapping[str, Any]) -> RewindPreview:
        files = resp.get("filesChanged") or ()
        return cls(files=tuple(str(f) for f in files),
                   insertions=int(resp.get("insertions") or 0),
                   deletions=int(resp.get("deletions") or 0),
                   checkpoint_available=bool(resp.get("canRewind")))

    @classmethod
    def no_checkpoint(cls) -> RewindPreview:
        return cls(files=(), insertions=0, deletions=0, checkpoint_available=False)


@dataclass(frozen=True)
class ConversationRewind:
    """A successful ``rewind_conversation``: the cut prompt's text (for the composer)
    and the assistant entry the chain now ends at (``None`` at the first prompt)."""
    prefill: str | None
    preceding_uuid: str | None

    @classmethod
    def from_cli(cls, resp: Mapping[str, Any]) -> ConversationRewind:
        prefill = resp.get("prefillText")
        preceding = resp.get("precedingAssistantUuid")
        return cls(prefill=prefill if isinstance(prefill, str) else None,
                   preceding_uuid=preceding if isinstance(preceding, str) else None)


# ── The undo backup ─────────────────────────────────────────────────────────────
#
# A rewind restores files to a CLI checkpoint, and the CLI cannot go back: the state
# after the last turn is never snapshotted and there is no redo (verification doc §2,
# §3c). So before the real ``rewind_files`` ViberPit copies every path the rewind can
# touch — the dry run's ``filesChanged`` plus every path the transcript's file-history
# index tracks from the target prompt on (a mode-only revert is restored but never
# previewed, second probe) — as bytes + mode, or "absent". Undo writes them back.
#
# Every read and write goes through ``viberpit.fs_beneath`` from a directory fd, so no
# symlink in the agent-controlled tree is ever followed: a symlinked path is recorded
# as skipped (the CLI's own restore refuses symlinks too) and left alone on undo.

#: The undo backup's size cap; a bigger one refuses ``REWIND_TOO_LARGE`` before the
#: rewind touches anything.
REWIND_BACKUP_MAX_BYTES = 64 * 1024 * 1024

_MANIFEST = "manifest.json"
_BLOBS = "blobs"
#: Backup entry states.
STATE_FILE = "file"
STATE_ABSENT = "absent"
STATE_SKIPPED = "skipped"


def backup_dir(session_id: str, rewind_id: str) -> Path:
    """Where rewind *rewind_id* of *session_id* keeps its undo backup."""
    return paths.rewind_dir() / session_id / rewind_id


def _locate(cwd: str, path: str) -> tuple[str, str]:
    """``(root, rel)`` for *path*: beneath the session's cwd (as given, or its realpath —
    the CLI may report either) when it lies there, otherwise beneath ``/``. Only the
    root itself is opened normally; ``rel`` is walked without following symlinks."""
    full = os.path.normpath(path if os.path.isabs(path) else os.path.join(cwd, path))
    for base in dict.fromkeys((os.path.normpath(cwd), os.path.realpath(cwd))):
        if full.startswith(base.rstrip(os.sep) + os.sep):
            return base, full[len(base.rstrip(os.sep)) + 1:]
    return os.sep, full.lstrip(os.sep)


def display_path(cwd: str, path: str) -> str:
    """*path* as the UI shows it: relative to the worktree when it lies there, else
    absolute."""
    root, rel = _locate(cwd, path)
    return rel if root != os.sep else os.sep + rel


@dataclass(frozen=True)
class TrackedFiles:
    """What the transcript's file-history index says about a rewind to one prompt.
    ``paths`` are every path tracked from that prompt's snapshot on (absolute);
    ``absent`` the ones that did not exist at it — the rewind deletes them."""
    paths: frozenset[str]
    absent: frozenset[str]


def tracked_since(jsonl_path: str, target_uuid: str, cwd: str) -> TrackedFiles:
    """Read the ``file-history-snapshot`` / ``file-history-delta`` rows of a CLI
    transcript (verification doc §1.2) from prompt *target_uuid*'s snapshot on.

    A snapshot row keys ``snapshot.trackedFileBackups`` by cwd-relative path; a delta
    row tracks one ``trackingPath`` against an open snapshot. A ``backupFileName`` of
    null means "did not exist", so a path whose state AT the target (its entry in the
    target snapshot, else its first delta after it) is null is one the rewind deletes.
    Rows from branches an earlier rewind dropped are included: over-including only
    backs up a file the rewind leaves alone. A missing or unreadable file reads empty."""
    seen = False
    tracked: dict[str, bool] = {}       # abs path → existed at the target
    at_target: set[str] = set()

    def _abs(rel: str) -> str:
        return os.path.normpath(rel if os.path.isabs(rel) else os.path.join(cwd, rel))

    def _existed(backup: object) -> bool:
        return not (isinstance(backup, dict) and backup.get("backupFileName") is None)

    try:
        fh = open(jsonl_path, "rb")
    except OSError:
        return TrackedFiles(frozenset(), frozenset())
    with fh:
        for line in fh:
            if b"file-history-" not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            kind = row.get("type")
            if kind == "file-history-snapshot":
                snap = row.get("snapshot")
                files = snap.get("trackedFileBackups") if isinstance(snap, dict) else None
                is_target = row.get("messageId") == target_uuid
                seen = seen or is_target
                if not seen or not isinstance(files, dict):
                    continue
                for rel, backup in files.items():
                    if not isinstance(rel, str) or not rel:
                        continue
                    p = _abs(rel)
                    if is_target:
                        at_target.add(p)
                        tracked[p] = _existed(backup)
                    else:
                        tracked.setdefault(p, True)
            elif kind == "file-history-delta":
                if not seen and row.get("snapshotMessageId") != target_uuid:
                    continue
                rel = row.get("trackingPath")
                if not isinstance(rel, str) or not rel:
                    continue
                p = _abs(rel)
                if p not in at_target and p not in tracked:
                    tracked[p] = _existed(row.get("backup"))
    return TrackedFiles(frozenset(tracked),
                        frozenset(p for p, existed in tracked.items() if not existed))


def prompted_since(jsonl_path: str, offset: int) -> bool:
    """Has a turn been written to the transcript past byte *offset* — the length it had
    just before a rewind cut the conversation? A ``rewind_conversation`` appends only
    ``last-prompt`` / ``cost-state`` rows, so any conversation entry after it is a turn
    an undo's truncation would drop. A file now SHORTER than *offset* (rewritten) also
    answers True: truncating it is no longer an undo."""
    try:
        with open(jsonl_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            if fh.tell() < offset:
                return True
            fh.seek(offset)
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("type") in ("user", "assistant"):
                    return True
    except OSError:
        return True
    return False


def truncate_transcript(jsonl_path: str, length: int) -> None:
    """Cut the transcript back to *length* bytes — its exact pre-rewind contents (the
    rewind only appended; verification doc, second probe)."""
    size = os.stat(jsonl_path).st_size
    if size < length:
        raise RewindError(REWIND_FAILED, f"transcript is shorter ({size}) than its "
                          f"pre-rewind length ({length})")
    os.truncate(jsonl_path, length)


def _open_root(root: str) -> int:
    return os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)


def take_backup(dest: Path, cwd: str, files: Iterable[str], *,
                max_bytes: int = REWIND_BACKUP_MAX_BYTES) -> list[dict[str, Any]]:
    """Copy each of *files* (absolute, or relative to *cwd*) into backup dir *dest*:
    a regular file's bytes and mode, ``absent`` for a missing path, ``skipped`` for a
    symlink anywhere on the path or a non-regular leaf. Returns the manifest entries.

    Raises ``RewindError(REWIND_TOO_LARGE)`` once the bytes read pass *max_bytes*,
    leaving no backup behind; nothing outside *dest* is ever written."""
    seen: dict[tuple[str, str], tuple[str, str]] = {}
    for f in files:
        root, rel = _locate(cwd, f)
        key = ("cwd" if root != os.sep else os.sep, rel)
        seen.setdefault(key, (root, rel))
    entries: list[dict[str, Any]] = []
    total = 0
    blobs = dest / _BLOBS
    blobs.mkdir(parents=True, exist_ok=True)
    os.chmod(dest, 0o700)
    roots: dict[str, int] = {}
    try:
        for n, (root, rel) in enumerate(sorted(seen.values())):
            entry: dict[str, Any] = {"root": root, "rel": rel, "path": os.path.join(root, rel),
                                     "state": STATE_ABSENT, "mode": None, "blob": None}
            entries.append(entry)
            if root not in roots:
                roots[root] = _open_root(root)
            try:
                fd = open_beneath(roots[root], rel)
            except SymlinkInPath:
                entry["state"] = STATE_SKIPPED
                continue
            except (FileNotFoundError, NotADirectoryError):
                continue
            with os.fdopen(fd, "rb") as src:
                st = os.fstat(src.fileno())
                if not stat.S_ISREG(st.st_mode):
                    entry["state"] = STATE_SKIPPED
                    continue
                # The stat size refuses early; the bytes actually read are what count
                # (a file growing under the read cannot slip past the cap).
                if total + st.st_size > max_bytes:
                    raise RewindError(REWIND_TOO_LARGE,
                                      f"the undo backup exceeds {max_bytes} bytes")
                data = src.read(max_bytes - total + 1)
                total += len(data)
                if total > max_bytes:
                    raise RewindError(REWIND_TOO_LARGE,
                                      f"the undo backup exceeds {max_bytes} bytes")
            name = str(n)
            (blobs / name).write_bytes(data)
            entry.update(state=STATE_FILE, mode=stat.S_IMODE(st.st_mode), blob=name)
        (dest / _MANIFEST).write_text(json.dumps({"entries": entries}))
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    finally:
        for fd in roots.values():
            os.close(fd)
    log.info("rewind.backup_taken dir=%s files=%d bytes=%d", dest, len(entries), total)
    return entries


def restore_backup(dest: Path) -> list[str]:
    """Put every path in backup *dest* back: write a file's bytes and mode, delete a
    path recorded absent (a regular file only), leave a skipped one alone. Nothing is
    followed through a symlink; a path that cannot be restored that way is logged at
    INFO and left. Returns the paths restored (absolute)."""
    manifest = json.loads((dest / _MANIFEST).read_text())
    restored: list[str] = []
    roots: dict[str, int] = {}
    try:
        for e in manifest.get("entries", []):
            state, root, rel = e.get("state"), e.get("root"), e.get("rel")
            if state == STATE_SKIPPED or not isinstance(root, str) or not isinstance(rel, str):
                continue
            if root not in roots:
                roots[root] = _open_root(root)
            try:
                if state == STATE_FILE:
                    data = (dest / _BLOBS / str(e["blob"])).read_bytes()
                    replace_file_beneath(roots[root], rel, data, int(e.get("mode") or 0o644))
                    restored.append(e["path"])
                elif state == STATE_ABSENT:
                    if unlink_file_beneath(roots[root], rel):
                        restored.append(e["path"])
            except (OSError, ValueError) as exc:
                log.info("rewind.restore_skipped path=%s state=%s: %s", e.get("path"),
                         state, exc)
    finally:
        for fd in roots.values():
            os.close(fd)
    log.info("rewind.backup_restored dir=%s restored=%d", dest, len(restored))
    return restored


def drop_backup(dest: str | Path | None) -> None:
    """Delete one undo backup (it stopped being undoable). Missing is fine."""
    if dest:
        shutil.rmtree(dest, ignore_errors=True)


def drop_session_backups(session_id: str) -> None:
    """Delete every undo backup of *session_id* (archive)."""
    shutil.rmtree(paths.rewind_dir() / session_id, ignore_errors=True)
