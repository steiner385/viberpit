"""`SendUserFile` ingest (#3487): make the files a session sent the user viewable.

Claude Code's built-in ``SendUserFile`` tool uploads its files to claude.ai and tells
the agent "N files delivered" — but a ViberPit user is not on claude.ai, and agents
usually write those files under ``/tmp`` or ``~/.claude/jobs``, outside the worktree
the browser can read. So the card asks the daemon that owns the session to ingest
them: each raster image goes into the content-addressed attachment store exactly as
`share_artifact` kind ``image`` does (``artifacts.ingest_image``), and the card
renders ``/api/attachments/{id}``.

The paths come ONLY from the session's own transcript — the main-thread
``SendUserFile`` call with that tool-use id — never from the request: a browser can
name a tool call, not a file. Anything that is not a readable png/jpeg/gif/webp
within the attachment cap answers ``url: null`` with a short ``reason``.

Idempotent: each file's row id is a uuid5 of (session, tool call, index), so a repeat
request returns the saved row without re-reading the file.

The call is found cheaply (``send_user_file_calls``): the main JSONL is streamed and
only lines that mention ``SendUserFile`` are parsed — never a full replay with its
sub-agent sweep — and concurrent lookups of one transcript share one scan, cached
until the file changes. A scan that cannot finish in time is ``HISTORY_UNAVAILABLE``
(503, retryable), never "no such call" (404).
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
from collections import OrderedDict
from uuid import NAMESPACE_URL, uuid5

from server.sessions.artifacts import ImageReadError, ingest_image

log = logging.getLogger(__name__)

SEND_USER_FILE_TOOL = "SendUserFile"
# The ``uploaded_by`` of an ingested row: the session published it, not a person.
INGEST_IDENTITY = "session"
# One tool call names at most this many files that get read; the rest are refused.
MAX_SENT_FILES = 20

# ``SentFileError.code``: no main-thread ``SendUserFile`` call has that id (404).
TOOL_CALL_NOT_FOUND = "TOOL_CALL_NOT_FOUND"
# ...or the history could not be read in time / at all (503, the client retries).
HISTORY_UNAVAILABLE = "SENT_FILES_HISTORY_UNAVAILABLE"
SENT_FILE_ERROR_CODES = (TOOL_CALL_NOT_FOUND, HISTORY_UNAVAILABLE)

# A transcript scan longer than this answers HISTORY_UNAVAILABLE; the scan itself
# keeps running and the retry joins it.
SCAN_CEILING_S = 10.0
# Transcripts whose SendUserFile index is kept (LRU), keyed by path.
_INDEX_CACHE_MAX = 64

# Per-file ``reason`` codes beyond ``artifacts.IMAGE_*``.
REASON_NO_CWD = "no_cwd"            # a relative path, and the session has no cwd
REASON_TOO_MANY = "too_many"        # past MAX_SENT_FILES
REASON_FAILED = "ingest_failed"     # the store write itself failed


class SentFileError(Exception):
    """The ingest refused the request: ``code`` is ``TOOL_CALL_NOT_FOUND`` (404) or
    ``HISTORY_UNAVAILABLE`` (503).

    ``error_data`` rides the daemon's RPC envelope so a remote edge rebuilds it
    (``RemoteOrchestrator.sent_files``) and the daemon logs it at INFO."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
        self.error_data = {"code": code}


def sent_file_paths(events, tool_use_id: str) -> list[str] | None:
    """The ``files`` of the main-thread ``SendUserFile`` call *tool_use_id* in a
    replay (event objects or wire dicts), as written; None when there is no such
    call — a sub-agent's call, another tool's id, or an unknown id."""
    for event in events:
        wire = event.to_wire() if hasattr(event, "to_wire") else event
        if not isinstance(wire, dict) or wire.get("type") != "toolCall":
            continue
        if (wire.get("toolId") != tool_use_id or wire.get("name") != SEND_USER_FILE_TOOL
                or wire.get("parentToolUseId") is not None):
            continue
        return _files_of(wire.get("input"))
    return None


def _files_of(tool_input) -> list[str]:
    files = tool_input.get("files") if isinstance(tool_input, dict) else None
    if not isinstance(files, list):
        return []
    # The web card's `sentFilesFromInput` filter, so both sides list the same files.
    return [f for f in files if isinstance(f, str) and f.strip()]


def scan_send_user_file_calls(path: str) -> dict[str, list[str]]:
    """``{tool_use_id: files}`` of every main-thread ``SendUserFile`` call in the
    transcript JSONL at *path*. Only lines mentioning the tool name are parsed, and
    sidechain (Layout-A sub-agent) entries are skipped; Layout-B sub-agents live in
    sidecar files this never opens. Raises ``OSError`` for an unreadable file.

    Unlike the replay it does not apply a rewind's cut: a call a rewind dropped
    still resolves, which only lets its card keep showing what it sent."""
    marker = SEND_USER_FILE_TOOL.encode()
    calls: dict[str, list[str]] = {}
    with open(path, "rb") as f:
        for raw in f:
            if marker not in raw:
                continue
            try:
                o = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(o, dict) or o.get("isSidechain") or o.get("type") != "assistant":
                continue
            content = (o.get("message") or {}).get("content")
            for b in content if isinstance(content, list) else []:
                if (isinstance(b, dict) and b.get("type") == "tool_use"
                        and b.get("name") == SEND_USER_FILE_TOOL
                        and isinstance(b.get("id"), str)):
                    calls[b["id"]] = _files_of(b.get("input"))
    return calls


# path -> ((mtime_ns, size), index); and path -> the scan in flight for it.
_index_cache: OrderedDict[str, tuple[tuple[int, int], dict[str, list[str]]]] = OrderedDict()
_inflight: dict[str, asyncio.Task] = {}


def _clear_index_cache() -> None:
    """Drop the cached indexes (tests)."""
    _index_cache.clear()


async def send_user_file_calls(path: str, *,
                               ceiling_s: float = SCAN_CEILING_S) -> dict[str, list[str]]:
    """``scan_send_user_file_calls(path)`` off the loop, deduplicated and cached.

    Concurrent callers for one transcript await ONE scan (several cards mount
    together); its index is reused until the file's mtime or size changes. Raises
    ``SentFileError(HISTORY_UNAVAILABLE)`` past *ceiling_s* or for an unreadable
    file — the scan keeps running, so a retry joins it rather than starting over."""
    try:
        st = await asyncio.to_thread(os.stat, path)
    except OSError as exc:
        log.info("session.sent_files transcript unreadable: %s", type(exc).__name__)
        raise SentFileError(HISTORY_UNAVAILABLE) from exc
    key = (st.st_mtime_ns, st.st_size)
    hit = _index_cache.get(path)
    if hit is not None and hit[0] == key:
        _index_cache.move_to_end(path)
        return hit[1]
    task = _inflight.get(path)
    if task is None:
        task = asyncio.create_task(_scan_and_cache(path, key))
        _inflight[path] = task
        task.add_done_callback(functools.partial(_scan_done, path))
    try:
        # shield: one caller timing out (or being cancelled) never stops the scan
        # the others — and its own retry — are waiting on.
        return await asyncio.wait_for(asyncio.shield(task), timeout=ceiling_s)
    except TimeoutError as exc:
        log.info("session.sent_files transcript scan exceeded %.0fs", ceiling_s)
        raise SentFileError(HISTORY_UNAVAILABLE) from exc
    except OSError as exc:
        log.info("session.sent_files transcript unreadable: %s", type(exc).__name__)
        raise SentFileError(HISTORY_UNAVAILABLE) from exc


def _scan_done(path: str, task: asyncio.Task) -> None:
    _inflight.pop(path, None)
    if not task.cancelled():
        task.exception()  # retrieved: every waiter may have timed out already


async def _scan_and_cache(path: str, key: tuple[int, int]) -> dict[str, list[str]]:
    index = await asyncio.to_thread(scan_send_user_file_calls, path)
    _index_cache[path] = (key, index)
    _index_cache.move_to_end(path)
    while len(_index_cache) > _INDEX_CACHE_MAX:
        _index_cache.popitem(last=False)
    return index


def resolve_sent_path(path: str, cwd: str | None) -> str | None:
    """*path* on the session host: absolute (``~`` expanded) as is, relative against
    the session's *cwd*; None for a relative path with no cwd.

    *cwd* is the session's cwd NOW, not at call time: the CLI resolved a relative
    path against the cwd the call ran in, so a session that has since moved (a
    ``cd`` the CLI persisted, a move to another worktree) can resolve it elsewhere
    — to a missing file at worst, which is then ``not_found``."""
    expanded = os.path.expanduser(path)
    if os.path.isabs(expanded):
        return expanded
    return os.path.join(cwd, expanded) if cwd else None


def _entry(path: str, *, url: str | None = None, media_type: str | None = None,
           reason: str | None = None) -> dict:
    return {"path": path, "url": url, "mediaType": media_type, "reason": reason}


async def ingest_sent_files(paths: list[str], tool_use_id: str, *, session_id: str,
                            cwd: str | None) -> dict:
    """``{files: [{path, url, mediaType, reason}]}`` for the *paths* the
    ``SendUserFile`` call *tool_use_id* named — one entry per file, in order. Never
    raises per file: a refused or failed file is ``url: null`` with its reason,
    logged at INFO (an expected answer, not a defect)."""
    from server.db import db_session

    out: list[dict] = []
    for index, path in enumerate(paths):
        if index >= MAX_SENT_FILES:
            out.append(_entry(path, reason=REASON_TOO_MANY))
            continue
        full = resolve_sent_path(path, cwd)
        if full is None:
            out.append(_entry(path, reason=REASON_NO_CWD))
            continue
        aid = str(uuid5(NAMESPACE_URL,
                        f"viberpit:sent-file:{session_id}:{tool_use_id}:{index}"))
        try:
            async with db_session() as db:
                row = await ingest_image(db, full, session_id=session_id,
                                         identity=INGEST_IDENTITY, attachment_id=aid)
                url, media_type = f"/api/attachments/{row.id}", row.media_type
        except ImageReadError as exc:
            log.info("session.sent_files id=%s tool=%s file %d refused: %s",
                     session_id, tool_use_id, index, exc.code)
            out.append(_entry(path, reason=exc.code))
            continue
        except Exception as exc:  # noqa: BLE001 - one file's failure is that file's answer
            log.info("session.sent_files id=%s tool=%s file %d failed: %s",
                     session_id, tool_use_id, index, type(exc).__name__)
            out.append(_entry(path, reason=REASON_FAILED))
            continue
        out.append(_entry(path, url=url, media_type=media_type))
    return {"files": out}
