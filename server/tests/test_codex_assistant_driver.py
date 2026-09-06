"""`CodexAgentDriver` with an assistant overlay (#1321/#1290).

The overlay's own unit tests (`test_assistant_codex_overlay.py`) prove the shape;
these prove the driver actually *applies* it, through the real transport, because
every property here is one the driver could quietly drop:

* the thread is started, never resumed (a resume silently loses `dynamicTools`),
* the tool surface and the neutral `cwd` reach `thread/start`,
* a ccswitch tool call is gated by the SAME `PermissionRegistry` as its Claude
  twin — reads auto-allow, destructive tools raise a card,
* every built-in approval is declined without a card,
* a permission-mode change cannot widen the sandbox.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
import pytest_asyncio

from server.assistant import options_codex as oc
from server.codex import schema
from server.sessions import events as ev
from server.tests.test_codex_driver import SESSION, THREAD, Harness

TOOL_CALL = schema.METHOD_ITEM_TOOL_CALL


class _Api:
    identity = "user@example.com"

    async def get(self, path: str, **_: Any) -> Any:
        return [{"id": "abc", "name": "Refactor auth", "status": "waiting_input"}]

    async def post(self, path: str, **_: Any) -> Any:
        return {"ok": True}


def _api() -> Any:
    """The AssistantApi seam, duck-typed. Returned as `Any` so the fake does not
    have to inherit a real httpx-backed client just to satisfy the annotation."""
    return _Api()

@pytest.fixture
def overlay(tmp_path, monkeypatch) -> oc.CodexAssistantOverlay:
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "cchome"))
    return oc.assistant_codex_options(api=_api(), model="gpt-5.6-sol")


@pytest_asyncio.fixture
async def harness(tmp_path):
    h = Harness(tmp_path)
    try:
        yield h
    finally:
        if getattr(h, "driver", None) is not None:
            await h.aclose()


def _call(tool: str = "list_sessions", arguments: Any = None,
          thread_id: str = THREAD) -> dict[str, Any]:
    return {"threadId": thread_id, "turnId": "turn-1", "callId": "call-1",
            "tool": tool, "arguments": {} if arguments is None else arguments}


async def _await_reply(harness: Harness, request_id: int) -> dict[str, Any]:
    return await harness.server.receive_reply(request_id)


# ---------------------------------------------------------------------------
# thread/start
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_thread_starts_with_the_tool_surface_and_a_neutral_cwd(harness, overlay):
    await harness.boot(assistant_overlay=overlay)
    params = harness.params(schema.METHOD_THREAD_START)
    assert params["cwd"] == overlay.cwd
    assert params["sandbox"] == "read-only"
    assert params["approvalPolicy"] == "untrusted"
    assert {t["name"] for t in params["dynamicTools"]} == {
        s.name for s in overlay.dynamic_tools}


@pytest.mark.asyncio
async def test_an_assistant_thread_is_started_even_when_the_row_says_resume(harness,
                                                                            overlay):
    # The vendor boundary: `thread/resume` has no `dynamicTools`, so resuming
    # would hand the drawer back with the built-ins declared and every ccswitch
    # tool gone. Starting fresh loses model context; resuming loses the boundary.
    await harness.boot(assistant_overlay=overlay, resume="thread-from-yesterday")
    assert harness.frames(schema.METHOD_THREAD_RESUME) == []
    assert len(harness.frames(schema.METHOD_THREAD_START)) == 1


@pytest.mark.asyncio
async def test_a_coding_thread_still_resumes(harness):
    # The other half of the same rule: nothing about a non-assistant session moved.
    await harness.boot(resume="thread-from-yesterday")
    assert len(harness.frames(schema.METHOD_THREAD_RESUME)) == 1
    assert harness.frames(schema.METHOD_THREAD_START) == []


@pytest.mark.asyncio
async def test_a_coding_thread_declares_no_dynamic_tools(harness):
    await harness.boot()
    assert "dynamicTools" not in harness.params(schema.METHOD_THREAD_START)


# ---------------------------------------------------------------------------
# Built-in approvals: default deny, no card
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_shell_approval_is_declined_without_raising_a_card(harness, overlay):
    driver = await harness.boot(assistant_overlay=overlay)
    request_id = harness.server.request(
        schema.METHOD_COMMAND_EXECUTION_APPROVAL,
        {"itemId": "item-1", "threadId": THREAD, "turnId": "turn-1",
         "startedAtMs": 0, "command": "/bin/bash -lc 'ls -la'", "cwd": "/"},
    )
    reply = await _await_reply(harness, request_id)
    await harness.settle()
    assert reply["result"] == {"decision": "decline"}
    # No card, in either direction: not in the registry, not on the event stream.
    assert harness.of(ev.PermissionRequest) == []
    assert harness.registry.pending(SESSION) == []


@pytest.mark.asyncio
async def test_a_file_change_approval_is_declined_too(harness, overlay):
    await harness.boot(assistant_overlay=overlay)
    request_id = harness.server.request(
        schema.METHOD_FILE_CHANGE_APPROVAL,
        {"itemId": "item-2", "threadId": THREAD, "turnId": "turn-1", "startedAtMs": 0},
    )
    assert (await _await_reply(harness, request_id))["result"] == {"decision": "decline"}


@pytest.mark.asyncio
async def test_a_coding_session_still_gets_its_permission_card(harness):
    # #1290's "preserve working Claude behavior", Codex edition: the default-deny
    # must be scoped to assistant threads, not applied to every Codex session.
    await harness.boot()
    harness.server.request(
        schema.METHOD_COMMAND_EXECUTION_APPROVAL,
        {"itemId": "item-1", "threadId": THREAD, "turnId": "turn-1",
         "startedAtMs": 0, "command": "ls", "cwd": "/"},
    )
    for _ in range(200):
        if harness.registry.pending(SESSION):
            break
        await asyncio.sleep(0.005)
    assert harness.registry.pending(SESSION), "a coding session lost its approval card"


# ---------------------------------------------------------------------------
# Tool calls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_read_tool_runs_and_answers_with_its_envelope(harness, overlay):
    await harness.boot(assistant_overlay=overlay)
    request_id = harness.server.request(TOOL_CALL, _call())
    reply = await _await_reply(harness, request_id)
    assert reply["result"]["success"] is True
    payload = json.loads(reply["result"]["contentItems"][0]["text"])
    assert payload["data"][0]["id"] == "abc"


@pytest.mark.asyncio
async def test_a_read_tool_raises_no_permission_card(harness, overlay):
    # The `mcp__ccswitch__` branch of PermissionRegistry auto-allows READs, and
    # routing through it is precisely how this stays identical to Claude.
    await harness.boot(assistant_overlay=overlay)
    request_id = harness.server.request(TOOL_CALL, _call())
    await _await_reply(harness, request_id)
    await harness.settle()
    assert harness.of(ev.PermissionRequest) == []


@pytest.mark.asyncio
async def test_a_destructive_tool_raises_a_card_and_a_denial_stops_it(harness, overlay):
    driver = await harness.boot(assistant_overlay=overlay)
    request_id = harness.server.request(
        TOOL_CALL, _call("archive_session", {"id": "abc"}))
    for _ in range(200):
        if harness.registry.pending(SESSION):
            break
        await asyncio.sleep(0.005)
    pending = harness.registry.pending(SESSION)
    assert pending, "a destructive ccswitch tool ran with no card"
    assert pending[0]["tool"] == "mcp__ccswitch__archive_session"
    driver.resolve_permission(pending[0]["permId"], "deny", "user@example.com")
    reply = await _await_reply(harness, request_id)
    assert reply["result"]["success"] is False
    assert reply["result"]["contentItems"][0]["text"] == "denied by user"


@pytest.mark.asyncio
async def test_an_undeclared_tool_name_is_a_tool_error(harness, overlay):
    await harness.boot(assistant_overlay=overlay)
    request_id = harness.server.request(TOOL_CALL, _call("Bash", {"command": "ls"}))
    reply = await _await_reply(harness, request_id)
    assert reply["result"]["success"] is False
    assert "no tool named" in reply["result"]["contentItems"][0]["text"]


@pytest.mark.asyncio
async def test_an_undeclared_tool_never_raises_a_confirmation_card(harness, overlay):
    # The registry treats an unannotated `mcp__ccswitch__*` name conservatively as
    # mutation-like, so reaching the gate with a name that does not exist would
    # park the turn behind a card for a tool nobody can run.
    await harness.boot(assistant_overlay=overlay)
    harness.server.request(TOOL_CALL, _call("Bash", {"command": "ls"}))
    await harness.settle()
    assert harness.registry.pending(SESSION) == []


@pytest.mark.asyncio
async def test_a_call_for_another_thread_never_reaches_a_tool(harness, overlay):
    # The manager refuses first — its per-thread sink map is what stops one
    # session's model running another session's tool table — so the app-server
    # gets a JSON-RPC error rather than a fabricated result.
    await harness.boot(assistant_overlay=overlay)
    request_id = harness.server.request(TOOL_CALL, _call(thread_id="someone-elses"))
    reply = await _await_reply(harness, request_id)
    assert "result" not in reply
    assert harness.manager.unrouted_dynamic_tools == 1


# ---------------------------------------------------------------------------
# The pinned permission mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_mode_change_cannot_widen_an_assistant_thread(harness, overlay):
    driver = await harness.boot(assistant_overlay=overlay)
    before = len(harness.frames(schema.METHOD_THREAD_RESUME))
    await driver.set_permission_mode("bypassPermissions")
    await harness.settle()
    # Applying a sandbox change means re-resuming, and a resume drops the tools.
    assert len(harness.frames(schema.METHOD_THREAD_RESUME)) == before
    assert driver._profile.sandbox_mode.value == "read-only"


@pytest.mark.asyncio
async def test_a_coding_session_can_still_change_mode(harness):
    driver = await harness.boot(mode="interactive")
    await driver.set_permission_mode("plan")
    await harness.settle()
    assert driver.mode == "plan"
