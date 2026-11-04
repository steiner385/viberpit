"""Settings › Claude config › Memory (D-13): read and edit the user-level CLAUDE.md.

The one file is ``shared_base()/CLAUDE.md`` (``~/.claude/CLAUDE.md``) on the edge host,
which owns ``~/.claude``; every account's ``$CLAUDE_CONFIG_DIR/CLAUDE.md`` links to it
(Task 1) and a worker host sees it over sshfs. So a save is one write, and no account
or host can drift.

- ``GET`` returns ``{text, etag, exists}``; ``PUT`` takes ``{text, etag}`` and returns
  ``{etag}``. The etag is the sha256 of the bytes on disk, ``""`` when there is no file,
  so a save over a file another tab changed is 409 ``MEMORY_CONFLICT`` carrying the
  current text, and nothing is lost.
- The write is atomic and durable: a temp file in the same directory, fsynced, renamed
  over the target, mode 0644 (``viberpit.fs_beneath.replace_file_beneath``). A reader —
  a session starting on any host — sees the old file or the new one, never half.
- A symlinked target is not ours to write through or replace: 409 ``MEMORY_FILE_IS_LINK``.
- Over ``MAX_MEMORY_BYTES`` is 413 ``MEMORY_TOO_LARGE``.
- Auth is the normal identity, as for every settings and library write, and each save
  writes an audit row. The config auto-snapshot (which covers CLAUDE.md) runs first.
"""
from __future__ import annotations

import asyncio
import errno
import hashlib
import logging
import os
import stat
import threading
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from server.audit import record_audit
from server.auth.middleware import current_identity
from server.db import get_db
from server.library.snapshots import snapshot_before_mutation
from server.sessions.memory_files import MAX_MEMORY_BYTES, shared_user_memory_path
from viberpit.fs_beneath import SymlinkInPath, open_beneath, replace_file_beneath

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings/memory", tags=["settings"])

MEMORY_CONFLICT = "MEMORY_CONFLICT"
MEMORY_FILE_IS_LINK = "MEMORY_FILE_IS_LINK"
MEMORY_TOO_LARGE = "MEMORY_TOO_LARGE"
FILE_MODE = 0o644

# Serializes check-then-write within the edge process (the only writer).
_write_lock = threading.Lock()


class MemoryEditError(Exception):
    def __init__(self, code: str, current: tuple[str, str] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.current = current  # (text, etag) for a conflict


def etag_of(data: bytes | None) -> str:
    return "" if data is None else hashlib.sha256(data).hexdigest()


def _base_fd() -> int:
    base = shared_user_memory_path().parent
    base.mkdir(parents=True, exist_ok=True)
    return os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)


def _read_current(dfd: int, name: str) -> bytes | None:
    """The target's bytes, or None when it does not exist. A symlink or any
    non-regular file raises ``MEMORY_FILE_IS_LINK``; over the cap ``MEMORY_TOO_LARGE``
    (a truncated read must never be offered for editing)."""
    try:
        fd = open_beneath(dfd, name)
    except SymlinkInPath as exc:
        raise MemoryEditError(MEMORY_FILE_IS_LINK) from exc
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ENOTDIR:
            return None
        raise
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise MemoryEditError(MEMORY_FILE_IS_LINK)
        data = fh.read(MAX_MEMORY_BYTES + 1)
    if len(data) > MAX_MEMORY_BYTES:
        raise MemoryEditError(MEMORY_TOO_LARGE)
    return data


def read_user_memory() -> dict[str, Any]:
    """``{text, etag, exists}`` of the shared user memory file."""
    name = shared_user_memory_path().name
    dfd = _base_fd()
    try:
        data = _read_current(dfd, name)
    finally:
        os.close(dfd)
    return {"text": "" if data is None else data.decode("utf-8", "replace"),
            "etag": etag_of(data), "exists": data is not None}


def write_user_memory(text: str, etag: str) -> dict[str, Any]:
    """Replace the shared user memory file with ``text`` if its current etag is
    ``etag``; returns ``{etag, created, bytes}``. Raises :class:`MemoryEditError`."""
    data = text.encode("utf-8")
    if len(data) > MAX_MEMORY_BYTES:
        raise MemoryEditError(MEMORY_TOO_LARGE)
    name = shared_user_memory_path().name
    with _write_lock:
        dfd = _base_fd()
        try:
            current = _read_current(dfd, name)
            if etag_of(current) != etag:
                raise MemoryEditError(MEMORY_CONFLICT, (
                    "" if current is None else current.decode("utf-8", "replace"),
                    etag_of(current)))
            snapshot_before_mutation()  # best-effort; swallows its own errors
            replace_file_beneath(dfd, name, data, FILE_MODE, fsync=True)
        finally:
            os.close(dfd)
    return {"etag": etag_of(data), "created": current is None, "bytes": len(data)}


def _http(err: MemoryEditError) -> HTTPException:
    if err.code == MEMORY_TOO_LARGE:
        return HTTPException(status_code=413, detail={"code": MEMORY_TOO_LARGE})
    detail: dict[str, Any] = {"code": err.code}
    if err.current is not None:
        detail["text"], detail["etag"] = err.current
    log.info("settings.memory refused code=%s", err.code)
    return HTTPException(status_code=409, detail=detail)


class MemoryBody(BaseModel):
    text: str
    etag: str


@router.get("")
async def get_user_memory(_identity: str = Depends(current_identity)) -> dict:
    try:
        return await asyncio.to_thread(read_user_memory)
    except MemoryEditError as err:
        raise _http(err) from err


@router.put("")
async def put_user_memory(body: MemoryBody, identity: str = Depends(current_identity),
                          db=Depends(get_db)) -> dict:
    if len(body.text.encode("utf-8")) > MAX_MEMORY_BYTES:
        raise _http(MemoryEditError(MEMORY_TOO_LARGE))
    try:
        out = await asyncio.to_thread(write_user_memory, body.text, body.etag)
    except MemoryEditError as err:
        raise _http(err) from err
    await record_audit(db=db, type="settings.memory_saved", identity=identity,
                       detail={"bytes": out["bytes"], "created": out["created"]})
    log.info("settings.memory saved bytes=%d created=%s", out["bytes"], out["created"])
    return {"etag": out["etag"]}
