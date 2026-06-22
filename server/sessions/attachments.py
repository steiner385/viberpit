"""Helpers for image attachments — pure helpers plus async DB/file helpers."""
from __future__ import annotations

from sqlalchemy import select

from ccswitch import paths
from server.db.models import Attachment

MAX_BYTES = 10 * 1024 * 1024
MAX_PER_MESSAGE = 5

_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}


def sniff_media_type(head: bytes) -> str | None:
    """Detect image type from leading bytes. Returns a media type or None."""
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


def image_block(media_type: str, data_b64: str) -> dict:
    return {"type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": data_b64}}


async def resolve_and_bind(db, session_id, turn_id, ids) -> list[dict]:
    """Bind up to MAX_PER_MESSAGE attachments to a session+turn; return descriptors."""
    out = []
    for aid in ids[:MAX_PER_MESSAGE]:
        row = (await db.execute(
            select(Attachment).where(Attachment.id == aid)
        )).scalars().first()
        if row is None:
            continue
        row.session_id = session_id
        row.turn_id = turn_id
        out.append({
            "id": row.id, "media_type": row.media_type, "filename": row.filename,
            "path": str(paths.attachments_dir() / f"{row.id}.{ext_for(row.media_type)}"),
        })
    return out


async def delete_for_session(db, session_id) -> int:
    """Unlink files and delete Attachment rows for a session; returns count deleted."""
    rows = (await db.execute(
        select(Attachment).where(Attachment.session_id == session_id)
    )).scalars().all()
    for row in rows:
        p = paths.attachments_dir() / f"{row.id}.{ext_for(row.media_type)}"
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
        await db.delete(row)
    return len(rows)
