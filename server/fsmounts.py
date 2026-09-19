"""Which filesystem holds a path — the ONE mount-table reader in this repo.

Two subsystems need the same question answered and must answer it identically:

* ``server/codex/process.py`` refuses a ``CODEX_HOME`` on a network filesystem —
  Codex keeps SQLite/WAL, history and thread state there, and the 2026-09-04
  sshfs torn-read incident (#1005) is what a shared mount does to a file format
  that assumes local locking.
* ``server/obs/logdir.py`` refuses a JSON log directory on a shared mount —
  two hosts appending to (and independently rotating) ONE rotating log file
  over sshfs is the same hazard applied to a format that assumes one writer
  (#1010).

Deliberately dependency-light: stdlib only, no ``server.config``, no logging. The
policy — what to DO about a network answer — belongs to each caller, because the
right answer differs: Codex raises and refuses to start, logging degrades to
stdout-only. Only the *answer* is shared.
"""
from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import Final

#: Filesystem types that must never hold runtime state. Any ``fuse*`` type is
#: refused wholesale: sshfs (the mount that caused #1005) reports ``fuse.sshfs``,
#: and every other FUSE backend shares the same "locking and mtime are a polite
#: fiction" property that SQLite/WAL — and a rotating log file — cannot tolerate.
NETWORK_FSTYPES: Final[frozenset[str]] = frozenset(
    {
        "nfs",
        "nfs4",
        "cifs",
        "smbfs",
        "smb3",
        "sshfs",
        "afs",
        "9p",
        "ceph",
        "glusterfs",
        "lustre",
        "davfs",
        "beegfs",
        "gpfs",
        "ocfs2",
        "gfs2",
    }
)

Mounts = Sequence[tuple[str, str]]


def _unescape_mount_field(field: str) -> str:
    """Decode the octal escapes ``/proc/mounts`` uses for spaces and tabs."""
    out: list[str] = []
    index = 0
    while index < len(field):
        char = field[index]
        if char == "\\" and index + 3 < len(field) and field[index + 1: index + 4].isdigit():
            try:
                out.append(chr(int(field[index + 1: index + 4], 8)))
                index += 4
                continue
            except ValueError:  # pragma: no cover - isdigit already guarantees this
                pass
        out.append(char)
        index += 1
    return "".join(out)


def parse_proc_mounts(text: str) -> tuple[tuple[str, str], ...]:
    """Parse ``/proc/mounts`` text into ``(mount_point, fstype)`` pairs."""
    mounts: list[tuple[str, str]] = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        mounts.append((_unescape_mount_field(fields[1]), fields[2]))
    return tuple(mounts)


def read_mounts(path: str = "/proc/mounts") -> tuple[tuple[str, str], ...]:
    """Read the live mount table. Returns ``()`` when it cannot be read."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return parse_proc_mounts(handle.read())
    except OSError:
        return ()


def resolve_real_path(path: str | Path) -> str | None:
    """Absolute, symlink-free form of *path*; ``None`` when it cannot be resolved.

    ``os.path.abspath`` is purely *lexical*, so it answers "which mount holds this
    path" for the link rather than for the directory we would really write to:
    with ``/tmp/x/state -> /mnt/shared/state``, an abspath match reports the local
    root filesystem for a path that lands on sshfs. ``realpath`` is non-strict, so
    it resolves the components that exist and appends the not-yet-created tail
    verbatim — which is what lets the refusal run *before* the directory is made.
    """
    try:
        return os.path.realpath(os.path.expanduser(str(path)))
    except OSError:
        return None


def first_symlinked_component(path: str | Path) -> Path | None:
    """The outermost component of *path* that is a symlink, or ``None``.

    Whole-path, not just the leaf: an ancestor link redirects the target exactly
    as effectively as a link on the target itself.
    """
    absolute = Path(os.path.abspath(os.path.expanduser(str(path))))
    current = Path(absolute.anchor or os.sep)
    for part in absolute.parts[1:]:
        current = current / part
        try:
            if current.is_symlink():
                return current
        except OSError:  # pragma: no cover - an unreadable component is not provably real
            return current
    return None


def filesystem_type_for(path: str | Path, *, mounts: Mounts | None = None) -> str | None:
    """The fstype of the mount that holds *path*, by longest mount-point match.

    The path need not exist yet — this is a mount-table question, so it is
    answerable before the directory is created (which is what lets the caller
    refuse *before* writing anything). Symlinks are resolved first, so the answer
    describes the filesystem the bytes would land on.
    """
    table = read_mounts() if mounts is None else tuple(mounts)
    if not table:
        return None
    target = resolve_real_path(path)
    if target is None:
        return None
    best: tuple[int, str] | None = None
    for point, fstype in table:
        normalized = os.path.abspath(point)
        if target == normalized or target.startswith(
            normalized if normalized.endswith(os.sep) else normalized + os.sep
        ):
            if best is None or len(normalized) > best[0]:
                best = (len(normalized), fstype)
    return best[1] if best is not None else None


def is_network_fstype(fstype: str) -> bool:
    lowered = fstype.lower()
    return (
        lowered in NETWORK_FSTYPES
        or lowered.startswith("fuse")
        or lowered.startswith("nfs")
    )
