"""Config snapshot API — snapshot / restore / diff / export / import over the
shared config base. Read + mutate routes call ``server.library.snapshots``
directly; no orchestrator/daemon involvement. Credentials/identity files are
excluded from every capture and every write (see ``snapshots.is_excluded``)."""
from __future__ import annotations

import io
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from server.auth.middleware import current_identity
from server.library import snapshots

router = APIRouter(prefix="/api/snapshots", tags=["snapshots"])


class CreateBody(BaseModel):
    label: str | None = None


@router.get("")
async def get_snapshots(_identity: str = Depends(current_identity)) -> list[dict]:
    return snapshots.list_snapshots()


@router.post("")
async def post_snapshot(
    body: CreateBody | None = None, _identity: str = Depends(current_identity)
) -> dict:
    label = body.label if body else None
    return snapshots.create_snapshot(label=label, kind="manual")


@router.post("/{snapshot_id}/restore")
async def post_restore(snapshot_id: str, _identity: str = Depends(current_identity)) -> dict:
    try:
        return snapshots.restore_snapshot(snapshot_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail={"error": "unknown_snapshot"})


@router.get("/{snapshot_id}/diff")
async def get_diff(snapshot_id: str, _identity: str = Depends(current_identity)) -> dict:
    try:
        return snapshots.diff_snapshot(snapshot_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail={"error": "unknown_snapshot"})


@router.get("/export")
async def get_export(_identity: str = Depends(current_identity)) -> Any:
    data = snapshots.export_bundle()
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/gzip",
        headers={
            "Content-Disposition": 'attachment; filename="ccswitch-config-bundle.tar.gz"'
        },
    )


@router.post("/import/preview")
async def post_import_preview(
    file: UploadFile = File(...), _identity: str = Depends(current_identity)
) -> dict:
    data = await file.read()
    return snapshots.import_preview(data)


@router.post("/import/apply")
async def post_import_apply(
    file: UploadFile = File(...), _identity: str = Depends(current_identity)
) -> dict:
    data = await file.read()
    return snapshots.import_apply(data)
