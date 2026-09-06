"""POST /api/assistant/session · GET /api/assistant/status (spec §4.1, sub-spec B §7)."""
from __future__ import annotations

import hashlib
import logging
import os

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ccswitch.selector import AllAccountsBusy
from server.audit import record_audit
from server.auth.middleware import current_identity
from server.db import get_db
from server.sessions.model_policy import resolve_model_policy
from server.sessions.engine import AgentEngineUnavailable
from server.sessions.types import ASSISTANT_KIND

from .policy import (assistant_enabled, assistant_engine, assistant_model,
                     operator_tools_enabled)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/assistant", tags=["assistant"])


class CreateBody(BaseModel):
    fresh: bool = False
    model: str | None = None


def _err(code: str, message: str, http: int) -> JSONResponse:
    return JSONResponse(status_code=http, content={"error": {"code": code, "message": message}})


def _scratch_cwd(identity: str) -> str:
    from ccswitch import paths as _paths
    h = hashlib.sha256(identity.encode()).hexdigest()[:16]
    p = os.path.join(str(_paths.cfg_root()), "assistant", h)
    os.makedirs(p, exist_ok=True)
    return p


@router.get("/status")
async def status_(request: Request, identity: str = Depends(current_identity), db=Depends(get_db)):
    orch = request.app.state.sessions
    enabled = await assistant_enabled(db)
    operator = await operator_tools_enabled(db)
    rec = await orch.find_assistant(identity) if enabled else None
    return {"enabled": enabled, "sessionId": rec.id if rec else None,
            "model": (rec.model if rec and rec.model else await assistant_model(db)),
            # Reported so the drawer can say which engine answered rather than
            # assuming Claude; the session's own engine wins over the deployment
            # default for an already-running drawer.
            "engine": (rec.engine.value if rec and rec.engine
                       else assistant_engine().value),
            "operatorTools": operator}


@router.post("/session", status_code=status.HTTP_201_CREATED)
async def create(body: CreateBody, request: Request,
                 identity: str = Depends(current_identity), db=Depends(get_db)):
    if getattr(request.state, "via", None) == "assistant":
        return _err("ASSISTANT_RECURSION", "the assistant cannot create assistants", 403)
    if not await assistant_enabled(db):
        return _err("ASSISTANT_DISABLED", "enable the assistant in Settings → General", 404)
    orch = request.app.state.sessions
    model = body.model or await assistant_model(db)
    if await resolve_model_policy(db, model=model) != "allow":
        return _err("MODEL_GATED", f"{model} needs approval and cannot run the assistant", 400)

    # Resolved before the reuse branch so every return path can report the engine
    # that will actually answer — a running thread's own, not the deployment
    # default it may no longer match.
    engine = assistant_engine()
    existing = await orch.find_assistant(identity)
    if existing is not None and not body.fresh:
        state = "existing"
        if existing.status == "ended":
            # Thread the caller's verified identity: ensure_driver's own docstring
            # warns that a resumed driver built under the SYSTEM_IDENTITY default
            # registers questions/permissions under "system" and then rejects every
            # answer the real user sends (the #1080 failure shape). The drawer's WS
            # attach re-threads it moments later, which is why this is self-correcting
            # today — but anything the resumed driver asks before that attach lands is
            # unanswerable, and the reap-and-reopen flow leans on this path.
            await orch.ensure_driver(existing.id, identity=identity)
            state = "resumed"
        return {"id": existing.id, "model": existing.model or model,
                "engine": (existing.engine.value if existing.engine else engine.value),
                "state": state}
    if existing is not None:
        await orch.archive_session(existing.id, archived_by="user",
                                   context={"reason": "assistant-reset"})
        await record_audit(db, "assistant.session.reset", identity, {"sessionId": existing.id})
        await db.commit()

    try:
        rec = await orch.create_session(
            name="Assistant", cwd=_scratch_cwd(identity), identity=identity,
            permission_mode="interactive", name_is_auto=False, model=model,
            kind=ASSISTANT_KIND, owner_identity=identity, engine=engine)
    except AgentEngineUnavailable as e:
        # A deployment that asked for a Codex drawer on a host with no Codex
        # runner gets a typed refusal, not a silent Claude fallback: the two
        # engines have different overlays and different boundaries, so quietly
        # substituting one would answer a question the operator did not ask.
        return _err("ASSISTANT_ENGINE_UNAVAILABLE", str(e), 409)
    except AllAccountsBusy as e:
        return _err("ALL_ACCOUNTS_LIMITED", str(e), 409)
    await record_audit(db, "assistant.session.create", identity,
                       {"sessionId": rec.id, "model": model, "engine": engine.value})
    await db.commit()
    return {"id": rec.id, "model": model, "engine": engine.value, "state": "created"}
