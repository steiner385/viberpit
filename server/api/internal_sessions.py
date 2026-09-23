"""Internal-only session creation for the primary daemon's own automation loops
(spec: automation dispatch balance, 2026-09-20). One route, one caller.

Backlog Runner, PR Doctor, SDLC Intake and the other fleet-global automations run
inside whichever daemon has the primary role (server/sessions/orchestrator.py,
``daemon_role() == "primary"``, gated ~line 2164) and used to call that daemon's own
``SessionOrchestrator.create_session`` directly — spawning unconditionally on the
primary's own host, the only create path in the whole app that never went through
the edge's capacity-ranked ``FleetOrchestrator``/``rank_hosts`` (server/sessions/
fleet.py). This route lets that same daemon reach its OWN co-located edge instead
(server/sessions/internal_dispatch.py is the caller), so an automation-originated
session gets ranked across hosts exactly like a human/API dispatch.

Trusted-internal fields (``origin``, ``origin_ref``, ``ownership_token``, an
ALREADY-resolved ``permission_mode``) are "deliberately absent from HTTP input" on
the public ``POST /api/sessions`` (see SessionOrchestrator.create_session's
docstring) — this route is exactly that trusted internal surface, gated by the
loopback-only ``ccsys1`` token (server/auth/internal_token.py) rather than a human
identity, so it does not extend the public contract at all.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from server.auth.internal_token import ALLOWED_PATH, CERTIFY_PATH

log = logging.getLogger(__name__)

router = APIRouter(tags=["internal"])


class InternalCertifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str


@router.post(CERTIFY_PATH)
async def certify_harvested_session(body: InternalCertifyRequest, request: Request):
    """Certify a harvested one-shot (classify / materialize) session ``complete``
    on the host that owns its driver.

    The chained ingest that harvests such a session runs inside the primary daemon,
    but the session itself was placed by the route above — on whichever host ranked
    best. The certify has to run where the live record and driver are, so the primary
    asks its co-located edge, and ``FleetOrchestrator`` routes by the row's host stamp
    (``certify_harvested_complete`` is in its ``_PER_SESSION`` set). Single-host: the
    facade runs it in-process. Same caller, same token, same posture as the create.
    """
    await request.app.state.sessions.certify_harvested_complete(body.session_id)
    return {"ok": True}


class InternalCreateSessionRequest(BaseModel):
    """Mirrors SessionOrchestrator.create_session's kwargs (minus cols/rows —
    terminal geometry, meaningless for a headless automation spawn). ``extra="forbid"``
    because this is a trusted-internal surface: an unrecognized field is a caller bug,
    not a value to silently ignore."""

    model_config = ConfigDict(extra="forbid")

    name: str
    prefer: str | None = None
    account: str | None = None
    initial_prompt: str | None = None
    cwd: str | None = None
    branch: str | None = None
    repo: str | None = None
    identity: str | None = None
    permission_mode: str | None = None
    attachment_ids: list[str] | None = None
    name_is_auto: bool | None = None
    origin: str | None = None
    origin_ref: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    kind: str | None = None
    owner_identity: str | None = None
    engine: str | None = None
    client_node: str | None = None
    display: str | None = None
    autopilot_paused: bool | None = None
    auto_archive_opt_out_reason: str | None = None
    ownership_token: str | None = None
    parent_session_id: str | None = None


@router.post(ALLOWED_PATH, status_code=status.HTTP_201_CREATED)
async def create_automation_session(body: InternalCreateSessionRequest, request: Request):
    """Fields the caller never set are omitted, not passed as None — so
    ``SessionOrchestrator.create_session``'s own defaults apply exactly as they would
    for a direct in-process call (today's ``create_kwargs`` is the same kind of sparse
    dict). ``IdentityMiddleware`` has already checked the ``ccsys1`` token, the
    loopback peer and that this is the one route it may call — nothing further to
    verify here.
    """
    orch = request.app.state.sessions
    kwargs = body.model_dump(exclude_unset=True, exclude={"prefer"})
    try:
        rec = await orch.create_session(prefer=body.prefer, **kwargs)
    except Exception as exc:
        # Same generic envelope the mTLS RPC framework uses for a create_session
        # error (server/ipc/daemon.py's `/rpc/{method}` handler): {type, message,
        # data}. The one caller of this route (internal_dispatch.create_via_edge)
        # reconstructs it with the SAME helper the RPC client uses
        # (server.ipc.client.reconstruct_create_session_error) — one error contract,
        # two transports.
        err: dict = {"type": type(exc).__name__, "message": str(exc)}
        data = getattr(exc, "error_data", None)
        if data is not None:
            err["data"] = data
        log.info("internal automation-session create refused: %s", err["type"])
        return JSONResponse(status_code=422, content={"error": err})
    return {"session": rec.to_dict()}
