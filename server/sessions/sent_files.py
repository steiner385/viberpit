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
"""
from __future__ import annotations

import logging
import os
from uuid import NAMESPACE_URL, uuid5

from server.sessions.artifacts import ImageReadError, ingest_image

log = logging.getLogger(__name__)

SEND_USER_FILE_TOOL = "SendUserFile"
# The ``uploaded_by`` of an ingested row: the session published it, not a person.
INGEST_IDENTITY = "session"
# One tool call names at most this many files that get read; the rest are refused.
MAX_SENT_FILES = 20

# ``SentFileError.code``: no main-thread ``SendUserFile`` call has that id.
TOOL_CALL_NOT_FOUND = "TOOL_CALL_NOT_FOUND"

# Per-file ``reason`` codes beyond ``artifacts.IMAGE_*``.
REASON_NO_CWD = "no_cwd"            # a relative path, and the session has no cwd
REASON_TOO_MANY = "too_many"        # past MAX_SENT_FILES
REASON_FAILED = "ingest_failed"     # the store write itself failed


class SentFileError(Exception):
    """The ingest refused the request (``code``: ``TOOL_CALL_NOT_FOUND``, a 404).

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
        tool_input = wire.get("input")
        files = tool_input.get("files") if isinstance(tool_input, dict) else None
        if not isinstance(files, list):
            return []
        # The web card's `sentFilesFromInput` filter, so both sides list the same files.
        return [f for f in files if isinstance(f, str) and f.strip()]
    return None


def resolve_sent_path(path: str, cwd: str | None) -> str | None:
    """*path* on the session host: absolute (``~`` expanded) as is, relative against
    the session's *cwd*; None for a relative path with no cwd."""
    expanded = os.path.expanduser(path)
    if os.path.isabs(expanded):
        return expanded
    return os.path.join(cwd, expanded) if cwd else None


def _entry(path: str, *, url: str | None = None, media_type: str | None = None,
           reason: str | None = None) -> dict:
    return {"path": path, "url": url, "mediaType": media_type, "reason": reason}


async def ingest_sent_files(events, tool_use_id: str, *, session_id: str,
                            cwd: str | None) -> dict:
    """``{files: [{path, url, mediaType, reason}]}`` for the ``SendUserFile`` call
    *tool_use_id* in *events* — one entry per named file, in order. Raises
    ``SentFileError(TOOL_CALL_NOT_FOUND)`` when the replay has no such call. Never
    raises per file: a refused or failed file is ``url: null`` with its reason,
    logged at INFO (an expected answer, not a defect)."""
    from server.db import db_session

    paths = sent_file_paths(events, tool_use_id)
    if paths is None:
        raise SentFileError(TOOL_CALL_NOT_FOUND)
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
