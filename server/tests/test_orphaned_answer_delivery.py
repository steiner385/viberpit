"""Answering a restart-orphaned question sends a NEW prompt.

There is no future left to resolve: the daemon's asyncio.Future and the SDK
subprocess that awaited it both died. The only honest delivery is the next turn's
prompt, carrying the selection and disowning the CLI's synthesised rejection.
"""
import asyncio
from datetime import datetime

from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord


def _rec(**over):
    base = dict(id="s1", name="s", account="a", engine=AgentEngine.CLAUDE_CODE, engine_session_id="cs", cols=80, rows=24,
                cwd="/tmp", repo=None, branch=None, status="idle", name_is_auto=True,
                created_at=datetime.utcnow(), updated_at=datetime.utcnow())
    base.update(over)
    return SessionRecord(**base)


def _orch(rec):
    o = SessionOrchestrator.__new__(SessionOrchestrator)
    o._records = {rec.id: rec}
    o._drivers = {}
    return o


def test_orphaned_interaction_reports_the_kind():
    o = _orch(_rec(interrupted_by_restart=True, interrupted_from_status="waiting_question"))
    assert o.orphaned_interaction("s1") == "question"


def test_orphaned_interaction_is_none_for_a_healthy_session():
    assert _orch(_rec()).orphaned_interaction("s1") is None


def test_orphaned_interaction_is_none_for_an_unknown_session():
    assert _orch(_rec()).orphaned_interaction("nope") is None


def test_answering_an_orphaned_question_sends_a_prompt():
    async def run():
        rec = _rec(interrupted_by_restart=True, interrupted_from_status="waiting_question")
        o = _orch(rec)
        sent = []

        async def _ensure(sid, identity=None):
            return object()

        async def _send(sid, text, attachment_ids=None):
            sent.append((sid, text))
            return "sent"

        o.ensure_driver = _ensure
        o.send_prompt = _send
        ok = await o.answer_question("s1", "q-0", ["Close #15205 as duplicate"], "tony", "option")

        assert ok is True
        assert len(sent) == 1
        assert "Close #15205 as duplicate" in sent[0][1]
        assert "restart" in sent[0][1].lower()

    asyncio.run(run())


def test_a_live_driver_still_wins_and_no_prompt_is_sent():
    """The live path must be untouched: orphan handling is strictly a fallback."""
    async def run():
        rec = _rec(interrupted_by_restart=True, interrupted_from_status="waiting_question")
        o = _orch(rec)
        sent = []

        class _D:
            def answer_question(self, qid, selected, identity, mode):
                return True

        o._drivers = {"s1": _D()}

        async def _send(sid, text, attachment_ids=None):
            sent.append(text)
            return "sent"

        o.send_prompt = _send
        assert await o.answer_question("s1", "q1", ["A"], "tony", "option") is True
        assert sent == [], "resolved a live future AND sent a duplicate prompt"

    asyncio.run(run())


def test_a_healthy_session_with_no_pending_question_still_returns_false():
    """An unresolvable answer must stay a rejection — the client shows
    QUESTION_NOT_PENDING. Only a genuinely orphaned session gets the fallback."""
    async def run():
        o = _orch(_rec())
        o._drivers = {}
        assert await o.answer_question("s1", "q1", ["A"], "tony", "option") is False

    asyncio.run(run())


def test_a_failed_send_reports_failure():
    async def run():
        rec = _rec(interrupted_by_restart=True, interrupted_from_status="waiting_question")
        o = _orch(rec)

        async def _ensure(sid, identity=None):
            return object()

        async def _send(sid, text, attachment_ids=None):
            return "draining"

        o.ensure_driver = _ensure
        o.send_prompt = _send
        assert await o.answer_question("s1", "q-0", ["A"], "tony", "option") is False

    asyncio.run(run())


def test_no_driver_reports_failure():
    async def run():
        rec = _rec(interrupted_by_restart=True, interrupted_from_status="waiting_question")
        o = _orch(rec)

        async def _ensure(sid, identity=None):
            return None

        o.ensure_driver = _ensure
        assert await o.answer_question("s1", "q-0", ["A"], "tony", "option") is False

    asyncio.run(run())


def test_an_orphaned_permission_does_not_take_the_question_fallback():
    """A permission cannot be answered with a selection — only re-asked (Task 5)."""
    async def run():
        rec = _rec(interrupted_by_restart=True, interrupted_from_status="waiting_input")
        o = _orch(rec)
        sent = []

        async def _send(sid, text, attachment_ids=None):
            sent.append(text)
            return "sent"

        o.send_prompt = _send
        assert await o.answer_question("s1", "q1", ["A"], "tony", "option") is False
        assert sent == []

    asyncio.run(run())
