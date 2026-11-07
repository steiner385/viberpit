"""Carry one Codex thread's history between fleet hosts (D-17 Codex move, #3347).

Claude's transcript lives on a filesystem both hosts see, so moving a Claude session
only has to *read* it on the target. Codex keeps a thread's whole history in ONE
file under a ``CODEX_HOME`` that must be local and private
(``sessions/YYYY/MM/DD/rollout-<ts>-<threadId>.jsonl``), so a move needs a byte
copy: the source daemon exports the rollout plus ViberPit's per-thread sidecars
(``codex-item-times/<thread>.jsonl`` and ``<thread>.turns.jsonl``), the manifest
carries them base64-inline, and the target writes them into its own ``CODEX_HOME``
before resuming by thread id — ``thread/resume`` takes no path, and copying the
file to the same relative path is enough (verified on codex-cli 0.153.2 in
``docs/superpowers/specs/2026-11-06-codex-session-move-verification.md``).

Rulings (from the plan, binding):

* **Never overwrite.** A rollout for the thread id already under the target's home
  refuses the import (``"exists"``); a sidecar already present is left untouched.
* **Retire, never delete.** Finalize renames the source rollout to
  ``<name>.moved-to-<owner>`` so nothing on the source can resume it while the bytes
  stay recoverable; an earlier retirement is never clobbered either.
* **Cap.** ``HISTORY_MAX_BYTES`` of raw bytes (rollout + sidecars); over it is
  ``"too_large"`` before anything is touched.
* Thread ids pass ``item_times.safe_thread_id`` (the one id rule) before any glob;
  ``rel_path`` is matched against the dated layout before any write; symlinks are
  never followed (``O_NOFOLLOW``); writes go temp file → fsync → ``os.replace`` at
  mode 0600 under directories created 0700.

Pure file logic: no orchestrator, DB or asyncio here. Refusals are exceptions the
saga maps to ``MoveError`` reasons; logging stays at INFO/DEBUG (a WARNING in this
repo auto-files an issue).
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import os
import re
import stat
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from server.codex.item_times import safe_thread_id, thread_sidecar_path

__all__ = [
    "HISTORY_MAX_BYTES",
    "Sidecar",
    "ThreadBundle",
    "ThreadMoveError",
    "export_thread",
    "find_rollout",
    "import_thread",
    "inspect",
    "remove_imported",
    "retire_rollout",
    "safe_thread_id",
    "verify_thread",
]

log = logging.getLogger("server.codex.thread_move")

HISTORY_MAX_BYTES = 128 * 1024 * 1024

# The two ViberPit-owned sidecars that must travel with a thread (replay timestamps
# and turn history); both are named by ``item_times.thread_sidecar_path``.
_SIDECAR_SUFFIXES = (".jsonl", ".turns.jsonl")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_OWNER_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]")
_RETIRE_ATTEMPTS = 1000


class ThreadMoveError(Exception):
    """A refusal or failure of one move step. ``code`` is one of ``not_found``,
    ``ambiguous``, ``too_large``, ``exists``, ``bad_bundle``, ``io``."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Sidecar:
    name: str
    size: int
    sha256: str
    data: str | None

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "size": self.size, "sha256": self.sha256, "data": self.data}


@dataclass(frozen=True)
class ThreadBundle:
    """One thread's history as the manifest carries it. ``data`` fields are base64
    of the raw bytes, or None once ``stripped()`` for storage (descriptor only)."""

    thread_id: str
    rel_path: str
    size: int
    sha256: str
    data: str | None
    sidecars: tuple[Sidecar, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "threadId": self.thread_id,
            "relPath": self.rel_path,
            "size": self.size,
            "sha256": self.sha256,
            "data": self.data,
            "sidecars": [s.to_dict() for s in self.sidecars],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ThreadBundle":
        if not isinstance(d, dict):
            raise ThreadMoveError("bad_bundle", "bundle is not an object")
        raw_sidecars = d.get("sidecars")
        if not isinstance(raw_sidecars, list):
            raise ThreadMoveError("bad_bundle", "sidecars is not a list")
        sidecars = []
        for raw in raw_sidecars:
            if not isinstance(raw, dict):
                raise ThreadMoveError("bad_bundle", "sidecar is not an object")
            sidecars.append(Sidecar(
                name=_str_field(raw, "name"),
                size=_size_field(raw, "size"),
                sha256=_sha_field(raw, "sha256"),
                data=_data_field(raw, "data"),
            ))
        return cls(
            thread_id=_str_field(d, "threadId"),
            rel_path=_str_field(d, "relPath"),
            size=_size_field(d, "size"),
            sha256=_sha_field(d, "sha256"),
            data=_data_field(d, "data"),
            sidecars=tuple(sidecars),
        )

    def stripped(self) -> "ThreadBundle":
        """The descriptor without any bytes — what ``move_manifest`` stores and what a
        reconcile verifies a holder against."""
        return replace(self, data=None, sidecars=tuple(replace(s, data=None) for s in self.sidecars))


def _str_field(d: dict, key: str) -> str:
    v = d.get(key)
    if not isinstance(v, str):
        raise ThreadMoveError("bad_bundle", f"{key} is not a string")
    return v


def _size_field(d: dict, key: str) -> int:
    v = d.get(key)
    if not isinstance(v, int) or isinstance(v, bool) or v < 0:
        raise ThreadMoveError("bad_bundle", f"{key} is not a non-negative integer")
    return v


def _sha_field(d: dict, key: str) -> str:
    v = _str_field(d, key)
    if not _SHA256_RE.match(v):
        raise ThreadMoveError("bad_bundle", f"{key} is not a sha256 hex digest")
    return v


def _data_field(d: dict, key: str) -> str | None:
    if key not in d:
        raise ThreadMoveError("bad_bundle", f"{key} is missing (null once stripped)")
    v = d[key]
    if v is not None and not isinstance(v, str):
        raise ThreadMoveError("bad_bundle", f"{key} is neither base64 nor null")
    return v


# --- locating ----------------------------------------------------------------


def _rel_path_re(thread_id: str) -> re.Pattern[str]:
    # thread_id has passed safe_thread_id, so re.escape is belt and braces.
    return re.compile(r"^sessions/\d{4}/\d{2}/\d{2}/rollout-[A-Za-z0-9T:_-]+-"
                      + re.escape(thread_id) + r"\.jsonl$")


def _is_regular(path: Path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def find_rollout(home: Path, thread_id: str) -> Path | None:
    """The thread's rollout under *home* (``sessions/YYYY/MM/DD/rollout-<ts>-<id>.jsonl``),
    None when there is none. Only a regular, non-symlink file at the documented layout
    counts; two or more is ``ThreadMoveError("ambiguous")``. An unsafe id answers
    None without touching the filesystem."""
    if not safe_thread_id(thread_id):
        return None
    pattern = _rel_path_re(thread_id)
    matches = [
        p for p in (Path(home) / "sessions").glob(f"*/*/*/rollout-*-{thread_id}.jsonl")
        if _is_regular(p) and pattern.match(p.relative_to(home).as_posix())
    ]
    if len(matches) > 1:
        raise ThreadMoveError(
            "ambiguous", f"{len(matches)} rollouts for thread {thread_id} under {home}")
    return matches[0] if matches else None


def _sidecar_paths(sidecar_root: Path | None, thread_id: str) -> list[Path]:
    """The sidecars that exist for *thread_id* (regular files only), in suffix order."""
    if sidecar_root is None:
        return []
    found = []
    for suffix in _SIDECAR_SUFFIXES:
        p = thread_sidecar_path(Path(sidecar_root), thread_id, suffix)
        if p is not None and _is_regular(p):
            found.append(p)
    return found


def inspect(home: Path, sidecar_root: Path | None, thread_id: str) -> int:
    """Total raw bytes an export would carry (rollout + existing sidecars); 0 when the
    thread has no rollout. The predicate compares this with ``HISTORY_MAX_BYTES``."""
    rollout = find_rollout(home, thread_id)
    if rollout is None:
        return 0
    return _size(rollout) + sum(_size(p) for p in _sidecar_paths(sidecar_root, thread_id))


def _size(path: Path) -> int:
    try:
        return os.lstat(path).st_size
    except OSError:
        return 0


# --- reading / writing -------------------------------------------------------


def _read_private(path: Path) -> bytes:
    """Read *path* to EOF without following a symlink; raises OSError."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{path} is not a regular file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(fd)


def _write_private(path: Path, data: bytes) -> None:
    """Create *path* with *data* at mode 0600: temp file in the same directory
    (``O_CREAT|O_EXCL|O_NOFOLLOW``), fsync, ``os.replace``. Missing parents are created
    0700. Raises OSError; never leaves the temp file behind."""
    missing: list[Path] = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for d in reversed(missing):
        d.mkdir(mode=0o700)
        os.chmod(d, 0o700)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(data)
            while view:
                n = os.write(fd, view)
                view = view[n:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dfd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _encode(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _decode(data: str, size: int, sha256: str, what: str) -> bytes:
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ThreadMoveError("bad_bundle", f"{what}: data is not base64 ({exc})") from exc
    if len(raw) != size:
        raise ThreadMoveError("bad_bundle", f"{what}: {len(raw)} bytes, descriptor says {size}")
    if hashlib.sha256(raw).hexdigest() != sha256:
        raise ThreadMoveError("bad_bundle", f"{what}: sha256 does not match the descriptor")
    return raw


# --- export / import / verify ------------------------------------------------


def export_thread(home: Path, sidecar_root: Path | None, thread_id: str, *,
                  max_bytes: int = HISTORY_MAX_BYTES) -> ThreadBundle:
    """Read the thread's rollout and existing sidecars into a bundle. ``not_found``
    without a rollout, ``too_large`` over *max_bytes* (checked on sizes before any
    read and again on the bytes read), ``io`` on a read failure."""
    rollout = find_rollout(home, thread_id)
    if rollout is None:
        raise ThreadMoveError("not_found", f"no rollout for thread {thread_id} under {home}")
    sidecar_paths = _sidecar_paths(sidecar_root, thread_id)
    total = _size(rollout) + sum(_size(p) for p in sidecar_paths)
    if total > max_bytes:
        raise ThreadMoveError("too_large", f"{total} bytes of history over the {max_bytes} byte cap")
    try:
        raw = _read_private(rollout)
        sidecars = []
        for p in sidecar_paths:
            body = _read_private(p)
            sidecars.append(Sidecar(name=p.name, size=len(body),
                                    sha256=hashlib.sha256(body).hexdigest(), data=_encode(body)))
    except OSError as exc:
        raise ThreadMoveError("io", f"reading thread {thread_id}: {exc}") from exc
    total = len(raw) + sum(s.size for s in sidecars)
    if total > max_bytes:
        raise ThreadMoveError("too_large", f"{total} bytes of history over the {max_bytes} byte cap")
    bundle = ThreadBundle(
        thread_id=thread_id,
        rel_path=rollout.relative_to(home).as_posix(),
        size=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
        data=_encode(raw),
        sidecars=tuple(sidecars),
    )
    log.info("codex.thread_move: exported thread %s (%d bytes, %d sidecars) from %s",
             thread_id, total, len(sidecars), home)
    return bundle


def _rel_path_ok(bundle: ThreadBundle) -> bool:
    return (safe_thread_id(bundle.thread_id)
            and _rel_path_re(bundle.thread_id).match(bundle.rel_path) is not None)


def _check_bundle(bundle: ThreadBundle) -> tuple[bytes, list[tuple[str, bytes]]]:
    """Decode and validate every byte of *bundle* before anything is written."""
    if not safe_thread_id(bundle.thread_id):
        raise ThreadMoveError("bad_bundle", "thread id is not a plain token")
    if not _rel_path_ok(bundle):
        raise ThreadMoveError("bad_bundle", f"relPath {bundle.rel_path!r} is not a dated rollout path")
    if bundle.data is None:
        raise ThreadMoveError("bad_bundle", "bundle is stripped (no rollout bytes)")
    raw = _decode(bundle.data, bundle.size, bundle.sha256, "rollout")
    allowed = {f"{bundle.thread_id}{suffix}" for suffix in _SIDECAR_SUFFIXES}
    sidecars = []
    for s in bundle.sidecars:
        if s.name not in allowed:
            raise ThreadMoveError("bad_bundle", f"sidecar {s.name!r} is not one of this thread's")
        if s.data is None:
            raise ThreadMoveError("bad_bundle", f"sidecar {s.name!r} is stripped")
        sidecars.append((s.name, _decode(s.data, s.size, s.sha256, f"sidecar {s.name}")))
    return raw, sidecars


def import_thread(home: Path, sidecar_root: Path | None, bundle: ThreadBundle) -> list[Path]:
    """Write the bundle under *home* (rollout at its ``rel_path``) and *sidecar_root*
    (each sidecar only when absent). Returns the paths it wrote, rollout first.
    ``bad_bundle`` for anything malformed, ``exists`` when the thread is already here,
    ``io`` on a write failure (after removing what it wrote). Nothing is written until
    every check has passed."""
    raw, sidecars = _check_bundle(bundle)
    home = Path(home)
    try:
        present = find_rollout(home, bundle.thread_id)
    except ThreadMoveError as exc:        # ambiguous: more than one is still "exists"
        raise ThreadMoveError("exists", exc.detail) from exc
    target = home / bundle.rel_path
    if present is not None or os.path.lexists(target):
        raise ThreadMoveError(
            "exists", f"thread {bundle.thread_id} already has a rollout under {home}")
    written: list[Path] = []
    try:
        _write_private(target, raw)
        written.append(target)
        if sidecar_root is not None:
            for name, body in sidecars:
                dest = Path(sidecar_root) / name
                if os.path.lexists(dest):
                    log.debug("codex.thread_move: sidecar %s already present, left untouched", dest)
                    continue
                _write_private(dest, body)
                written.append(dest)
    except OSError as exc:
        remove_imported(written)
        raise ThreadMoveError("io", f"writing thread {bundle.thread_id} under {home}: {exc}") from exc
    log.info("codex.thread_move: imported thread %s (%d bytes, %d files) under %s",
             bundle.thread_id, bundle.size, len(written), home)
    return written


def verify_thread(home: Path, bundle: ThreadBundle) -> bool:
    """True when the rollout at ``bundle.rel_path`` under *home*, read to EOF, has
    exactly ``bundle.size`` bytes and ``bundle.sha256``. False when missing, a
    symlink, or when the bundle's path is not a dated rollout path."""
    if not _rel_path_ok(bundle):
        return False
    try:
        raw = _read_private(Path(home) / bundle.rel_path)
    except OSError:
        return False
    return len(raw) == bundle.size and hashlib.sha256(raw).hexdigest() == bundle.sha256


# --- retire / abandon --------------------------------------------------------


def retire_rollout(home: Path, thread_id: str, owner: str) -> Path | None:
    """Rename the source rollout to ``<name>.moved-to-<owner>`` so the source can no
    longer resume it while the bytes stay recoverable. Returns the new path, None
    when the thread has no rollout; never clobbers an earlier retirement."""
    rollout = find_rollout(home, thread_id)
    if rollout is None:
        return None
    safe_owner = _OWNER_UNSAFE_RE.sub("_", owner) or "unknown"
    base = f"{rollout.name}.moved-to-{safe_owner}"
    candidates = [base] + [f"{base}.{n}" for n in range(1, _RETIRE_ATTEMPTS)]
    for name in candidates:
        dest = rollout.with_name(name)
        if os.path.lexists(dest):
            continue
        try:
            os.rename(rollout, dest)
        except OSError as exc:
            raise ThreadMoveError("io", f"retiring thread {thread_id}: {exc}") from exc
        log.info("codex.thread_move: retired thread %s to %s", thread_id, dest.name)
        return dest
    raise ThreadMoveError("io", f"retiring thread {thread_id}: every retirement name is taken")


def remove_imported(paths: list[Path]) -> None:
    """Abandon an import: unlink exactly *paths* (what ``import_thread`` returned).
    Never raises."""
    for p in paths:
        try:
            os.unlink(p)
        except OSError as exc:
            log.debug("codex.thread_move: could not remove %s: %s", p, exc)
