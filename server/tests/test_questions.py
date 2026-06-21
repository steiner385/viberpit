"""Tests for QuestionRegistry — mirrors test_permissions.py."""
import asyncio
from server.sessions.questions import QuestionRegistry


def test_request_resolve_sets_future_to_selected_list():
    reg = QuestionRegistry()
    async def run():
        qid, fut = reg.request("s1", "t1", "me")
        assert qid is not None
        reg.resolve(qid, ["Option A"], "me")
        return await fut
    result = asyncio.run(run())
    assert result == ["Option A"]


def test_request_resolve_multi_select():
    reg = QuestionRegistry()
    async def run():
        qid, fut = reg.request("s1", "t1", "me")
        reg.resolve(qid, ["A", "B"], "me")
        return await fut
    result = asyncio.run(run())
    assert result == ["A", "B"]


def test_resolve_wrong_identity_returns_false():
    reg = QuestionRegistry()
    async def run():
        qid, fut = reg.request("s1", "t1", "owner")
        result = reg.resolve(qid, ["X"], "attacker")
        return result, fut
    result, fut = asyncio.run(run())
    assert result is False
    assert not fut.done()


def test_resolve_unknown_id_returns_false():
    reg = QuestionRegistry()
    async def run():
        reg.request("s1", "t1", "me")
        return reg.resolve("nonexistent-id", ["X"], "me")
    assert asyncio.run(run()) is False


def test_resolve_already_done_returns_false():
    """Resolving the same question twice is idempotent — second call returns False."""
    reg = QuestionRegistry()
    async def run():
        qid, fut = reg.request("s1", "t1", "me")
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
        qid, fut = reg.request("s1", "t1", "me")
        reg.deny_all("s1")
        return await fut
    result = asyncio.run(run())
    assert result is None


def test_deny_all_only_affects_target_session():
    reg = QuestionRegistry()
    async def run():
        qid1, fut1 = reg.request("s1", "t1", "me")
        qid2, fut2 = reg.request("s2", "t1", "me")
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
        qid, fut = reg.request("s1", "t1", "me")
        reg.resolve(qid, ["A"], "me")
        await fut
        # deny_all on a session with no pending questions — must not raise
        reg.deny_all("s1")
    asyncio.run(run())


def test_multiple_questions_same_session():
    """Multiple concurrent questions for the same session can both be resolved."""
    reg = QuestionRegistry()
    async def run():
        qid1, fut1 = reg.request("s1", "t1", "me")
        qid2, fut2 = reg.request("s1", "t1", "me")
        reg.resolve(qid1, ["First"], "me")
        reg.resolve(qid2, ["Second"], "me")
        return await fut1, await fut2
    r1, r2 = asyncio.run(run())
    assert r1 == ["First"]
    assert r2 == ["Second"]
