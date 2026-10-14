"""Config snapshot API — snapshot / restore / diff / export / import over the
shared config base. Read + mutate routes call ``server.library.snapshots``
directly (off the event loop — every op is blocking tar/filesystem I/O); no
orchestrator/daemon involvement. Credentials/identity files are
excluded from every capture and every write (see ``snapshots.is_excluded``)."""
from __future__ import annotations

import asyncio
import io
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from viberpit import brand
from server.auth.middleware import current_identity
from server.library import snapshots

router = APIRouter(prefix="/api/snapshots", tags=["snapshots"])


class CreateBody(BaseModel):
    label: str | None = None


@router.get("")
async def get_snapshots(_identity: str = Depends(current_identity)) -> list[dict]:
    return await asyncio.to_thread(snapshots.list_snapshots)


@router.post("")
async def post_snapshot(
    body: CreateBody | None = None, _identity: str = Depends(current_identity)
) -> dict:
    label = body.label if body else None
    return await asyncio.to_thread(snapshots.create_snapshot, label=label, kind="manual")


@router.post("/{snapshot_id}/restore")
async def post_restore(snapshot_id: str, _identity: str = Depends(current_identity)) -> dict:
    try:
        return await asyncio.to_thread(snapshots.restore_snapshot, snapshot_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail={"error": "unknown_snapshot"})


@router.get("/{snapshot_id}/diff")
async def get_diff(snapshot_id: str, _identity: str = Depends(current_identity)) -> dict:
    try:
        return await asyncio.to_thread(snapshots.diff_snapshot, snapshot_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail={"error": "unknown_snapshot"})


@router.get("/export")
async def get_export(_identity: str = Depends(current_identity)) -> Any:
    data = await asyncio.to_thread(snapshots.export_bundle)
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{brand.SLUG}-config-bundle.tar.gz"'
        },
    )


@router.post("/import/preview")
async def post_import_preview(
    file: UploadFile = File(...), _identity: str = Depends(current_identity)
) -> dict:
    data = await file.read()
    return await asyncio.to_thread(snapshots.import_preview, data)


@router.post("/import/apply")
async def post_import_apply(
    file: UploadFile = File(...), _identity: str = Depends(current_identity)
) -> dict:
    data = await file.read()
    return await asyncio.to_thread(snapshots.import_apply, data)
