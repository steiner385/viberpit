"""One implementation per WS-only session action, reachable over both WebSocket and HTTP.

Plan: docs/superpowers/plans/2026-09-04-ai-assistant.md, Task 3.1. Spec:
docs/superpowers/specs/2026-09-04-ai-assistant-design.md §4.2, §4.3, §7.

Each function below is the body that used to live inline in a `server/bridge/ws.py`
message-type branch: same orchestrator call (same args, same order), same
``record_audit`` call (now carrying ``via`` — non-None only for assistant-token
callers), wrapped in the same try/except-log-never-raise pattern the WS code used.
``server/bridge/ws.py`` and ``server/api/sessions.py`` both call these helpers and
each render ``ActionError`` in their own wire format (WS error frame / HTTP status).
"""
from __future__ import annotations

import logging
from typing import Any

from server.audit import record_audit
from server.config import get_settings
from server.sessions.types import SYSTEM_IDENTITY

log = logging.getLogger(__name__)

_QUESTION_MODES = ("option", "freeform", "chat")


class ActionError(Exception):
    """Raised by a session action when it cannot complete; carries enough for both
    a WS error frame and an HTTP error response."""

    def __init__(self, code: str, message: str, http: int,
                 extra: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http = http
        self.extra = extra  # merged into the HTTP ``detail`` by the route layer


async def _audit(db: Any, type_: str, identity: str, detail: dict, via: str | None) -> None:
    """Best-effort audit write — an audit failure must never fail the action."""
    try:
        await record_audit(db, type_, identity, detail, via=via)
    except Exception:
        log.warning("session_actions.audit_failed event=%s detail=%s", type_, detail,
                    exc_info=True)


async def interrupt(orch, db, *, session_id: str, identity: str, via: str | None = None) -> dict:
    """Stop the session's in-flight turn."""
    await orch.interrupt(session_id)
    await _audit(db, "session.interrupt", identity, {"sessionId": session_id}, via)
    return {"ok": True}


async def stop_subagent(orch, db, *, session_id: str, tool_use_id: str, identity: str,
                        via: str | None = None) -> dict:
    """Stop one running Task sub-agent (spec 2026-09-11 §4.4). 404 when it is not
    running: the button only renders while the thread reports running, so this is a
    race with the sub-agent finishing, not a path a user can take on purpose."""
    task_id = await orch.stop_subagent(session_id, tool_use_id)
    if task_id is None:
        raise ActionError(
            "SUBAGENT_NOT_RUNNING",
            "That task is not running — it may have already finished.",
            404,
        )
    await _audit(db, "session.subagent_stop", identity,
                 {"sessionId": session_id, "toolUseId": tool_use_id, "taskId": task_id}, via)
    return {"ok": True, "taskId": task_id}


async def mcp_reconnect(orch, db, *, session_id: str, server: str, identity: str,
                        via: str | None = None) -> dict:
    """Reconnect one MCP server inside a live session (spec 2026-10-09 §4)."""
    try:
        servers = await orch.mcp_reconnect(session_id, server)
    except LookupError:
        raise ActionError("SESSION_NOT_LIVE", "This session is not running.", 404) from None
    except ValueError as exc:
        raise ActionError("MCP_RECONNECT_FAILED", str(exc)[:300], 409) from exc
    await _audit(db, "session.mcp_reconnect", identity,
                 {"sessionId": session_id, "server": server}, via)
    return {"servers": servers}


async def mcp_toggle(orch, db, *, session_id: str, server: str, enabled: bool, identity: str,
                     via: str | None = None) -> dict:
    """Enable/disable one MCP server inside a live session and persist the override."""
    try:
        servers = await orch.mcp_toggle(session_id, server, enabled)
    except LookupError:
        raise ActionError("SESSION_NOT_LIVE", "This session is not running.", 404) from None
    except ValueError as exc:
        raise ActionError("MCP_TOGGLE_FAILED", str(exc)[:300], 409) from exc
    await _audit(db, "session.mcp_toggle", identity,
                 {"sessionId": session_id, "server": server, "enabled": enabled}, via)
    return {"servers": servers}


RULE_HUMAN_ONLY_CODE = "RULES_HUMAN_ONLY"


def require_human(identity: str, via: str | None) -> None:
    """Standing rules widen what a session may do unattended, so only a person at the UI
    may create, edit, revoke or preview one: no token-borne caller (assistant, external
    MCP, CLI, internal, session) and not the system sentinel (spec §4)."""
    if via is not None or identity == SYSTEM_IDENTITY:
        raise ActionError(
            RULE_HUMAN_ONLY_CODE, "Only a person in the UI can manage always-allow rules.", 403)


def rules_enabled() -> bool:
    return bool(get_settings().permission_rules_enabled)


def require_rules_enabled() -> None:
    if not rules_enabled():
        raise ActionError("RULES_DISABLED", "Always-allow rules are turned off.", 404)


async def _permission_refused(orch, session_id: str, perm_id: str) -> ActionError:
    """Why ``orch.resolve_permission`` returned False: still pending under another
    identity (403) vs. gone (404)."""
    if any(p.get("permId") == perm_id for p in await orch.pending_permissions(session_id)):
        return ActionError(
            "PERMISSION_NOT_OWNED",
            "That permission request is still pending, but it was raised under a "
            "different identity than yours, so your decision was refused. A chat "
            "message will deny it with your reply.",
            403,
        )
    return ActionError(
        "PERMISSION_NOT_PENDING",
        "That permission request is no longer pending — the session may have "
        "moved on. Reload to see its current state.",
        404,
    )


async def session_facts(orch, db, session_id: str) -> tuple[str | None, str | None]:
    """``(repo, cwd)`` for a session: the live record first (the only source for a session
    on another host), the DB row as fallback."""
    from server.sessions.facade import _maybe_await  # noqa: PLC0415

    rec = await _maybe_await(orch.get_session(session_id))
    if rec is not None:
        return rec.repo, rec.cwd
    from server.db.models import Session  # noqa: PLC0415

    row = await db.get(Session, session_id)
    if row is None:
        raise ActionError("SESSION_NOT_FOUND", "No such session.", 404)
    return row.repo, row.cwd


def check_pattern_for_scope(pattern: str, scope: str, confirm: bool) -> None:
    """Safety verdict for a rule write: refuse -> 400, warn/global without confirm -> 409."""
    from server.permission_rules.safety import assess, needs_confirmation  # noqa: PLC0415

    a = assess(pattern, scope)
    if a.level == "refuse":
        raise ActionError("RULE_REFUSED", "That rule is too broad or unsafe to save.", 400,
                          {"reasons": list(a.reasons)})
    if needs_confirmation(a, scope) and not confirm:
        raise ActionError("RULE_NEEDS_CONFIRMATION", "This rule needs your confirmation.", 409,
                          {"level": a.level, "reasons": list(a.reasons)})


async def _sweep_candidates(db, session_id: str, scope: str,
                            repo: str | None, cwd: str | None) -> list[tuple[str, str | None]]:
    if scope == "session":
        return [(session_id, cwd)]
    from sqlalchemy import select  # noqa: PLC0415

    from server.db.models import Session  # noqa: PLC0415

    q = select(Session.id, Session.cwd).where(Session.status == "waiting_input")
    if scope == "repo":
        q = q.where(Session.repo == repo)
    rows = (await db.execute(q)).all()
    return [(sid, c) for sid, c in rows]


async def grant_always(
    orch, db, *, session_id: str, perm_id: str, pattern: str, scope: str, confirm: bool,
    identity: str, via: str | None,
) -> dict:
    """Create a standing rule from a pending card, resolve that card, and resolve every other
    pending card (this session / its repo / anywhere, by scope) the new rule covers
    (spec §3.3, §5)."""
    from server.permission_rules import store  # noqa: PLC0415
    from server.permission_rules.patterns import PatternError, matches_any, parse  # noqa: PLC0415
    from server.permission_rules.safety import SCOPES  # noqa: PLC0415

    require_human(identity, via)
    require_rules_enabled()
    if scope not in SCOPES:
        raise ActionError("INVALID_SCOPE", f"Scope must be one of {', '.join(SCOPES)}.", 400)
    card = next((p for p in await orch.pending_permissions(session_id)
                 if p["permId"] == perm_id), None)
    if card is None:
        raise ActionError("PERMISSION_NOT_PENDING",
                          "That permission request is no longer pending.", 404)
    if not card.get("always_allowed", True) or card.get("engine_managed"):
        raise ActionError("RULE_NOT_ALLOWED",
                          "This request cannot be turned into an always-allow rule.", 400)
    repo, cwd = await session_facts(orch, db, session_id)
    if scope == "repo" and not repo:
        raise ActionError("NO_REPO", "This session has no repository to scope a rule to.", 400)
    scope_key = {"session": session_id, "repo": repo, "global": None}[scope]
    check_pattern_for_scope(pattern, scope, confirm)
    try:
        parsed = parse(pattern)
    except PatternError:  # assess() already refused anything unparsable; defensive
        raise ActionError("RULE_REFUSED", "That pattern is not valid.", 400) from None
    if matches_any([parsed], card["tool"], card["input"], cwd) is None:
        raise ActionError("PATTERN_DOES_NOT_COVER",
                          "That pattern does not cover the request being approved.", 400)

    rule = await store.add_rule(db, scope=scope, scope_key=scope_key, pattern=pattern,
                                created_by=identity, created_via=via)
    await record_audit(db, "permission_rule.create", identity,
                       {"ruleId": rule.id, "pattern": pattern, "scope": scope,
                        "scopeKey": scope_key, "sessionId": session_id, "permId": perm_id},
                       via=via)
    await db.commit()  # durable before any card resolves: a crash mid-sweep keeps the rule

    ok = await orch.resolve_permission(session_id, perm_id, "allow", identity)
    await orch.note_user_interaction(session_id)
    if not ok:
        raise await _permission_refused(orch, session_id, perm_id)
    await _audit(db, "permission.rule_allow", identity,
                 {"sessionId": session_id, "permId": perm_id, "ruleId": rule.id,
                  "pattern": pattern, "trigger": "grant"}, via)

    resolved = [perm_id]
    for sid, sid_cwd in await _sweep_candidates(db, session_id, scope, repo, cwd):
        try:
            for other in await orch.pending_permissions(sid):
                if sid == session_id and other["permId"] == perm_id:
                    continue
                if not other.get("always_allowed", True):
                    continue
                if matches_any([parsed], other["tool"], other["input"], sid_cwd) is None:
                    continue
                if await orch.resolve_permission(sid, other["permId"], "allow", identity):
                    resolved.append(other["permId"])
                    await _audit(db, "permission.rule_allow", identity,
                                 {"sessionId": sid, "permId": other["permId"],
                                  "ruleId": rule.id, "pattern": pattern, "trigger": "grant"},
                                 via)
        except Exception:
            log.warning("grant_always: sweep of session %s failed", sid, exc_info=True)
    return {"rule": rule.to_wire(), "resolvedPermIds": resolved}


async def resolve_permission(
    orch, db, *, session_id: str, perm_id: str, decision: str, identity: str,
    via: str | None = None,
) -> dict:
    """Answer a pending permission card. ``decision`` is one of allow/deny/always.

    ``"always"`` is human-only (spec §4): the assistant keeps its historical 400, every
    other token-borne caller gets ``RULES_HUMAN_ONLY``. For a human it creates a
    session-scope rule from the first ``ok`` suggestion and allows the card; an
    engine-managed (Codex) card, or a card that is not pending, passes ``"always"`` through
    unchanged (Codex's acceptForSession / today's 404-403 handling); with the rules kill
    switch off or no ``ok`` suggestion it degrades to a plain ``"allow"``."""
    if decision == "always":
        if via == "assistant":
            raise ActionError(
                "ALWAYS_FORBIDDEN_VIA_ASSISTANT",
                "The assistant may not grant an always-allow permission.",
                400,
            )
        require_human(identity, via)
        card = next((p for p in await orch.pending_permissions(session_id)
                     if p.get("permId") == perm_id), None)
        if card is not None and not card.get("engine_managed"):
            if not rules_enabled():
                decision = "allow"
            else:
                best = next((s for s in card.get("suggestions") or []
                             if s.get("level") == "ok"), None)
                if best is None:
                    decision = "allow"
                else:
                    await grant_always(
                        orch, db, session_id=session_id, perm_id=perm_id,
                        pattern=best["pattern"], scope="session", confirm=False,
                        identity=identity, via=via)
                    return {"ok": True}
    ok = await orch.resolve_permission(session_id, perm_id, decision, identity)
    await orch.note_user_interaction(session_id)
    await _audit(db, "permission." + decision, identity,
                 {"sessionId": session_id, "permId": perm_id}, via)
    if not ok:
        raise await _permission_refused(orch, session_id, perm_id)
    return {"ok": True}


async def answer_question(
    orch, db, *, session_id: str, question_id: str, selected: list, mode: str,
    identity: str, via: str | None = None,
) -> dict:
    """Answer a pending AskUserQuestion."""
    if mode not in _QUESTION_MODES:
        mode = "option"
    ok = await orch.answer_question(session_id, question_id, selected, identity, mode)
    await orch.note_user_interaction(session_id)
    await _audit(db, "question.answer", identity,
                 {"sessionId": session_id, "questionId": question_id,
                  "selected": selected, "mode": mode}, via)
    if not ok:
        # The registry refuses BOTH an unknown id and an answer from an identity
        # other than the one the ask was registered under. Telling those apart is
        # what stops a still-pending card from being reported as "no longer
        # pending" and sending the user to reload a page that re-offers it
        # (2026-09-22, session 75994c11: every answer refused, card still live).
        if any(q.get("question_id") == question_id
               for q in await orch.pending_questions(session_id)):
            raise ActionError(
                "QUESTION_NOT_OWNED",
                "That question is still pending, but it was asked under a different "
                "identity than yours, so this answer was refused. Send a chat message "
                "to answer it instead.",
                403,
            )
        raise ActionError(
            "QUESTION_NOT_PENDING",
            "That question is no longer pending — the session may have moved on. "
            "Reload to see its current state.",
            404,
        )
    return {"ok": True}


async def set_permission_mode(
    orch, db, *, session_id: str, mode: str, identity: str, via: str | None = None,
) -> dict:
    """Change the session's permission mode."""
    await orch.set_permission_mode(session_id, mode)
    await _audit(db, "permission_mode.change", identity,
                 {"sessionId": session_id, "mode": mode}, via)
    return {"ok": True}


async def set_model(
    orch, db, *, session_id: str, model: str | None, when: str = "now",
    identity: str, via: str | None = None,
) -> dict:
    """Switch the session's model (→ ``set_model_for_session``). Also the call site
    ``orchestrator.run_command``'s WS `/model` slash command routes through (passing
    its own ``when``) — the notice-string / after_turn composition for that command
    stays in ``run_command``, reading this call's ``restarted`` result; this helper
    owns the single apply-and-audit path both entry points share."""
    from server.sessions.orchestrator import ModelBlockedError  # noqa: PLC0415
    from server.sessions.types import is_assistant  # noqa: PLC0415

    # A gated model must never PARK the AI-drawer session: `set_model_for_session`
    # would file a model-approval hold, and the card that clears it renders inside the
    # very drawer that just stopped answering — an assistant no one can un-park. Refuse
    # up front instead, with the same MODEL_GATED code POST /api/assistant/session
    # already returns when it creates one (server/assistant/api.py).
    #
    # Sits here rather than in the route so the WS `/model` slash command, which reaches
    # this same helper via run_command, is gated too.
    #
    # Asks "is this the assistant?" BEFORE consulting policy, so an ordinary session
    # never pays a policy read this function does not use — `set_model_for_session`
    # re-runs `resolve_model_policy` itself, and doing it here first would just be the
    # same query twice on every model switch.
    #
    # `_maybe_await` because `orch` is TWO different things here: the HTTP route hands
    # in the facade / RemoteOrchestrator, whose `get_session` is async, while the WS
    # `/model` slash command reaches this from inside `orchestrator.run_command` and
    # hands in the raw SessionOrchestrator, whose `get_session` is sync. Awaiting the
    # raw one raises "object SessionRecord can't be used in 'await' expression".
    from server.sessions.facade import _maybe_await  # noqa: PLC0415

    rec = await _maybe_await(orch.get_session(session_id))
    if rec is not None and is_assistant(rec):
        from server.sessions.model_policy import launch_model, resolve_model_policy  # noqa: PLC0415
        if await resolve_model_policy(db, model=launch_model(model)) != "allow":
            raise ActionError(
                "MODEL_GATED", f"{model} needs approval and cannot run the assistant", 400,
            )

    try:
        restarted = await orch.set_model_for_session(session_id, model, when=when,
                                                       identity=identity)
    except ModelBlockedError:
        raise ActionError(
            "MODEL_BLOCKED", f"{model} is blocked by policy", 409,
        )
    await _audit(db, "session.model_change", identity,
                 {"sessionId": session_id, "model": model}, via)
    return {"ok": True, "restarted": restarted}
