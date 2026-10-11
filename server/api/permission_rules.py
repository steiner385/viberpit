"""Standing always-allow rules: grant-from-card, preview, list, edit, revoke.

Spec 2026-10-11-always-allow-permission-rules §3.3, §3.4, §4. Every write is human-only
(``session_actions.require_human``) and the assistant route allowlist deliberately omits all of
these. Every route 404s while ``permission_rules_enabled`` is off. The business logic lives in
``server.sessions.session_actions``; this module only maps ``ActionError`` to HTTP.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel

from server.api.sessions import _action_error_response, _get_orchestrator
from server.audit import record_audit
from server.auth.middleware import current_identity
from server.db import get_db
from server.permission_rules import store
from server.permission_rules.patterns import PatternError, describe, matches_any, parse
from server.permission_rules.safety import SCOPES, assess, needs_confirmation
from server.sessions import session_actions
from server.sessions.session_actions import ActionError

router = APIRouter(tags=["permission-rules"])


class AlwaysRequest(BaseModel):
    pattern: str
    scope: str
    confirm: bool = False


class PreviewRequest(BaseModel):
    sessionId: str
    permId: str | None = None
    pattern: str
    scope: str


class RulePatch(BaseModel):
    pattern: str
    confirm: bool = False


def _via(request: Request) -> str | None:
    return getattr(request.state, "via", None)


@router.post("/api/sessions/{session_id}/permissions/{perm_id}/always")
async def grant_always(session_id: str, perm_id: str, body: AlwaysRequest, request: Request,
                       identity: str = Depends(current_identity), db=Depends(get_db)):
    try:
        return await session_actions.grant_always(
            _get_orchestrator(request), db, session_id=session_id, perm_id=perm_id,
            pattern=body.pattern, scope=body.scope, confirm=body.confirm,
            identity=identity, via=_via(request))
    except ActionError as exc:
        return _action_error_response(exc)


@router.post("/api/permission-rules/preview")
async def preview_rule(body: PreviewRequest, request: Request,
                       identity: str = Depends(current_identity), db=Depends(get_db)):
    try:
        session_actions.require_human(identity, _via(request))
        session_actions.require_rules_enabled()
        if body.scope not in SCOPES:
            raise ActionError("INVALID_SCOPE", f"Scope must be one of {', '.join(SCOPES)}.", 400)
        orch = _get_orchestrator(request)
        _repo, cwd = await session_actions.session_facts(orch, db, body.sessionId)
    except ActionError as exc:
        return _action_error_response(exc)
    a = assess(body.pattern, body.scope)
    try:
        parsed = parse(body.pattern)
    except PatternError:
        return {"valid": False, "level": a.level, "reasons": list(a.reasons),
                "description": None, "needsConfirmation": False,
                "coversRequest": None, "alsoPending": 0}
    covers: bool | None = None
    also = 0
    for card in await orch.pending_permissions(body.sessionId):
        hit = matches_any([parsed], card["tool"], card["input"], cwd) is not None
        if card["permId"] == body.permId:
            covers = hit
        elif hit:
            also += 1
    return {"valid": True, "level": a.level, "reasons": list(a.reasons),
            "description": describe(body.pattern),
            "needsConfirmation": needs_confirmation(a, body.scope),
            "coversRequest": covers, "alsoPending": also}


@router.get("/api/permission-rules")
async def list_rules(request: Request, sessionId: str | None = Query(default=None),
                     _identity: str = Depends(current_identity), db=Depends(get_db)):
    try:
        session_actions.require_rules_enabled()
        if sessionId is None:
            rules = await store.list_rules(db)
        else:
            repo, _cwd = await session_actions.session_facts(
                _get_orchestrator(request), db, sessionId)
            rules = await store.applicable_rules(db, sessionId, repo)
    except ActionError as exc:
        return _action_error_response(exc)
    return [r.to_wire() for r in rules]


@router.patch("/api/permission-rules/{rule_id}")
async def update_rule(rule_id: str, body: RulePatch, request: Request,
                      identity: str = Depends(current_identity), db=Depends(get_db)):
    via = _via(request)
    try:
        session_actions.require_human(identity, via)
        session_actions.require_rules_enabled()
        rule = await store.get_rule(db, rule_id)
        if rule is None:
            raise ActionError("RULE_NOT_FOUND", "No such rule.", 404)
        session_actions.check_pattern_for_scope(body.pattern, rule.scope, body.confirm)
        try:
            updated = await store.update_rule(db, rule_id, new_pattern=body.pattern)
        except ValueError:
            raise ActionError("DUPLICATE_RULE", "That scope already has this rule.", 409) from None
        if updated is None:
            raise ActionError("RULE_NOT_FOUND", "No such rule.", 404)
        await record_audit(db, "permission_rule.update", identity,
                           {"ruleId": rule_id, "from": rule.pattern, "to": body.pattern,
                            "scope": rule.scope, "scopeKey": rule.scope_key}, via=via)
        await db.commit()
    except ActionError as exc:
        return _action_error_response(exc)
    return updated.to_wire()


@router.delete("/api/permission-rules/{rule_id}", status_code=204)
async def revoke_rule(rule_id: str, request: Request,
                      identity: str = Depends(current_identity), db=Depends(get_db)):
    via = _via(request)
    try:
        session_actions.require_human(identity, via)
        session_actions.require_rules_enabled()
        rule = await store.revoke_rule(db, rule_id)
        if rule is None:
            raise ActionError("RULE_NOT_FOUND", "No such rule.", 404)
        await record_audit(db, "permission_rule.revoke", identity,
                           {"ruleId": rule_id, "pattern": rule.pattern, "scope": rule.scope,
                            "scopeKey": rule.scope_key}, via=via)
        await db.commit()
    except ActionError as exc:
        return _action_error_response(exc)
    return Response(status_code=204)
