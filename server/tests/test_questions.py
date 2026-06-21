"""Tests for QuestionRegistry — mirrors test_permissions.py."""
import asyncio
from server.sessions.questions import QuestionRegistry

# ---------------------------------------------------------------------------
# Helpers — default payload values shared across tests
# ---------------------------------------------------------------------------

_OPTS = [{"label": "Option A"}, {"label": "Option B"}]


def _req(reg, session_id="s1", turn_id="t1", header="H", question="Q?",
         options=None, multi_select=False, identity="me"):
    """Convenience wrapper so existing tests don't spell out every kwarg."""
    return reg.request(
        session_id, turn_id, header, question,
        options if options is not None else _OPTS,
        multi_select, identity,
    )


# ---------------------------------------------------------------------------
# Basic resolve / future semantics
# ---------------------------------------------------------------------------

def test_request_resolve_sets_future_to_selected_list():
    reg = QuestionRegistry()
    async def run():
        qid, fut = _req(reg)
        assert qid is not None
        reg.resolve(qid, ["Option A"], "me")
        return await fut
    result = asyncio.run(run())
    assert result == (["Option A"], "option")


def test_request_resolve_multi_select():
    reg = QuestionRegistry()
    async def run():
        qid, fut = _req(reg, multi_select=True)
        reg.resolve(qid, ["A", "B"], "me")
        return await fut
    result = asyncio.run(run())
    assert result == (["A", "B"], "option")


def test_resolve_wrong_identity_returns_false():
    reg = QuestionRegistry()
    async def run():
        qid, fut = _req(reg, identity="owner")
        result = reg.resolve(qid, ["X"], "attacker")
        return result, fut
    result, fut = asyncio.run(run())
    assert result is False
    assert not fut.done()


def test_resolve_unknown_id_returns_false():
    reg = QuestionRegistry()
    async def run():
        _req(reg)
        return reg.resolve("nonexistent-id", ["X"], "me")
    assert asyncio.run(run()) is False


def test_resolve_already_done_returns_false():
    """Resolving the same question twice is idempotent — second call returns False."""
    reg = QuestionRegistry()
    async def run():
        qid, fut = _req(reg)
        first = reg.resolve(qid, ["A"], "me")
        await fut  # consume the result
        second = reg.resolve(qid, ["A"], "me")
        return first, second
    first, second = asyncio.run(run())
    assert first is True
    assert second is False


def test_deny_all_resolves_pending_to_none():
    reg = QuestionRegistry()
    async def run():
        qid, fut = _req(reg)
        reg.deny_all("s1")
        return await fut
    result = asyncio.run(run())
    assert result is None


def test_deny_all_only_affects_target_session():
    reg = QuestionRegistry()
    async def run():
        qid1, fut1 = _req(reg, session_id="s1")
        qid2, fut2 = _req(reg, session_id="s2")
        reg.deny_all("s1")
        # s1 should be resolved to None
        assert fut1.done()
        assert await fut1 is None
        # s2 should still be pending
        assert not fut2.done()
        # Clean up s2
        reg.deny_all("s2")
        assert await fut2 is None
    asyncio.run(run())


def test_deny_all_on_already_resolved_is_safe():
    """deny_all should not crash on already-resolved futures."""
    reg = QuestionRegistry()
    async def run():
        qid, fut = _req(reg)
        reg.resolve(qid, ["A"], "me")
        await fut
        # deny_all on a session with no pending questions — must not raise
        reg.deny_all("s1")
    asyncio.run(run())


def test_multiple_questions_same_session():
    """Multiple concurrent questions for the same session can both be resolved."""
    reg = QuestionRegistry()
    async def run():
        qid1, fut1 = _req(reg, question="Q1?", session_id="s1")
        qid2, fut2 = _req(reg, question="Q2?", session_id="s1")
        reg.resolve(qid1, ["First"], "me")
        reg.resolve(qid2, ["Second"], "me")
        return await fut1, await fut2
    r1, r2 = asyncio.run(run())
    assert r1 == (["First"], "option")
    assert r2 == (["Second"], "option")


# ---------------------------------------------------------------------------
# NEW: request() stores full payload; pending() returns it
# ---------------------------------------------------------------------------

def test_request_stores_full_payload():
    """request() must store header, question, options, multi_select so pending() can rebuild events."""
    reg = QuestionRegistry()
    opts = [{"label": "Yes"}, {"label": "No"}]
    async def run():
        qid, fut = reg.request("sess", "turn-1", "My Header", "Is this right?", opts, True, "alice")
        return qid, fut
    qid, fut = asyncio.run(run())
    rec = reg._pending[qid]
    assert rec["turn_id"] == "turn-1"
    assert rec["header"] == "My Header"
    assert rec["question"] == "Is this right?"
    assert rec["options"] == opts
    assert rec["multi_select"] is True
    assert rec["identity"] == "alice"
    assert rec["session_id"] == "sess"


def test_pending_returns_payload_for_session():
    """pending(session_id) returns one entry per still-pending question, with the right fields."""
    reg = QuestionRegistry()
    opts_a = [{"label": "A1"}]
    opts_b = [{"label": "B1"}, {"label": "B2"}]
    async def run():
        qid_a, fut_a = reg.request("s1", "t1", "H-A", "Q-A?", opts_a, False, "me")
        qid_b, fut_b = reg.request("s1", "t2", "H-B", "Q-B?", opts_b, True, "me")
        # Also register one for a different session — should be excluded.
        reg.request("s2", "t3", "H-C", "Q-C?", [], False, "me")
        return qid_a, qid_b, fut_a, fut_b
    qid_a, qid_b, fut_a, fut_b = asyncio.run(run())
    pendings = reg.pending("s1")
    assert len(pendings) == 2
    # Registration order preserved (dict insertion order).
    p0, p1 = pendings
    assert p0["question_id"] == qid_a
    assert p0["turn_id"] == "t1"
    assert p0["header"] == "H-A"
    assert p0["question"] == "Q-A?"
    assert p0["options"] == opts_a
    assert p0["multi_select"] is False
    assert p1["question_id"] == qid_b
    assert p1["turn_id"] == "t2"
    assert p1["header"] == "H-B"
    assert p1["question"] == "Q-B?"
    assert p1["options"] == opts_b
    assert p1["multi_select"] is True


def test_pending_excludes_resolved_questions():
    """Once a question is resolved, it must not appear in pending()."""
    reg = QuestionRegistry()
    async def run():
        qid_a, fut_a = reg.request("s1", "t1", "H", "Q1?", [], False, "me")
        qid_b, fut_b = reg.request("s1", "t2", "H", "Q2?", [], False, "me")
        # Resolve the first one.
        reg.resolve(qid_a, ["Yes"], "me")
        await fut_a
        return qid_b
    qid_b = asyncio.run(run())
    pendings = reg.pending("s1")
    assert len(pendings) == 1
    assert pendings[0]["question_id"] == qid_b


def test_pending_excludes_denied_questions():
    """After deny_all, pending() returns an empty list for that session."""
    reg = QuestionRegistry()
    async def run():
        reg.request("s1", "t1", "H", "Q?", [], False, "me")
        reg.deny_all("s1")
    asyncio.run(run())
    assert reg.pending("s1") == []


def test_pending_empty_when_no_questions():
    """pending() on a session with no registered questions returns []."""
    reg = QuestionRegistry()
    assert reg.pending("no-such-session") == []


def test_resolve_carries_mode_to_future():
    """resolve(..., mode='chat') resolves the future to (selected, mode)."""
    reg = QuestionRegistry()
    async def run():
        qid, fut = _req(reg)
        ok = reg.resolve(qid, ["typed text"], "me", mode="chat")
        return ok, await fut
    ok, result = asyncio.run(run())
    assert ok is True
    assert result == (["typed text"], "chat")
