from __future__ import annotations
import hashlib, logging, shutil, time
from pathlib import Path
from ccswitch import paths

log = logging.getLogger(__name__)

ALLOWED: dict[str, str] = {
    "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp",
}
MAX_BYTES = 5 * 1024 * 1024
MAX_IMAGES = 8


class UploadError(Exception):
    """Raised for an unsupported type, oversize image, or missing staged file."""


def _staging() -> Path:
    d = paths.uploads_dir() / "_staging"
    d.mkdir(parents=True, exist_ok=True)
    return d


def sniff_mime(raw: bytes) -> str | None:
    """Detect image type from magic bytes (do not trust the declared MIME)."""
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def stage_image(declared_mime: str, raw: bytes) -> dict:
    if len(raw) > MAX_BYTES:
        raise UploadError(f"image exceeds {MAX_BYTES} bytes")
    sniffed = sniff_mime(raw)
    if sniffed is None or sniffed not in ALLOWED:
        raise UploadError(f"unsupported image type (declared {declared_mime!r})")
    if sniffed != declared_mime:
        raise UploadError(f"declared {declared_mime!r} but sniffed {sniffed!r}")
    sha = hashlib.sha256(raw).hexdigest()
    dest = _staging() / f"{sha}.{ALLOWED[sniffed]}"
    if not dest.exists():
        dest.write_bytes(raw)
    return {"sha": sha, "mime": sniffed, "name": ""}


def staging_path(sha: str) -> Path | None:
    for ext in ALLOWED.values():
        p = _staging() / f"{sha}.{ext}"
        if p.exists():
            return p
    return None


def image_path(session_id: str, sha: str) -> Path | None:
    base = paths.uploads_dir() / session_id
    for ext in ALLOWED.values():
        p = base / f"{sha}.{ext}"
        if p.exists():
            return p
    return None


def associate(session_id: str, sha: str, mime: str) -> Path:
    existing = image_path(session_id, sha)
    if existing is not None:
        return existing
    src = staging_path(sha)
    if src is None:
        raise UploadError(f"no staged image for sha {sha}")
    base = paths.uploads_dir() / session_id
    base.mkdir(parents=True, exist_ok=True)
    dest = base / f"{sha}.{ALLOWED.get(mime, src.suffix.lstrip('.'))}"
    shutil.copyfile(src, dest)
    return dest


def sweep_staging(max_age_h: float = 6.0) -> int:
    cutoff = time.time() - max_age_h * 3600
    removed = 0
    staging = _staging()
    for p in staging.iterdir():
        if p.is_file() and p.stat().st_mtime < cutoff:
            p.unlink(missing_ok=True)
            removed += 1
    return removed
