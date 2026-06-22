"""Image attachment upload + serve endpoints."""
from __future__ import annotations

import hashlib
import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select

from ccswitch import paths
from server.audit import record_audit
from server.auth.middleware import current_identity
from server.db import get_db
from server.db.models import Attachment
from server.sessions.attachments import MAX_BYTES, ext_for, sniff_media_type

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/attachments", tags=["attachments"])

_CHUNK = 64 * 1024


@router.post("", status_code=status.HTTP_201_CREATED)
async def upload_attachment(
    file: UploadFile = File(...),
    sessionId: str | None = Form(default=None),
    identity: str = Depends(current_identity),
    db=Depends(get_db),
):
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
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
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

    data = bytes(buf)
    sha = hashlib.sha256(data).hexdigest()
    row = Attachment(
        session_id=sessionId, media_type=media_type,
        filename=(file.filename or "image")[:255],
        byte_size=len(data), sha256=sha, uploaded_by=identity,
    )
    db.add(row)
    await db.flush()  # populate row.id

    d = paths.attachments_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{row.id}.{ext_for(media_type)}").write_bytes(data)
    await db.commit()

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
    row = (await db.execute(
        select(Attachment).where(Attachment.id == attachment_id)
    )).scalars().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"code": "not_found", "message": "attachment not found"})
    path = paths.attachments_dir() / f"{row.id}.{ext_for(row.media_type)}"
    if not path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"code": "not_found", "message": "attachment file missing"})
    return FileResponse(str(path), media_type=row.media_type,
                        headers={"Cache-Control": "private",
                                 "Content-Disposition": "inline"})
