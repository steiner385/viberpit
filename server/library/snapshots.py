"""Config snapshot / export / import / diff for the shared library layer.

A snapshot is a tar.gz of the non-excluded files under ``paths.shared_base()``
plus a JSON sidecar of metadata, stored under ``paths.root()/"snapshots"``.

SECURITY (constitution II/IV): credential- and identity-bearing files are
NEVER captured in a snapshot/export NOR written during restore/import. The
single ``is_excluded`` predicate gates BOTH tar creation and extraction, so a
poisoned bundle cannot smuggle a credentials file onto disk, and a snapshot can
never leak one. Extraction is also guarded against tar-slip (path traversal).
"""
from __future__ import annotations

import io
import json
import os
import tarfile
import tempfile
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ccswitch import paths

# Top-level path segments that are heavy/ephemeral and never captured.
_EXCLUDED_TOP_DIRS = {
    "projects",
    "todos",
    "shell-snapshots",
    "statsig",
    "history",
}
# Two-segment prefixes excluded wholesale (kept siblings must not match).
_EXCLUDED_DIR_PREFIXES = (
    ("plugins", "cache"),
)
# Auto-snapshot throttle: collapse a burst of mutations into one snapshot.
_AUTO_THROTTLE_S = 5.0


def is_excluded(rel_posix_path: str) -> bool:
    """True if this relative POSIX path must never be captured or restored.

    Excludes credential/identity files (by basename, case-insensitive) and
    heavy/ephemeral directories (by leading path segments), matching on segments
    so ``plugins/cache/...`` is excluded but ``plugins/installed_plugins.json``
    is kept.
    """
    segments = [s for s in rel_posix_path.split("/") if s and s != "."]
    if not segments:
        return True
    basename = segments[-1].lower()
    if basename == ".credentials.json" or basename.endswith(".credentials.json"):
        return True
    if basename in (".credentials", ".claude.json"):
        return True
    if segments[0] in _EXCLUDED_TOP_DIRS:
        return True
    for prefix in _EXCLUDED_DIR_PREFIXES:
        if tuple(segments[: len(prefix)]) == prefix:
            return True
    return False


def snapshots_dir() -> Path:
    d = paths.root() / "snapshots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _iter_files(base: Path) -> Iterator[tuple[Path, str]]:
    """Yield (abs_path, rel_posix) for every non-excluded file under base."""
    if not base.is_dir():
        return
    for abs_path in base.rglob("*"):
        if not abs_path.is_file():
            continue
        rel = abs_path.relative_to(base).as_posix()
        if is_excluded(rel):
            continue
        yield abs_path, rel


def _write_tar_gz(dest: Path, base: Path) -> tuple[int, int]:
    """Tar.gz the non-excluded files of base into dest. Return (fileCount, sizeBytes)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    file_count = 0
    with tarfile.open(dest, "w:gz") as tar:
        for abs_path, rel in _iter_files(base):
            tar.add(abs_path, arcname=rel)
            file_count += 1
    return file_count, dest.stat().st_size


def _tar_gz_bytes(base: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for abs_path, rel in _iter_files(base):
            tar.add(abs_path, arcname=rel)
    return buf.getvalue()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _meta_path(snapshot_id: str) -> Path:
    return snapshots_dir() / f"{snapshot_id}.json"


def _tar_path(snapshot_id: str) -> Path:
    return snapshots_dir() / f"{snapshot_id}.tar.gz"


def create_snapshot(label: str | None = None, kind: str = "manual") -> dict[str, Any]:
    """Create a snapshot of the current shared base. Return its meta dict."""
    snap_id = uuid.uuid4().hex
    tar_dest = _tar_path(snap_id)
    file_count, size_bytes = _write_tar_gz(tar_dest, paths.shared_base())
    meta: dict[str, Any] = {
        "id": snap_id,
        "label": label,
        "kind": kind,
        "createdAt": _now_iso(),
        "fileCount": file_count,
        "sizeBytes": size_bytes,
    }
    meta_dest = _meta_path(snap_id)
    fd, tmp = tempfile.mkstemp(dir=meta_dest.parent, prefix=".tmp-snap-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(meta, f)
        os.replace(tmp, meta_dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return meta


def snapshot_before_mutation() -> dict[str, Any] | None:
    """Best-effort auto snapshot before a mutation. Swallows all errors and is
    throttled: skips if the newest existing auto snapshot is < 5s old, so a
    burst of mutations collapses to a single snapshot. Returns None on skip/failure.
    """
    try:
        newest_auto_mtime = 0.0
        for meta in _iter_meta():
            if meta.get("kind") != "auto":
                continue
            tar = _tar_path(str(meta.get("id")))
            try:
                mtime = os.path.getmtime(tar)
            except OSError:
                continue
            newest_auto_mtime = max(newest_auto_mtime, mtime)
        if newest_auto_mtime and (_time() - newest_auto_mtime) < _AUTO_THROTTLE_S:
            return None
        return create_snapshot(kind="auto")
    except Exception:
        return None


def _time() -> float:
    import time

    return time.time()


def _iter_meta() -> Iterator[dict[str, Any]]:
    d = snapshots_dir()
    for meta_file in d.glob("*.json"):
        try:
            data = json.loads(meta_file.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("id"):
            yield data


def list_snapshots() -> list[dict[str, Any]]:
    """All snapshot meta dicts, newest createdAt first. Orphaned/unreadable
    sidecars are ignored."""
    metas = list(_iter_meta())
    metas.sort(key=lambda m: str(m.get("createdAt") or ""), reverse=True)
    return metas


def _read_tar_members(fileobj_or_path: Any) -> dict[str, bytes]:
    """Read a tar.gz (path or file-like) into {rel_posix: content_bytes},
    defensively skipping excluded and tar-slip members."""
    out: dict[str, bytes] = {}
    if isinstance(fileobj_or_path, (str, Path)):
        tar = tarfile.open(fileobj_or_path, "r:gz")
    else:
        tar = tarfile.open(fileobj=fileobj_or_path, mode="r:gz")
    with tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            rel = member.name
            if rel.startswith("/") or ".." in Path(rel).parts:
                continue  # tar-slip guard
            if is_excluded(rel):
                continue
            extracted = tar.extractfile(member)
            if extracted is None:
                continue
            out[rel] = extracted.read()
    return out


def _snapshot_members(snapshot_id: str) -> dict[str, bytes]:
    tar = _tar_path(snapshot_id)
    if not tar.is_file():
        raise FileNotFoundError(f"unknown snapshot: {snapshot_id}")
    return _read_tar_members(tar)


def _safe_dest(base: Path, rel: str) -> Path | None:
    """Resolve base/rel and reject any path escaping base (tar-slip guard)."""
    base_resolved = base.resolve()
    dest = (base / rel).resolve()
    try:
        dest.relative_to(base_resolved)
    except ValueError:
        return None
    return base / rel


def _write_member(base: Path, rel: str, content: bytes) -> bool:
    if is_excluded(rel):
        return False
    dest = _safe_dest(base, rel)
    if dest is None:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".tmp-snap-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return True


def _current_members() -> dict[str, bytes]:
    base = paths.shared_base()
    out: dict[str, bytes] = {}
    for abs_path, rel in _iter_files(base):
        try:
            out[rel] = abs_path.read_bytes()
        except OSError:
            continue
    return out


def restore_snapshot(snapshot_id: str) -> dict[str, Any]:
    """Back up the current state, then extract the snapshot over shared_base()."""
    members = _snapshot_members(snapshot_id)  # raises FileNotFoundError if unknown
    backup = create_snapshot(kind="pre-restore", label="before restore")
    base = paths.shared_base()
    base.mkdir(parents=True, exist_ok=True)
    restored = 0
    for rel, content in members.items():
        if _write_member(base, rel, content):
            restored += 1
    return {"restored": restored, "backupId": backup["id"]}


def diff_snapshot(snapshot_id: str) -> dict[str, list[str]]:
    """Compare snapshot (baseline) vs current shared_base()."""
    baseline = _snapshot_members(snapshot_id)  # raises FileNotFoundError if unknown
    current = _current_members()
    added = sorted(set(current) - set(baseline))
    removed = sorted(set(baseline) - set(current))
    modified = sorted(rel for rel in set(baseline) & set(current) if baseline[rel] != current[rel])
    return {"added": added, "removed": removed, "modified": modified}


def export_bundle() -> bytes:
    """Tar.gz bytes of the current non-excluded shared_base() (for download)."""
    return _tar_gz_bytes(paths.shared_base())


def import_preview(data: bytes) -> dict[str, list[str]]:
    """Preview importing a bundle vs current shared_base(). Never writes."""
    incoming = _read_tar_members(io.BytesIO(data))
    current = _current_members()
    creates: list[str] = []
    overwrites: list[str] = []
    unchanged: list[str] = []
    for rel, content in incoming.items():
        if rel not in current:
            creates.append(rel)
        elif current[rel] != content:
            overwrites.append(rel)
        else:
            unchanged.append(rel)
    return {
        "creates": sorted(creates),
        "overwrites": sorted(overwrites),
        "unchanged": sorted(unchanged),
    }


def import_apply(data: bytes) -> dict[str, Any]:
    """Back up the current state, then extract the bundle over shared_base()."""
    incoming = _read_tar_members(io.BytesIO(data))
    backup = create_snapshot(kind="pre-import", label="before import")
    base = paths.shared_base()
    base.mkdir(parents=True, exist_ok=True)
    current = _current_members()
    created = 0
    overwritten = 0
    for rel, content in incoming.items():
        existed = rel in current
        if _write_member(base, rel, content):
            if existed:
                overwritten += 1
            else:
                created += 1
    return {"created": created, "overwritten": overwritten, "backupId": backup["id"]}
