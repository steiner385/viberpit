"""Per-thread routing of the app-server's notification and approval streams.

One `codex app-server` serves every thread of one account, so "which session
does this frame belong to?" is a question only the manager can answer — the
connection carries no session identity. These tests pin the two properties that
matter: a frame reaches exactly the thread it names, and a frame nobody owns is
never answered.
"""
from __future__ import annotations

import pytest

from server.codex import schema
from server.codex.manager import (
    CodexRunnerManager,
    CodexRunnerMissingError,
    RunnerNoticeKind,
)
from server.codex.capabilities import CodexAvailability


def _manager(tmp_path) -> CodexRunnerManager:
    async def _creds(account: str):  # pragma: no cover - never reached
        raise AssertionError("no runner is started in these tests")

    return CodexRunnerManager(
        availability=CodexAvailability(available=True, binary_path="/usr/bin/codex"),
        codex_home_base=tmp_path,
        credential_supplier=_creds,
    )


def _turn_started(thread_id: str) -> schema.ServerNotificationParams:
    return schema.parse_server_notification(
        schema.METHOD_TURN_STARTED,
        {
            "threadId": thread_id,
            "turn": {"id": "turn-1", "status": "inProgress", "items": []},
        },
    )


def test_a_thread_scoped_notification_reaches_only_its_own_thread(tmp_path):
    manager = _manager(tmp_path)
    seen: dict[str, list[str]] = {"a": [], "b": []}
    manager.register_thread(
        "acct", "thread-a", lambda n: None,
        events=lambda m, p: seen["a"].append(m))
    manager.register_thread(
        "acct", "thread-b", lambda n: None,
        events=lambda m, p: seen["b"].append(m))

    manager._on_notification("acct", schema.METHOD_TURN_STARTED, _turn_started("thread-a"))

    assert seen == {"a": [schema.METHOD_TURN_STARTED], "b": []}


def _error(thread_id: str) -> schema.ServerNotificationParams:
    return schema.parse_server_notification(
        "error",
        {
            "threadId": thread_id,
            "turnId": "turn-1",
            "willRetry": False,
            "error": {"message": "boom"},
        },
    )


def test_an_account_scoped_notification_fans_out_to_every_thread(tmp_path):
    """`account/updated` describes the shared app-server, so each session reacts."""
    manager = _manager(tmp_path)
    seen: list[str] = []
    for thread in ("thread-a", "thread-b"):
        manager.register_thread(
            "acct", thread, lambda n: None,
            events=lambda m, p, t=thread: seen.append(t))

    params = schema.parse_server_notification("account/updated", {"planType": "pro"})
    manager._on_notification("acct", "account/updated", params)

    assert sorted(seen) == ["thread-a", "thread-b"]


def test_an_error_notification_routes_by_its_redacted_thread_id(tmp_path):
    """`error` wraps its thread id in `Redacted`. Comparing the wrapper against a
    plain id would route every error to nobody — leaving the one session whose
    turn just failed as the only session never told."""
    manager = _manager(tmp_path)
    seen: dict[str, list[str]] = {"a": [], "b": []}
    manager.register_thread(
        "acct", "thread-a", lambda n: None, events=lambda m, p: seen["a"].append(m))
    manager.register_thread(
        "acct", "thread-b", lambda n: None, events=lambda m, p: seen["b"].append(m))

    manager._on_notification("acct", "error", _error("thread-a"))

    assert seen == {"a": ["error"], "b": []}
    assert manager.unrouted_events == 0


def test_a_notification_for_an_unregistered_thread_is_dropped_and_counted(tmp_path):
    manager = _manager(tmp_path)
    manager._on_notification("acct", schema.METHOD_TURN_STARTED, _turn_started("ghost"))
    assert manager.unrouted_events == 1


def test_one_raising_sink_does_not_stop_the_others(tmp_path):
    """The reader is shared with every other thread on this runner — and with the
    daemon serving Claude sessions."""
    manager = _manager(tmp_path)
    delivered: list[str] = []

    def _boom(method, params):
        raise RuntimeError("subscriber is broken")

    manager.register_thread("acct", "thread-a", lambda n: None, events=_boom)
    manager.register_thread(
        "acct", "thread-b", lambda n: None,
        events=lambda m, p: delivered.append(m))

    params = schema.parse_server_notification("account/updated", {"planType": "pro"})
    manager._on_notification("acct", "account/updated", params)

    assert delivered == ["account/updated"]
    assert manager.notify_errors == 1


def test_unregister_thread_stops_every_stream(tmp_path):
    manager = _manager(tmp_path)
    seen: list[str] = []
    manager.register_thread(
        "acct", "thread-a", lambda n: None,
        events=lambda m, p: seen.append(m),
        approvals=None)
    manager.unregister_thread("acct", "thread-a")

    manager._on_notification("acct", schema.METHOD_TURN_STARTED, _turn_started("thread-a"))

    assert seen == []
    assert manager.unrouted_events == 1


def _approval(thread_id: str) -> schema.ApprovalParams:
    return schema.parse_approval_params(
        schema.METHOD_COMMAND_EXECUTION_APPROVAL,
        {
            "itemId": "item-1",
            "threadId": thread_id,
            "turnId": "turn-1",
            "startedAtMs": 0,
            "command": "ls -la",
        },
    )


@pytest.mark.asyncio
async def test_an_approval_reaches_the_thread_that_raised_it(tmp_path):
    manager = _manager(tmp_path)
    answered: list[str] = []

    async def _answer(method, params):
        answered.append(params.thread_id)
        return schema.CommandExecutionApprovalResponse(decision="decline")

    manager.register_thread("acct", "thread-a", lambda n: None, approvals=_answer)
    manager.register_thread(
        "acct", "thread-b", lambda n: None,
        approvals=lambda m, p: pytest.fail("wrong thread answered"))

    response = await manager._on_approval(
        "acct", schema.METHOD_COMMAND_EXECUTION_APPROVAL, _approval("thread-a"))

    assert answered == ["thread-a"]
    assert response.decision == "decline"


@pytest.mark.asyncio
async def test_an_unowned_approval_is_never_answered(tmp_path):
    """Raising is the point: the protocol turns it into a JSON-RPC error, and an
    error can never become an allow."""
    manager = _manager(tmp_path)
    with pytest.raises(CodexRunnerMissingError):
        await manager._on_approval(
            "acct", schema.METHOD_COMMAND_EXECUTION_APPROVAL, _approval("ghost"))
    assert manager.unrouted_approvals == 1


@pytest.mark.asyncio
async def test_an_approval_arriving_during_teardown_is_never_answered(tmp_path):
    manager = _manager(tmp_path)

    async def _answer(method, params):  # pragma: no cover - must not be reached
        raise AssertionError("a closing driver answered an approval")

    manager.register_thread("acct", "thread-a", lambda n: None, approvals=_answer)
    manager.unregister_thread("acct", "thread-a")

    with pytest.raises(CodexRunnerMissingError):
        await manager._on_approval(
            "acct", schema.METHOD_COMMAND_EXECUTION_APPROVAL, _approval("thread-a"))


def test_supervision_notices_still_reach_a_thread_registered_with_sinks(tmp_path):
    """register_thread grew keyword streams; the original one must still work."""
    manager = _manager(tmp_path)
    notices: list[RunnerNoticeKind] = []
    manager.register_thread(
        "acct", "thread-a", lambda n: notices.append(n.kind),
        events=lambda m, p: None)

    from server.codex.manager import RunnerNotice
    manager._notify(RunnerNotice(kind=RunnerNoticeKind.CRASHED, account="acct", generation=1))

    assert notices == [RunnerNoticeKind.CRASHED]
