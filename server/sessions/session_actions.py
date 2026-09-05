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

log = logging.getLogger(__name__)

_QUESTION_MODES = ("option", "freeform", "chat")


class ActionError(Exception):
    """Raised by a session action when it cannot complete; carries enough for both
    a WS error frame and an HTTP error response."""

    def __init__(self, code: str, message: str, http: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http = http


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


async def resolve_permission(
    orch, db, *, session_id: str, perm_id: str, decision: str, identity: str,
    via: str | None = None,
) -> dict:
    """Answer a pending permission card. ``decision`` is one of allow/deny/always;
    ``"always"`` may never reach here from the assistant token — a model must never
    be able to widen a grant (spec §4.4)."""
    if decision == "always" and via == "assistant":
        raise ActionError(
            "ALWAYS_FORBIDDEN_VIA_ASSISTANT",
            "The assistant may not grant an always-allow permission.",
            400,
        )
    ok = await orch.resolve_permission(session_id, perm_id, decision, identity)
    await orch.note_user_interaction(session_id)
    await _audit(db, "permission." + decision, identity,
                 {"sessionId": session_id, "permId": perm_id}, via)
    if not ok:
        raise ActionError(
            "PERMISSION_NOT_PENDING",
            "That permission request is no longer pending — the session may have "
            "moved on. Reload to see its current state.",
            404,
        )
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
    rec = await orch.get_session(session_id)
    if rec is not None and is_assistant(rec):
        from server.sessions.model_policy import resolve_model_policy  # noqa: PLC0415
        if await resolve_model_policy(db, model=model) != "allow":
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
