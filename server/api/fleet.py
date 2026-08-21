"""Fleet registry endpoints: list hosts, tune the operator-owned fields.

Read-only for everything the probe writes. Letting the API set ``healthy`` or
``load_avg`` would let the UI lie to the ranker, so the writable surface is
exactly the three fields an operator owns: enabled, weight, max_sessions.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select as sa_select, update as sa_update

from server.auth.middleware import current_identity
from server.db import db_session
from server.db.models import Host

router = APIRouter(prefix="/api/fleet", tags=["fleet"])

# wire name -> column name. Everything else on Host is probe-written.
_PATCHABLE = {"enabled": "enabled", "weight": "weight", "maxSessions": "max_sessions"}
_WEIGHT_MIN, _WEIGHT_MAX = 1, 1000


class HostPatch(BaseModel):
    enabled: bool | None = None
    weight: int | None = None
    maxSessions: int | None = None


def host_to_wire(h: Host) -> dict:
    """Serialize a Host row for the API.

    ``healthy`` stays tri-state: None means "never probed", and collapsing it to
    false would tell the operator a host is DOWN when it is merely unknown.
    """
    return {
        "name": h.name,
        "endpoint": h.endpoint,
        "enabled": h.enabled,
        "weight": h.weight,
        "maxSessions": h.max_sessions,
        "healthy": h.healthy,
        "activeSessions": h.active_sessions,
        "loadAvg": h.load_avg,
        "memAvailableMb": h.mem_available_mb,
        "sha": h.sha,
        "protocol": h.protocol,
        "lastProbeAt": h.last_probe_at.isoformat() if h.last_probe_at else None,
        "lastError": h.last_error,
    }


def sanitize_patch(body: dict) -> dict:
    """Keep only operator-settable fields, mapped to column names and clamped.

    A None value means "not supplied" and is ignored — a PATCH omitting a field
    must leave it alone rather than nulling it.
    """
    out: dict = {}
    for wire, col in _PATCHABLE.items():
        if body.get(wire) is None:
            continue
        val = body[wire]
        if col == "weight":
            val = max(_WEIGHT_MIN, min(_WEIGHT_MAX, int(val)))
        out[col] = val
    return out


@router.get("/hosts")
async def list_hosts(_identity: str = Depends(current_identity)) -> dict:
    async with db_session() as db:
        rows = (await db.execute(sa_select(Host).order_by(Host.name))).scalars().all()
    return {"hosts": [host_to_wire(r) for r in rows]}


@router.put("/hosts/{name}")
async def update_host(name: str, body: HostPatch,
                      _identity: str = Depends(current_identity)) -> dict:
    values = sanitize_patch(body.model_dump())
    if not values:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"code": "no_writable_fields",
                                    "message": "nothing settable in this request"})
    async with db_session() as db:
        await db.execute(sa_update(Host).where(Host.name == name).values(**values))
        row = (await db.execute(
            sa_select(Host).where(Host.name == name))).scalars().first()
    # Re-read rather than trusting rowcount: an UPDATE that matches a row but
    # changes nothing reports 0 on some drivers, which would 404 a real host.
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"code": "unknown_host", "message": name})
    return host_to_wire(row)
