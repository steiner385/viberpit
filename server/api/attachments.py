"""Image attachment upload + serve endpoints (single canonical surface).

POST /api/attachments      → streaming upload, hard byte cap, magic-byte sniff,
                             declared-vs-sniffed reconciliation, sha256 dedup,
                             DB row + audit. Returns {id, mediaType, filename,
                             byteSize, url}.
GET  /api/attachments/{id} → serve the bytes for a row by its content-addressed file.
"""
from __future__ import annotations

import hashlib
import logging

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select

from server.audit import record_audit
from server.auth.middleware import current_identity
from server.db import get_db
from server.db.models import Attachment
from server.sessions.attachments import (
    MAX_BYTES,
    sniff_media_type,
    storage_path,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/attachments", tags=["attachments"])

_CHUNK = 64 * 1024


@router.post("", status_code=status.HTTP_201_CREATED)
async def upload_attachment(
    file: UploadFile = File(...),
    identity: str = Depends(current_identity),
    db=Depends(get_db),
):
    """Accept a multipart image upload; stage it as a DB-backed, deduped attachment.

    - Streams into memory with a hard byte cap (413 if exceeded).
    - Sniffs the real type from magic bytes; 400 if unsupported.
    - Rejects (400) when the declared Content-Type disagrees with the sniffed type.
    - Dedups by sha256: identical bytes reuse the existing row + on-disk file.
    """
    declared = (file.content_type or "").split(";")[0].strip().lower()

    # Stream into memory with a hard cap; sniff the first chunk for the real type.
    buf = bytearray()
    media_type: str | None = None
    while True:
        chunk = await file.read(_CHUNK)
        if not chunk:
            break
        buf.extend(chunk)
        if media_type is None and len(buf) >= 12:
            media_type = sniff_media_type(bytes(buf[:12]))
        if len(buf) > MAX_BYTES:
            return JSONResponse(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                content={"error": {"code": "too_large",
                                   "message": f"image exceeds {MAX_BYTES} bytes"}},
            )
    if media_type is None:
        media_type = sniff_media_type(bytes(buf[:12]))
    if media_type is None:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": {"code": "bad_type",
                               "message": "file is not a supported image (png/jpeg/webp/gif)"}},
        )
    # Type-confusion defense: a declared type, when present, must match the sniff.
    if declared and declared != media_type:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": {"code": "type_mismatch",
                               "message": f"declared {declared!r} but sniffed {media_type!r}"}},
        )

    data = bytes(buf)
    sha = hashlib.sha256(data).hexdigest()

    # Dedup: if an identical-bytes attachment already exists, reuse its row + file.
    existing = (await db.execute(
        select(Attachment).where(Attachment.sha256 == sha)
    )).scalars().first()
    if existing is not None:
        # Ensure the content-addressed file is present (it normally is).
        path = storage_path(sha, existing.media_type)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        await record_audit(db=db, type="attachment.upload", identity=identity,
                           detail={"id": existing.id, "bytes": len(data),
                                   "type": existing.media_type, "dedup": True})
        return JSONResponse(
            status_code=status.HTTP_201_CREATED,
            content={"id": existing.id, "mediaType": existing.media_type,
                     "filename": existing.filename, "byteSize": existing.byte_size,
                     "url": f"/api/attachments/{existing.id}"},
        )

    row = Attachment(
        session_id=None, media_type=media_type,
        filename=(file.filename or "image")[:255],
        byte_size=len(data), sha256=sha, uploaded_by=identity,
    )
    db.add(row)
    await db.flush()  # populate row.id

    path = storage_path(sha, media_type)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(data)

    await record_audit(db=db, type="attachment.upload", identity=identity,
                       detail={"id": row.id, "bytes": len(data), "type": media_type})

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={"id": row.id, "mediaType": media_type, "filename": row.filename,
                 "byteSize": len(data), "url": f"/api/attachments/{row.id}"},
    )


@router.get("/{attachment_id}")
async def serve_attachment(
    attachment_id: str,
    _identity: str = Depends(current_identity),
    db=Depends(get_db),
):
    """Serve an attachment's bytes by row id → content-addressed file. 404 if missing."""
    row = (await db.execute(
        select(Attachment).where(Attachment.id == attachment_id)
    )).scalars().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"code": "not_found", "message": "attachment not found"})
    path = storage_path(row.sha256, row.media_type)
    if not path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"code": "not_found", "message": "attachment file missing"})
    return FileResponse(str(path), media_type=row.media_type,
                        headers={"Cache-Control": "private",
                                 "Content-Disposition": "inline"})
