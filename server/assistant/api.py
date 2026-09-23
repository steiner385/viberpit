"""POST /api/assistant/session · GET /api/assistant/status (spec §4.1, sub-spec B §7)."""
from __future__ import annotations

import hashlib
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import func as sa_func
from sqlalchemy import select

from ccswitch.selector import AllAccountsBusy
from server.api.fleet_errors import FleetDispatchRefused, fleet_refusal_response
from server.assistant.token import mint_user_token
from server.audit import record_audit
from server.auth.middleware import current_identity
from server.config import get_settings
from server.db import get_db
from server.db.models import AssistantToken, NotificationChannel, NotificationRule
from server.db.models import Session as DbSession
from server.obs.metrics import assistant_feedback_total
from server.sessions.model_policy import resolve_model_policy
from server.sessions.engine import AgentEngineUnavailable
from server.sessions.types import ASSISTANT_KIND
from server.time_iso import iso_utc

from . import watches as watch_conditions
from .policy import (assistant_enabled, assistant_engine, assistant_model,
                     operator_tools_enabled)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/assistant", tags=["assistant"])

# GET /history returns at most this many rows, newest-archived first — no pagination
# (spec: the drawer's "History" list is a short recency list, not a full archive browser).
_HISTORY_LIMIT = 20


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
    except FleetDispatchRefused as e:
        # A fleet edge refuses a dispatch with these three whenever every daemon
        # is restarting, disabled or skew-fenced — routine conditions, but nothing
        # here caught them, so they escaped as an unhandled 500 and the drawer
        # rendered "internal server error" instead of the reason (#2184). Shared
        # with POST /api/sessions rather than re-triaged: identical codes, so the
        # drawer reads them with the New Session dialog's own copy.
        return fleet_refusal_response(e)
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


def _history_dict(row: DbSession) -> dict:
    return {
        "id": row.id,
        "snippet": row.initial_prompt,
        # `archived_at` is the lifecycle fact; `updated_at` (bumped on every row write)
        # is only a fallback for legacy rows archived before that column existed — the
        # same null-safety convention `SessionOrchestrator.list_archived` /
        # `purge_oldest_archived` already apply.
        "archivedAt": iso_utc(row.archived_at or row.updated_at),
    }


@router.get("/history")
async def history(identity: str = Depends(current_identity), db=Depends(get_db)) -> list[dict]:
    """The caller's own archived assistant threads, most-recently-archived first,
    capped at `_HISTORY_LIMIT` (the drawer's "History" list). Identity-scoped in the
    WHERE clause exactly like `list_tokens`/`list_watches` above — never widened to
    another identity's threads."""
    order_key = sa_func.coalesce(DbSession.archived_at, DbSession.updated_at)
    rows = (await db.execute(
        select(DbSession)
        .where(DbSession.kind == ASSISTANT_KIND, DbSession.owner_identity == identity,
               DbSession.status == "archived")
        .order_by(order_key.desc())
        .limit(_HISTORY_LIMIT)
    )).scalars().all()
    return [_history_dict(r) for r in rows]


@router.post("/session/{session_id}/resume")
async def resume(session_id: str, request: Request,
                 identity: str = Depends(current_identity), db=Depends(get_db)):
    """Resume an archived assistant thread from the History list as the live one.

    Mirrors `create`'s archive/identity-scoping logic rather than reinventing it: the
    current live thread (if any, and not already `session_id`) is archived exactly as
    a `fresh` reset archives it, then `session_id` is restored via `orch.unarchive`
    (a pure status flip — the worktree never moved) and its driver is ensured under
    the CALLER's identity, not SYSTEM_IDENTITY (see `ensure_driver`'s docstring: a
    driver resumed under the wrong identity rejects every answer, #1080).
    """
    if getattr(request.state, "via", None) == "assistant":
        return _err("ASSISTANT_RECURSION", "the assistant cannot resume assistants", 403)
    if not await assistant_enabled(db):
        return _err("ASSISTANT_DISABLED", "enable the assistant in Settings → General", 404)

    orch = request.app.state.sessions
    engine = assistant_engine()

    # Identity + kind scoped in the WHERE clause (never fetched-then-checked), so a
    # foreign or non-assistant id answers identically to a nonexistent one — the same
    # cross-identity 404 (never 403) `revoke_token` above already uses, to avoid
    # confirming the row exists.
    row = (await db.execute(
        select(DbSession).where(
            DbSession.id == session_id, DbSession.kind == ASSISTANT_KIND,
            DbSession.owner_identity == identity)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "assistant thread not found")

    existing = await orch.find_assistant(identity)
    if existing is not None and existing.id == session_id:
        # Already the live thread — no-op, just report current state.
        return {"id": existing.id, "model": existing.model or await assistant_model(db),
                "engine": (existing.engine.value if existing.engine else engine.value),
                "state": "existing"}

    if row.status != "archived":
        # Neither the live thread nor an archived one (e.g. a stray "ended" row
        # that was reaped but never archived) — nothing this endpoint can resume.
        raise HTTPException(404, "assistant thread not found")

    if existing is not None:
        await orch.archive_session(existing.id, archived_by="user",
                                   context={"reason": "assistant-resume"})
        await record_audit(db, "assistant.session.reset", identity, {"sessionId": existing.id})
        await db.commit()

    restored = await orch.unarchive(session_id)
    if restored is None:
        raise HTTPException(404, "assistant thread not found")
    await orch.ensure_driver(restored.id, identity=identity)
    await record_audit(db, "assistant.session.resume", identity, {"sessionId": restored.id})
    await db.commit()
    return {"id": restored.id, "model": restored.model or await assistant_model(db),
            "engine": (restored.engine.value if restored.engine else engine.value),
            "state": "resumed"}


class TokenCreateBody(BaseModel):
    scope: str = "read"
    ttlDays: int = 30
    label: str | None = None


def _token_dict(row: AssistantToken) -> dict:
    return {
        "id": row.id,
        "scope": row.scope,
        "label": row.label,
        "createdAt": row.created_at.isoformat(),
        "expiresAt": row.expires_at.isoformat(),
        "revokedAt": row.revoked_at.isoformat() if row.revoked_at else None,
        "lastUsedAt": row.last_used_at.isoformat() if row.last_used_at else None,
    }


@router.post("/tokens", status_code=status.HTTP_201_CREATED)
async def create_token(body: TokenCreateBody, request: Request,
                       identity: str = Depends(current_identity), db=Depends(get_db)):
    """Mint a user-scoped external `ccas2:` token (spec v2 §5/§7, Task 9.5). Browser
    identity only: an already-issued assistant token (`via` set, whether `ccas1:` or
    `ccas2:`) must never be able to mint another — this route is also unreachable via
    either bearer scheme regardless, since neither appears in `ASSISTANT_ROUTE_ALLOW`,
    but the explicit check keeps that true even if a future allowlist edit forgets it."""
    if getattr(request.state, "via", None):
        return _err("ASSISTANT_RECURSION", "an assistant token cannot mint another token", 403)
    if body.scope not in ("read", "act"):
        return _err("INVALID_SCOPE", "scope must be 'read' or 'act'", 422)
    max_days = get_settings().assistant_token_max_days
    if not (0 < body.ttlDays <= max_days):
        return _err("INVALID_TTL", f"ttlDays must be between 1 and {max_days}", 422)
    token_id = str(uuid.uuid4())
    token = mint_user_token(identity, body.scope, token_id)
    row = AssistantToken(
        id=token_id, identity=identity, scope=body.scope,
        token_hash=hashlib.sha256(token.encode()).hexdigest(),
        expires_at=datetime.now(timezone.utc) + timedelta(days=body.ttlDays),
        label=body.label)
    db.add(row)
    await record_audit(db, "assistant.token.create", identity,
                       {"id": token_id, "scope": body.scope, "ttlDays": body.ttlDays,
                        "label": body.label})
    await db.commit()
    return {**_token_dict(row), "token": token}


@router.get("/tokens")
async def list_tokens(identity: str = Depends(current_identity), db=Depends(get_db)):
    rows = (await db.execute(
        select(AssistantToken).where(AssistantToken.identity == identity)
        .order_by(AssistantToken.created_at.desc())
    )).scalars().all()
    return [_token_dict(r) for r in rows]


@router.delete("/tokens/{token_id}")
async def revoke_token(token_id: str, identity: str = Depends(current_identity),
                       db=Depends(get_db)):
    row = (await db.execute(
        select(AssistantToken).where(AssistantToken.id == token_id,
                                     AssistantToken.identity == identity)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "token not found")
    if row.revoked_at is None:
        row.revoked_at = datetime.now(timezone.utc)
        await record_audit(db, "assistant.token.revoke", identity, {"id": token_id})
        await db.commit()
    return {"ok": True}


class FeedbackBody(BaseModel):
    sessionId: str
    turnId: str
    vote: Literal["up", "down"]
    note: str | None = None


@router.post("/feedback")
async def feedback(body: FeedbackBody, request: Request,
                   identity: str = Depends(current_identity), db=Depends(get_db)):
    """Per-turn thumbs feedback on the assistant drawer (spec v2 §4.10, Task 10.1).

    Self-recursion guarded like `create`/`resume`/`create_token` above, even though
    this route is also absent from `ASSISTANT_ROUTE_ALLOW` (so a `ccas1:` bearer is
    already 403'd by the middleware before reaching here) — the assistant voting on
    its own turns makes no sense regardless of which layer catches it.
    """
    if getattr(request.state, "via", None) == "assistant":
        return _err("ASSISTANT_RECURSION", "the assistant cannot vote on its own turns", 403)
    detail: dict[str, str] = {"sessionId": body.sessionId, "turnId": body.turnId, "vote": body.vote}
    if body.note:
        detail["note"] = body.note
    await record_audit(db, "assistant.feedback", identity, detail)
    await db.commit()
    assistant_feedback_total.labels(body.vote).inc()
    return {"ok": True}


class WatchBody(BaseModel):
    kind: str
    target: str
    condition: str
    once: bool = True
    ttl_hours: int = 72


def _assistant_channel_url(identity: str) -> str:
    """The one `NotificationChannel(kind="assistant")` per identity that watches route
    through — deterministic from the identity so create/list/delete always find the
    same row without persisting the identity anywhere new (spec v2 §4.7)."""
    return "assistant:" + hashlib.sha256(identity.encode()).hexdigest()[:16]


async def _find_assistant_channel(db, identity: str) -> NotificationChannel | None:
    return (await db.execute(
        select(NotificationChannel).where(
            NotificationChannel.url == _assistant_channel_url(identity))
    )).scalar_one_or_none()


async def _get_or_create_assistant_channel(db, identity: str) -> NotificationChannel:
    channel = await _find_assistant_channel(db, identity)
    if channel is None:
        channel = NotificationChannel(
            name=f"assistant:{identity}", kind="assistant",
            url=_assistant_channel_url(identity), enabled=True)
        db.add(channel)
        await db.flush()
    return channel


def _watch_dict(r: NotificationRule) -> dict:
    return {
        "id": r.id,
        "eventClass": r.event_class,
        "scopeRepo": r.scope_repo,
        "scopeAutomation": r.scope_automation,
        "scopeAccount": r.scope_account,
        "scopeSessionId": r.scope_session_id,
        "once": bool(r.once),
        "ownerSessionId": r.owner_session_id,
        "expiresAt": r.expires_at.isoformat() if r.expires_at else None,
        "createdAt": r.created_at.isoformat() if r.created_at else None,
    }


# A kind="account" watch's target sentinel for "every account", not one in particular
# (the "Notify me if any account gets limited" starter has no specific account to name).
# Mapped to a NULL scope_account below — the wildcard `scope_matches` (notify_rules.py)
# already gives any None scope field, rather than inventing a second wildcard convention
# alongside event_class's "*".
_ACCOUNT_WILDCARD_TARGETS: frozenset[str] = frozenset({"any", "*", "all"})


@router.post("/watches", status_code=status.HTTP_201_CREATED)
async def create_watch(body: WatchBody, request: Request,
                       identity: str = Depends(current_identity), db=Depends(get_db)):
    """Create a proactive watch (spec v2 §4.7, Task 8.3 + #1354): a `NotificationRule`
    scoped to this identity's `assistant` channel, delivered into the drawer instead of
    an outbound webhook. `kind`/`condition` are resolved against real, currently-emitted
    event classes only — see `server/assistant/watches.py` for the full kind/condition
    map."""
    try:
        event_class, scope_field = watch_conditions.resolve(body.kind, body.condition)
    except watch_conditions.UnknownWatchCondition as e:
        return _err("WATCH_CONDITION_UNSUPPORTED", str(e), 422)
    if body.ttl_hours <= 0:
        return _err("INVALID_TTL", "ttl_hours must be positive", 422)

    channel = await _get_or_create_assistant_channel(db, identity)
    existing = (await db.execute(
        select(NotificationRule.id).where(NotificationRule.channel_id == channel.id)
    )).scalars().all()
    cap = get_settings().assistant_watch_max_per_identity
    if len(existing) >= cap:
        return _err("WATCH_CAP_EXCEEDED", f"at most {cap} watches per identity", 409)

    scope_value: str | None = body.target
    if body.kind == "account" and body.target.strip().lower() in _ACCOUNT_WILDCARD_TARGETS:
        scope_value = None
    rule = NotificationRule(
        event_class=event_class, action="channel", channel_id=channel.id,
        once=body.once, owner_session_id=getattr(request.state, "assistant_session_id", None),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=body.ttl_hours),
        **{scope_field: scope_value},
    )
    db.add(rule)
    await db.flush()
    await record_audit(db, "assistant.watch.create", identity,
                       {"id": rule.id, "kind": body.kind, "condition": body.condition,
                        "target": body.target, "once": body.once})
    await db.commit()
    return _watch_dict(rule)


@router.get("/watches")
async def list_watches(identity: str = Depends(current_identity), db=Depends(get_db)) -> list[dict]:
    channel = await _find_assistant_channel(db, identity)
    if channel is None:
        return []
    rows = (await db.execute(
        select(NotificationRule).where(NotificationRule.channel_id == channel.id)
        .order_by(NotificationRule.created_at)
    )).scalars().all()
    return [_watch_dict(r) for r in rows]


@router.delete("/watches/{watch_id}")
async def delete_watch(watch_id: str, identity: str = Depends(current_identity),
                       db=Depends(get_db)) -> dict:
    channel = await _find_assistant_channel(db, identity)
    if channel is not None:
        rule = (await db.execute(
            select(NotificationRule).where(
                NotificationRule.id == watch_id, NotificationRule.channel_id == channel.id)
        )).scalar_one_or_none()
        if rule is not None:
            await db.delete(rule)
            await db.commit()
            return {"ok": True}
    raise HTTPException(404, "watch not found")
