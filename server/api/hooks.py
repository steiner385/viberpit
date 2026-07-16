"""Hooks API — read/write the shared settings.json hooks block + a sandboxed dry-run.

Hooks run arbitrary shell on the host on every session, so the GET response carries a
standing warning for the UI to surface."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from server.auth.middleware import current_identity
from server.library import hooks as hooks_lib

router = APIRouter(prefix="/api/hooks", tags=["hooks"])

_WARNING = "Hooks run arbitrary shell on the host on every session. Edit with care."


class HookRow(BaseModel):
    event: str
    matcher: str | None = None
    command: str
    enabled: bool = True


class HooksBody(BaseModel):
    events: list[HookRow]


class DryRunBody(BaseModel):
    command: str
    event: dict[str, Any] | None = None


@router.get("")
async def get_hooks(_identity: str = Depends(current_identity)) -> dict:
    return {"events": hooks_lib.list_hooks()["events"], "warning": _WARNING}


@router.put("")
async def put_hooks(body: HooksBody, _identity: str = Depends(current_identity)) -> dict:
    rows = [r.model_dump() for r in body.events]
    try:
        hooks_lib.set_hooks(rows)
    except hooks_lib.HooksValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return {"events": hooks_lib.list_hooks()["events"], "warning": _WARNING}


@router.post("/dry-run")
async def post_dry_run(body: DryRunBody, _identity: str = Depends(current_identity)) -> dict:
    return hooks_lib.dry_run(body.command, body.event)
