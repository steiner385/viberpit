"""Image attachments — pure helpers plus async DB/file lifecycle.

Canonical implementation unifying the two prior parallel ones:

  * Persistence + lifecycle (DB ``Attachment`` rows, session/turn binding, orphan
    sweep, dedup-aware deletion) — the genuine superset capability.
  * Content-addressed-by-sha256 on-disk storage with dedup — the same image bytes
    uploaded twice reuse one on-disk file and (where possible) one row.

The DB row ``id`` (UUID) is the public API handle; on disk the bytes live at
``attachments_dir()/{sha256}.{ext}``. Because multiple rows can share one sha file
(dedup), every unlink path is reference-aware: a file is only removed once no
remaining ``Attachment`` row references its sha.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ccswitch import paths
from server.db.models import Attachment

# Per-message limits (confirmed product decision): 10 MB / 5 images per message.
MAX_BYTES = 10 * 1024 * 1024
MAX_PER_MESSAGE = 5

# Supported image types → on-disk extension. Used both for sniffing acceptance
# and for the content-addressed filename suffix.
_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}


def sniff_media_type(head: bytes) -> str | None:
    """Detect image type from leading magic bytes. Returns a media type or None.

    Never trusts the declared/Content-Type — the API layer additionally rejects an
    upload whose declared type disagrees with what we sniff (type-confusion defense).
    """
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return "image/gif"
    if len(head) >= 12 and head[0:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def ext_for(media_type: str) -> str:
    return _EXT.get(media_type, "bin")


def storage_path(sha256: str, media_type: str):
    """Content-addressed on-disk path for an attachment's bytes."""
    return paths.attachments_dir() / f"{sha256}.{ext_for(media_type)}"


def image_block(media_type: str, data_b64: str) -> dict:
    """Anthropic image content block."""
    return {"type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": data_b64}}


async def resolve_and_bind(db, session_id, turn_id, ids) -> list[dict]:
    """Bind up to MAX_PER_MESSAGE attachments to a session+turn; return descriptors.

    Unknown ids are skipped (the cap is applied to the provided list before lookup).
    Each descriptor carries the content-addressed on-disk ``path`` plus the serve
    ``url`` (``/api/attachments/{id}``) the live UserMessage echoes for rendering.
    """
    out: list[dict] = []
    for aid in list(ids)[:MAX_PER_MESSAGE]:
        row = (await db.execute(
            select(Attachment).where(Attachment.id == aid)
        )).scalars().first()
        if row is None:
            continue
        row.session_id = session_id
        row.turn_id = turn_id
        out.append({
            "id": row.id,
            "mime": row.media_type,
            "name": row.filename,
            "path": str(storage_path(row.sha256, row.media_type)),
            "url": f"/api/attachments/{row.id}",
        })
    return out


async def _unlink_if_unreferenced(db, sha256: str, media_type: str) -> None:
    """Remove the on-disk sha file only when no Attachment row still references it.

    Dedup means one file can back several rows; deleting one session's row must not
    pull a file still referenced by another. Call AFTER deleting the row(s) being
    removed (or excluding them from the count).
    """
    remaining = (await db.execute(
        select(Attachment.id).where(Attachment.sha256 == sha256).limit(1)
    )).first()
    if remaining is not None:
        return
    p = storage_path(sha256, media_type)
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass


async def sweep_orphans(db, older_than_seconds: int = 86400) -> int:
    """Delete unbound (session_id IS NULL) attachments older than cutoff.

    Returns the number of rows removed. File unlink is dedup-aware.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)
    rows = (await db.execute(
        select(Attachment).where(
            Attachment.session_id.is_(None), Attachment.created_at < cutoff)
    )).scalars().all()
    for row in rows:
        sha, mt = row.sha256, row.media_type
        await db.delete(row)
        await db.flush()
        await _unlink_if_unreferenced(db, sha, mt)
    return len(rows)


async def delete_for_session(db, session_id) -> int:
    """Unlink files and delete Attachment rows for a session; returns count deleted.

    Dedup-aware: an on-disk sha file referenced by another session's row is NOT
    removed.
    """
    rows = (await db.execute(
        select(Attachment).where(Attachment.session_id == session_id)
    )).scalars().all()
    for row in rows:
        sha, mt = row.sha256, row.media_type
        await db.delete(row)
        await db.flush()
        await _unlink_if_unreferenced(db, sha, mt)
    return len(rows)
