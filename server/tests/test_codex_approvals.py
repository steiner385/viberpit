"""Codex approvals: one human decision, answered exactly once, or declined.

The app-server blocks a running command on the JSON-RPC reply to its approval
request, so every path here is really a question about a *pending native
request*: who may answer it, whether the answer is still valid when it arrives,
and what happens to it when the session, the turn, or the app-server goes away.
Every one of those paths must be able to end in `decline` and none of them may
end in `accept` by default — an approval ccswitch cannot service must never be
granted.

The requests are driven through the real transport (the fake writes a JSON-RPC
request frame) so the assertions are on the reply that actually goes on the
wire, not on the driver's return value.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
import pytest_asyncio

from server.codex import schema
from server.codex.protocol import JSONRPC_METHOD_NOT_FOUND
from server.sessions import events as ev
from server.tests.test_codex_driver import ACCOUNT, SESSION, THREAD, Harness

APPROVAL = schema.METHOD_COMMAND_EXECUTION_APPROVAL
FILE_CHANGE = schema.METHOD_FILE_CHANGE_APPROVAL


@pytest_asyncio.fixture
async def harness(tmp_path):
    h = Harness(tmp_path)
    try:
        yield h
    finally:
        if getattr(h, "driver", None) is not None:
            await h.aclose()


def _command_params(thread_id: str = THREAD, turn_id: str = "turn-1") -> dict[str, Any]:
    return {
        "itemId": "item-7",
        "threadId": thread_id,
        "turnId": turn_id,
        "startedAtMs": 1,
        "command": "rm -rf build",
        "cwd": "/work",
        "reason": "not on the trusted list",
    }


def _file_change_params(thread_id: str = THREAD) -> dict[str, Any]:
    return {
        "itemId": "item-8",
        "threadId": thread_id,
        "turnId": "turn-1",
        "startedAtMs": 1,
        "grantRoot": "/work",
    }


async def _ask(harness: Harness, method: str = APPROVAL, params: Any = None) -> int:
    """Have the app-server raise one approval request and wait for its card."""
    request_id = harness.server.request(
        method, params if params is not None else _command_params()
    )
    for _ in range(200):
        if harness.registry.pending(SESSION):
            return request_id
        await asyncio.sleep(0.005)
    raise AssertionError("no permission card was raised")


def _perm_id(harness: Harness) -> str:
    pending = harness.registry.pending(SESSION)
    assert len(pending) == 1
    return pending[0]["permId"]


async def _reply(harness: Harness, request_id: int) -> dict[str, Any]:
    return await harness.server.receive_reply(request_id)


# ---------------------------------------------------------------------------
# The three decisions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_allow_answers_accept_once(harness):
    driver = await harness.boot()
    await harness.run_turn("build it")
    request_id = await _ask(harness)

    card = harness.of(ev.PermissionRequest)[-1]
    assert (card.tool, card.kind) == ("Bash", ev.ToolKind.BASH)
    assert card.input["command"] == "rm -rf build"
    assert driver.current_status() == "waiting_input"

    assert driver.resolve_permission(_perm_id(harness), "allow", driver.identity)
    reply = await _reply(harness, request_id)

    assert reply["result"] == {"decision": "accept"}
    assert harness.of(ev.PermissionResolved)[-1].allowed is True


@pytest.mark.asyncio
async def test_always_answers_the_engine_s_session_scoped_accept(harness):
    """"Always" is honoured by Codex for this session only. It must never write a
    ccswitch grant (that cache is not consulted for an engine that enforces
    approvals itself) and never a persisted global exception."""
    driver = await harness.boot()
    await harness.run_turn("build it")
    request_id = await _ask(harness)

    driver.resolve_permission(_perm_id(harness), "always", driver.identity)
    reply = await _reply(harness, request_id)

    assert reply["result"] == {"decision": "acceptForSession"}
    assert harness.registry._grants == {}


@pytest.mark.asyncio
async def test_deny_answers_decline(harness):
    driver = await harness.boot()
    await harness.run_turn("build it")
    request_id = await _ask(harness)

    driver.resolve_permission(_perm_id(harness), "deny", driver.identity)
    reply = await _reply(harness, request_id)

    assert reply["result"] == {"decision": "decline"}
    assert harness.of(ev.PermissionResolved)[-1].allowed is False


@pytest.mark.asyncio
async def test_a_file_change_request_renders_as_an_edit_card(harness):
    driver = await harness.boot()
    await harness.run_turn("patch it")
    request_id = await _ask(harness, FILE_CHANGE, _file_change_params())

    card = harness.of(ev.PermissionRequest)[-1]
    assert (card.tool, card.kind) == ("Edit", ev.ToolKind.EDIT)
    driver.resolve_permission(_perm_id(harness), "deny", driver.identity)

    assert (await _reply(harness, request_id))["result"] == {"decision": "decline"}


@pytest.mark.asyncio
async def test_only_the_owning_identity_can_answer(harness):
    driver = await harness.boot()
    await harness.run_turn("build it")
    await _ask(harness)

    assert driver.resolve_permission(_perm_id(harness), "allow", "someone@else") is False
    assert harness.registry.pending(SESSION)


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_request_for_another_thread_is_declined_without_a_card(harness):
    """One app-server serves every thread of an account. The manager routes by
    thread id, so this is defence in depth — checked by calling the sink
    directly, because a correct manager can never deliver such a request."""
    driver = await harness.boot()
    response = await driver._on_approval(
        APPROVAL,
        schema.parse_approval_params(APPROVAL, _command_params(thread_id="other")),
    )

    assert response.decision == "decline"
    assert harness.registry.pending(SESSION) == []
    assert driver.counters.stale_approvals == 1


@pytest.mark.asyncio
async def test_a_request_from_a_previous_runner_generation_is_declined(harness):
    """The generation fence: the process that asked is gone, so nothing it asked
    can be answered — least of all with an accept."""
    driver = await harness.boot()
    harness.manager._runners[ACCOUNT].generation = 42
    request_id = harness.server.request(APPROVAL, _command_params())
    await harness.settle()

    assert (await _reply(harness, request_id))["result"] == {"decision": "decline"}
    assert driver.counters.stale_approvals == 1


@pytest.mark.asyncio
async def test_an_allow_decided_after_the_runner_was_replaced_is_declined(harness):
    """A human can take minutes. If the app-server was replaced meanwhile, the
    decision describes a request the new process never made."""
    driver = await harness.boot()
    await harness.run_turn("build it")
    request_id = await _ask(harness)

    harness.manager._runners[ACCOUNT].generation = 99
    driver.resolve_permission(_perm_id(harness), "allow", driver.identity)
    reply = await _reply(harness, request_id)

    assert reply["result"] == {"decision": "decline"}
    assert harness.of(ev.PermissionResolved)[-1].allowed is False
    assert driver.counters.stale_approvals == 1


@pytest.mark.asyncio
async def test_the_permissions_approval_surface_stays_unsupported(harness):
    """`item/permissions/requestApproval` hands back a permissions GRANT, which
    ccswitch has no vocabulary for. It must keep taking the fail-closed
    `-32601` path rather than being answered by this driver."""
    await harness.boot()
    request_id = harness.server.request(
        "item/permissions/requestApproval", _command_params()
    )
    await harness.settle()

    reply = await _reply(harness, request_id)
    assert reply["error"]["code"] == JSONRPC_METHOD_NOT_FOUND
    assert harness.registry.pending(SESSION) == []


# ---------------------------------------------------------------------------
# Exit paths — no native request is ever left stranded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_interrupt_declines_every_pending_request(harness):
    driver = await harness.boot()
    await harness.run_turn("build it")
    request_id = await _ask(harness)

    await driver.interrupt()
    reply = await _reply(harness, request_id)

    assert reply["result"] == {"decision": "decline"}
    assert harness.registry.pending(SESSION) == []
    assert harness.of(ev.PermissionResolved)[-1].allowed is False


@pytest.mark.asyncio
async def test_a_crash_notice_declines_every_pending_request(harness):
    from server.codex.manager import RunnerNotice, RunnerNoticeKind

    driver = await harness.boot()
    await harness.run_turn("build it")
    request_id = await _ask(harness)

    driver._on_notice(
        RunnerNotice(kind=RunnerNoticeKind.CRASHED, account=ACCOUNT, generation=1)
    )
    await harness.settle()

    assert (await _reply(harness, request_id))["result"] == {"decision": "decline"}
    assert harness.registry.pending(SESSION) == []


@pytest.mark.asyncio
async def test_auth_loss_declines_every_pending_request(harness):
    driver = await harness.boot()
    await harness.run_turn("build it")
    request_id = await _ask(harness)

    harness.server.notify(
        "error",
        {
            "threadId": THREAD,
            "turnId": "turn-1",
            "willRetry": False,
            "error": {"message": "401", "codexErrorInfo": "unauthorized"},
        },
    )
    await harness.settle()

    assert (await _reply(harness, request_id))["result"] == {"decision": "decline"}
    assert harness.registry.pending(SESSION) == []


@pytest.mark.asyncio
async def test_closing_the_session_declines_every_pending_request(harness):
    driver = await harness.boot()
    await harness.run_turn("build it")
    request_id = await _ask(harness)

    await driver.aclose()

    assert (await _reply(harness, request_id))["result"] == {"decision": "decline"}
    assert harness.registry.pending(SESSION) == []


@pytest.mark.asyncio
async def test_archiving_the_session_declines_every_pending_request(harness):
    driver = await harness.boot()
    await harness.run_turn("build it")
    request_id = await _ask(harness)

    await driver.archive()

    assert (await _reply(harness, request_id))["result"] == {"decision": "decline"}
    assert harness.registry.pending(SESSION) == []


@pytest.mark.asyncio
async def test_a_request_arriving_after_teardown_is_never_answered_by_the_driver(
    harness,
):
    """`unregister_thread` runs FIRST in aclose, so a late request finds no
    handler and takes the protocol's fail-closed path instead of reaching a
    driver that is already closing."""
    driver = await harness.boot()
    await driver.aclose()

    request_id = harness.server.request(APPROVAL, _command_params())
    await harness.settle()

    reply = await _reply(harness, request_id)
    assert "result" not in reply
    assert harness.registry.pending(SESSION) == []
