"""Fleet registry endpoints: list hosts, tune the operator-owned fields.

Read-only for everything the probe writes. Letting the API set ``healthy`` or
``load_avg`` would let the UI lie to the ranker, so the writable surface is
exactly the fields an operator owns: enabled, weight, max_sessions, and the
upgrade config (ssh_host, repo_path).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy import delete as sa_delete, select as sa_select, update as sa_update

from server.auth.middleware import current_identity
from server.db import db_session
from server.db.models import Host
from server.deploy.steps import SSH_HOST_RE, SSH_REPO_RE
from server.sessions import host_upgrade

router = APIRouter(prefix="/api/fleet", tags=["fleet"])

# wire name -> column name. Everything else on Host is probe-written.
_PATCHABLE = {"enabled": "enabled", "weight": "weight", "maxSessions": "max_sessions",
              "sshHost": "ssh_host", "repoPath": "repo_path"}
_WEIGHT_MIN, _WEIGHT_MAX = 1, 1000


class HostCreate(BaseModel):
    name: str
    endpoint: str
    weight: int = 100
    maxSessions: int | None = None

    @field_validator("name")
    @classmethod
    def name_nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be blank")
        return v.strip()

    @field_validator("endpoint")
    @classmethod
    def endpoint_nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("endpoint must not be blank")
        return v.strip()


class HostPatch(BaseModel):
    enabled: bool | None = None
    weight: int | None = None
    maxSessions: int | None = None
    sshHost: str | None = None
    repoPath: str | None = None


class UpgradeBody(BaseModel):
    """POST /hosts/{name}/upgrade body. ``force`` skips the active-sessions guard."""
    force: bool = False


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
        "checkoutSha": h.checkout_sha,
        "protocol": h.protocol,
        "lastProbeAt": h.last_probe_at.isoformat() if h.last_probe_at else None,
        "lastError": h.last_error,
        "sshHost": h.ssh_host,
        "repoPath": h.repo_path,
        # Live in-process upgrade progress, so the UI can poll it off the same
        # list it already refreshes ({"state": "idle", ...} when never run).
        "upgrade": host_upgrade.upgrade_wire(h.name),
    }


def sanitize_patch(body: dict) -> dict:
    """Keep only operator-settable fields, mapped to column names and clamped.

    A None value means "not supplied" and is ignored — a PATCH omitting a field
    must leave it alone rather than nulling it; an EMPTY STRING clears an
    upgrade-config field back to NULL (not configured).

    ssh_host / repo_path land in the argv of a sudo-capable ssh command, so they
    are validated with the SAME patterns the deploy controller applies to
    CCSWITCH_DAEMON_SSH_HOST / _REPO (``server.deploy.steps``) — a value that
    could smuggle an ssh option (``-oProxyCommand=…``) or shell metacharacters
    is rejected with 400, never stored.
    """
    out: dict = {}
    for wire, col in _PATCHABLE.items():
        if body.get(wire) is None:
            continue
        val = body[wire]
        if col == "weight":
            val = max(_WEIGHT_MIN, min(_WEIGHT_MAX, int(val)))
        elif col in ("ssh_host", "repo_path"):
            val = str(val).strip() or None
            if val is not None and col == "ssh_host" and not SSH_HOST_RE.match(val):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"code": "invalid_ssh_host",
                            "message": f"{val!r} is not a valid [user@]host value"})
            if val is not None and col == "repo_path" and not SSH_REPO_RE.match(val):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"code": "invalid_repo_path",
                            "message": f"{val!r} is not a safe absolute path"})
        out[col] = val
    return out


@router.post("/hosts", status_code=status.HTTP_201_CREATED)
async def create_host(body: HostCreate,
                      _identity: str = Depends(current_identity)) -> dict:
    async with db_session() as db:
        existing = (await db.execute(
            sa_select(Host).where(Host.name == body.name))).scalars().first()
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "host_exists",
                        "message": f"a host named {body.name!r} already exists"})
        host = Host(
            name=body.name,
            endpoint=body.endpoint,
            weight=max(_WEIGHT_MIN, min(_WEIGHT_MAX, body.weight)),
            max_sessions=body.maxSessions,
            enabled=True,
        )
        db.add(host)
        await db.flush()
        # Serialize inside the session: `host` is the row we just added, so a
        # re-read is redundant, and .first() types as Host|None which
        # host_to_wire does not accept.
        wire = host_to_wire(host)
    return wire


@router.delete("/hosts/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_host(name: str, _identity: str = Depends(current_identity)) -> None:
    async with db_session() as db:
        # Check existence rather than trusting rowcount: it is not typed on
        # Result[Any], and a statement that matches a row can still report 0 on
        # some drivers — which would 404 a host that exists. Mirrors update_host.
        found = (await db.execute(
            sa_select(Host.name).where(Host.name == name))).scalar_one_or_none()
        if found is not None:
            await db.execute(sa_delete(Host).where(Host.name == name))
    if found is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"code": "unknown_host", "message": name})


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


@router.post("/hosts/{name}/upgrade", status_code=status.HTTP_202_ACCEPTED)
async def upgrade_host(name: str, body: UpgradeBody | None = None,
                       _identity: str = Depends(current_identity)) -> dict:
    """Kick off a background daemon upgrade on a remote runner (202 Accepted).

    The work runs in an asyncio task — never in this request — and progress is
    polled via the ``upgrade`` block on ``GET /hosts``. Guards, in order:
    unknown host 404; unconfigured / disabled / busy host 409 (``force`` skips
    only the active-sessions check — restarting the daemon kills or drains its
    live sessions, which must be an explicit operator choice); a second POST
    while one is running 409s rather than stacking upgrades.
    """
    async with db_session() as db:
        row = (await db.execute(
            sa_select(Host).where(Host.name == name))).scalars().first()
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail={"code": "unknown_host", "message": name})
        if not row.ssh_host or not row.repo_path:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "upgrade_not_configured",
                        "message": f"host {name!r} has no ssh_host/repo_path "
                                   "configured — set both before upgrading"})
        if not row.enabled:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "host_disabled",
                        "message": f"host {name!r} is disabled"})
        if (row.active_sessions or 0) > 0 and not (body and body.force):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "sessions_active",
                        "message": f"host {name!r} reports "
                                   f"{row.active_sessions} active session(s); "
                                   "pass force=true to upgrade anyway"})
        ssh_host, repo_path, endpoint = row.ssh_host, row.repo_path, row.endpoint
    if not host_upgrade.start_upgrade(name, ssh_host=ssh_host,
                                      repo_path=repo_path, endpoint=endpoint):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "upgrade_running",
                    "message": f"an upgrade for host {name!r} is already running"})
    return {"started": True, "host": name}
