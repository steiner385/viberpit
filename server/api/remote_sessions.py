"""Remote sessions API (plan 2026-11-25-remote-sessions-reverse): Claude Code Remote Control
sessions started outside ViberPit, on the operator's accounts.

* ``GET  /api/remote-sessions`` — ``{enabled, sessions: [...], accounts: [...]}``.
* ``POST /api/remote-sessions/{session_id}/prompt`` — ``{text}`` -> ``{ok, account}``.
* ``POST /api/remote-sessions/{session_id}/answer`` — a permission (``{askId, decision,
  message?}``) or a question (``{askId, selected, mode?}``) -> ``{ok, sent, account, kind}``.

The live view is the ordinary ``/ws`` attach with session id ``remote:<session_id>``.

Sending a prompt or an answer acts on another machine, so both are person-only (403
``SESSION_CONTROL_HUMAN_ONLY``, honouring the operator's local-host opt-in exactly as the
Remote Control enable does) and audited (``remote_session.prompt`` /
``remote_session.answer``, outcome included; never the prompt text). Refusals carry
``{detail: {code, message}}`` with the codes in ``remote_sessions.service``.
"""
from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictStr

from server.audit import record_audit
from server.auth.identity import human_gate_via
from server.auth.middleware import current_identity
from server.db import get_db
from server.ipc.client import DaemonError, DaemonUnavailable
from server.remote_sessions import service as rs
from server.sessions.session_actions import (SESSION_CONTROL_HUMAN_ONLY_CODE, ActionError,
                                             require_human)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/remote-sessions", tags=["remote-sessions"])


def _error(code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=rs.HTTP_STATUS.get(code, 502),
                        content={"detail": {"code": code, "message": message}})


def _refusal(exc: Exception) -> tuple[str, str]:
    """``(code, message)`` for a failed remote sessions call."""
    if isinstance(exc, rs.RemoteSessionsError):
        return exc.code, str(exc)
    if isinstance(exc, (DaemonUnavailable, DaemonError)):
        log.info("remote sessions: daemon call failed (%s)", type(exc).__name__)
        return rs.UNAVAILABLE, "Remote sessions are unavailable right now."
    raise exc


def _human_gate(request: Request, identity: str, action: str) -> JSONResponse | None:
    try:
        require_human(identity, human_gate_via(request), action=action,
                      code=SESSION_CONTROL_HUMAN_ONLY_CODE, trust_local_host=True)
    except ActionError as exc:
        return JSONResponse(status_code=exc.http,
                            content={"detail": {"code": exc.code, "message": exc.message}})
    return None


@router.get("")
async def list_remote_sessions(request: Request,
                               identity: str = Depends(current_identity)) -> Any:
    """Every Remote Control session on the operator's accounts that ViberPit does not
    run itself. Each: ``{id, account, title, status, connection, machineId, origin, cwd,
    model, url, updatedAt, needsYou, unread}``. With the kill switch off this is
    ``{enabled: false, sessions: [], accounts: []}`` (200)."""
    try:
        return await request.app.state.sessions.remote_sessions_list()
    except (rs.RemoteSessionsError, DaemonUnavailable, DaemonError) as exc:
        return _error(*_refusal(exc))


class PromptRequest(BaseModel):
    text: StrictStr = Field(min_length=1, max_length=rs.MAX_PROMPT_CHARS)


@router.post("/{session_id}/prompt")
async def prompt_remote_session(session_id: str, body: PromptRequest, request: Request,
                                identity: str = Depends(current_identity),
                                db=Depends(get_db)) -> Any:
    """Send *text* into the remote session as a prompt. Person-only; audited."""
    refused = _human_gate(request, identity,
                          "send a prompt to a session running outside ViberPit")
    if refused is not None:
        return refused
    try:
        result = await request.app.state.sessions.remote_session_prompt(session_id, body.text)
    except (rs.RemoteSessionsError, DaemonUnavailable, DaemonError) as exc:
        code, message = _refusal(exc)
        await record_audit(db=db, type="remote_session.prompt", identity=identity,
                           detail={"session_id": session_id, "chars": len(body.text),
                                   "outcome": code})
        return _error(code, message)
    await record_audit(db=db, type="remote_session.prompt", identity=identity,
                       detail={"session_id": session_id, "chars": len(body.text),
                               "account": result.get("account"), "outcome": "sent"})
    return result


class AnswerRequest(BaseModel):
    # A permission card's permId, or a question card's questionId.
    askId: StrictStr = Field(min_length=1, max_length=300)
    decision: Literal["allow", "deny"] | None = None
    message: StrictStr | None = Field(default=None, max_length=10_000)
    selected: list[StrictStr] | None = Field(default=None, max_length=50)
    mode: Literal["option", "freeform", "chat"] = "option"


@router.post("/{session_id}/answer")
async def answer_remote_session(session_id: str, body: AnswerRequest, request: Request,
                                identity: str = Depends(current_identity),
                                db=Depends(get_db)) -> Any:
    """Answer a pending permission or question card of an open remote session.
    Person-only; audited. A multi-question card is sent once every question has an
    answer (``sent: false`` until then)."""
    refused = _human_gate(request, identity,
                          "answer a session running outside ViberPit")
    if refused is not None:
        return refused
    answer: dict[str, Any] = {"mode": body.mode}
    if body.decision is not None:
        answer["decision"] = body.decision
    if body.message is not None:
        answer["message"] = body.message
    if body.selected is not None:
        answer["selected"] = list(body.selected)
    detail: dict[str, Any] = {"session_id": session_id, "ask_id": body.askId,
                              "decision": body.decision}
    try:
        result = await request.app.state.sessions.remote_session_answer(
            session_id, body.askId, **answer)
    except (rs.RemoteSessionsError, DaemonUnavailable, DaemonError) as exc:
        code, message = _refusal(exc)
        await record_audit(db=db, type="remote_session.answer", identity=identity,
                           detail={**detail, "outcome": code})
        return _error(code, message)
    await record_audit(db=db, type="remote_session.answer", identity=identity,
                       detail={**detail, "account": result.get("account"),
                               "kind": result.get("kind"),
                               "outcome": "sent" if result.get("sent") else "held"})
    return result
