"""Unit tests for server/sessions/session_actions.py (plan Task 3.1).

Each helper is exercised against a fake orchestrator with AsyncMock methods;
``record_audit`` is patched so we can assert its call shape (type, detail, via)
without touching a real DB.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from server.sessions import session_actions as sa
from server.sessions.orchestrator import ModelBlockedError


class FakeOrch:
    def __init__(self) -> None:
        self.interrupt = AsyncMock()
        self.resolve_permission = AsyncMock(return_value=True)
        self.note_user_interaction = AsyncMock()
        self.answer_question = AsyncMock(return_value=True)
        self.set_permission_mode = AsyncMock()
        self.set_model_for_session = AsyncMock(return_value=False)
        # `set_model` reads the record to decide whether this is the AI-drawer session
        # (a gated model is refused there rather than parked). None = an ordinary
        # session, which is what every test here drives.
        self.get_session = AsyncMock(return_value=None)


@pytest.fixture
def orch() -> FakeOrch:
    return FakeOrch()


def _patched_audit():
    return patch("server.sessions.session_actions.record_audit", new_callable=AsyncMock)


# ---------------------------------------------------------------------------
# interrupt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interrupt_calls_orchestrator_and_records_audit(orch):
    with _patched_audit() as audit:
        result = await sa.interrupt(orch, db=object(), session_id="s1", identity="u@example.com")
    orch.interrupt.assert_awaited_once_with("s1")
    audit.assert_awaited_once()
    args, kwargs = audit.call_args
    assert args[1] == "session.interrupt"
    assert args[2] == "u@example.com"
    assert args[3] == {"sessionId": "s1"}
    assert kwargs["via"] is None
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_interrupt_passes_via_through(orch):
    with _patched_audit() as audit:
        await sa.interrupt(orch, db=object(), session_id="s1", identity="u@example.com",
                            via="assistant")
    assert audit.call_args.kwargs["via"] == "assistant"


# ---------------------------------------------------------------------------
# resolve_permission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_permission_calls_orchestrator_with_correct_args(orch):
    with _patched_audit() as audit:
        result = await sa.resolve_permission(
            orch, db=object(), session_id="s1", perm_id="p1", decision="allow",
            identity="u@example.com")
    orch.resolve_permission.assert_awaited_once_with("s1", "p1", "allow", "u@example.com")
    orch.note_user_interaction.assert_awaited_once_with("s1")
    audit.assert_awaited_once()
    args, kwargs = audit.call_args
    assert args[1] == "permission.allow"
    assert args[3] == {"sessionId": "s1", "permId": "p1"}
    assert kwargs["via"] is None
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_resolve_permission_always_forbidden_via_assistant_before_orchestrator_call(orch):
    with _patched_audit() as audit:
        with pytest.raises(sa.ActionError) as exc_info:
            await sa.resolve_permission(
                orch, db=object(), session_id="s1", perm_id="p1", decision="always",
                identity="u@example.com", via="assistant")
    assert exc_info.value.code == "ALWAYS_FORBIDDEN_VIA_ASSISTANT"
    assert exc_info.value.http == 400
    orch.resolve_permission.assert_not_awaited()
    orch.note_user_interaction.assert_not_awaited()
    audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_permission_always_allowed_for_human(orch):
    with _patched_audit():
        result = await sa.resolve_permission(
            orch, db=object(), session_id="s1", perm_id="p1", decision="always",
            identity="u@example.com", via=None)
    orch.resolve_permission.assert_awaited_once_with("s1", "p1", "always", "u@example.com")
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_resolve_permission_not_pending_raises_after_recording_audit(orch):
    orch.resolve_permission.return_value = False
    with _patched_audit() as audit:
        with pytest.raises(sa.ActionError) as exc_info:
            await sa.resolve_permission(
                orch, db=object(), session_id="s1", perm_id="p1", decision="deny",
                identity="u@example.com")
    assert exc_info.value.code == "PERMISSION_NOT_PENDING"
    assert exc_info.value.http == 404
    # Matches the pre-extraction WS behaviour: audit + note_user_interaction still
    # run even when the decision could not be delivered.
    orch.note_user_interaction.assert_awaited_once_with("s1")
    audit.assert_awaited_once()


# ---------------------------------------------------------------------------
# answer_question
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_answer_question_calls_orchestrator_with_correct_args(orch):
    with _patched_audit() as audit:
        result = await sa.answer_question(
            orch, db=object(), session_id="s1", question_id="q1", selected=["a"],
            mode="option", identity="u@example.com")
    orch.answer_question.assert_awaited_once_with("s1", "q1", ["a"], "u@example.com", "option")
    orch.note_user_interaction.assert_awaited_once_with("s1")
    args, kwargs = audit.call_args
    assert args[1] == "question.answer"
    assert args[3] == {"sessionId": "s1", "questionId": "q1", "selected": ["a"], "mode": "option"}
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_answer_question_clamps_unknown_mode_to_option(orch):
    with _patched_audit() as audit:
        await sa.answer_question(
            orch, db=object(), session_id="s1", question_id="q1", selected=[],
            mode="bogus", identity="u@example.com")
    orch.answer_question.assert_awaited_once_with("s1", "q1", [], "u@example.com", "option")
    assert audit.call_args.args[3]["mode"] == "option"


@pytest.mark.asyncio
async def test_answer_question_not_pending_raises_after_recording_audit(orch):
    orch.answer_question.return_value = False
    with _patched_audit() as audit:
        with pytest.raises(sa.ActionError) as exc_info:
            await sa.answer_question(
                orch, db=object(), session_id="s1", question_id="q1", selected=[],
                mode="option", identity="u@example.com")
    assert exc_info.value.code == "QUESTION_NOT_PENDING"
    assert exc_info.value.http == 404
    orch.note_user_interaction.assert_awaited_once_with("s1")
    audit.assert_awaited_once()


# ---------------------------------------------------------------------------
# set_permission_mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_permission_mode_calls_orchestrator_and_records_audit(orch):
    with _patched_audit() as audit:
        result = await sa.set_permission_mode(
            orch, db=object(), session_id="s1", mode="acceptEdits", identity="u@example.com")
    orch.set_permission_mode.assert_awaited_once_with("s1", "acceptEdits")
    args, kwargs = audit.call_args
    assert args[1] == "permission_mode.change"
    assert args[3] == {"sessionId": "s1", "mode": "acceptEdits"}
    assert result == {"ok": True}


# ---------------------------------------------------------------------------
# set_model
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_model_calls_set_model_for_session_and_records_audit(orch):
    orch.set_model_for_session.return_value = True
    with _patched_audit() as audit:
        result = await sa.set_model(
            orch, db=object(), session_id="s1", model="opus", identity="u@example.com")
    orch.set_model_for_session.assert_awaited_once_with(
        "s1", "opus", when="now", identity="u@example.com")
    args, kwargs = audit.call_args
    assert args[1] == "session.model_change"
    assert args[3] == {"sessionId": "s1", "model": "opus"}
    assert result == {"ok": True, "restarted": True}


@pytest.mark.asyncio
async def test_set_model_blocked_raises_action_error(orch):
    orch.set_model_for_session.side_effect = ModelBlockedError("haiku")
    with _patched_audit() as audit:
        with pytest.raises(sa.ActionError) as exc_info:
            await sa.set_model(
                orch, db=object(), session_id="s1", model="haiku", identity="u@example.com")
    assert exc_info.value.code == "MODEL_BLOCKED"
    assert exc_info.value.http == 409
    audit.assert_not_awaited()


# ---------------------------------------------------------------------------
# audit failure must never raise
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_failure_never_raises(orch):
    with patch("server.sessions.session_actions.record_audit",
               new_callable=AsyncMock, side_effect=RuntimeError("db down")):
        result = await sa.interrupt(orch, db=object(), session_id="s1", identity="u@example.com")
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_set_model_refuses_a_gated_model_for_the_assistant_instead_of_parking(orch):
    """The drawer must never be left parked on a model-approval hold: the card that
    clears one renders inside the drawer the hold just silenced. Gated here, in the
    shared helper, so the WS `/model` command is covered as well as the HTTP route."""
    from server.sessions.types import ASSISTANT_KIND

    orch.get_session = AsyncMock(return_value=SimpleNamespace(kind=ASSISTANT_KIND))
    with patch("server.sessions.session_actions.record_audit", new_callable=AsyncMock), \
         patch("server.sessions.model_policy.resolve_model_policy",
               new_callable=AsyncMock, return_value="require_approval"):
        with pytest.raises(sa.ActionError) as exc:
            await sa.set_model(orch, db=object(), session_id="s1", model="fable",
                               identity="u@example.com")
    assert exc.value.code == "MODEL_GATED" and exc.value.http == 400
    orch.set_model_for_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_model_leaves_an_ordinary_session_to_the_normal_park_path(orch):
    """The gate is assistant-only: a gated model on a normal session still reaches
    set_model_for_session, which parks it for human approval as it always has."""
    orch.get_session = AsyncMock(return_value=SimpleNamespace(kind=None))
    with patch("server.sessions.session_actions.record_audit", new_callable=AsyncMock):
        result = await sa.set_model(orch, db=object(), session_id="s1", model="fable",
                                    identity="u@example.com")
    orch.set_model_for_session.assert_awaited_once()
    assert result == {"ok": True, "restarted": False}
