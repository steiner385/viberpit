import asyncio
import pytest
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord
from server.sessions.driver import SessionDriver
from server.sessions.permissions import PermissionRegistry
from server.tests.fakes import fake_sdk


async def _orch_with_goal_session():
    client = fake_sdk.FakeSDKClient(messages=[], server_info={"commands": [{"name": "goal"}]})
    orch = SessionOrchestrator(sdk_factory=lambda *a, **k: client)
    d = SessionDriver("s1", "a", "/tmp", "/tmp/c", mode="interactive",
                      sdk_factory=lambda *a, **k: client, registry=PermissionRegistry(),
                      identity="t")
    await d.start()
    orch._drivers["s1"] = d
    orch._records["s1"] = SessionRecord(id="s1", name="n", account="a",
                                        claude_session_id="c1", cols=80, rows=24, cwd="/tmp")

    async def _noop_persist(sid, goal):
        pass
    orch._persist_goal = _noop_persist  # type: ignore[attr-defined]
    return orch, client


@pytest.mark.anyio
async def test_goal_command_captures_and_forwards():
    orch, client = await _orch_with_goal_session()
    res = await orch.run_command("s1", "goal", "ship the PR")
    assert res is None                                   # passthrough
    assert orch._records["s1"].active_goal == "ship the PR"
    assert client.sent == ["/goal ship the PR"]          # still forwarded to the CLI


@pytest.mark.anyio
async def test_goal_clear_resets_active_goal():
    orch, client = await _orch_with_goal_session()
    await orch.run_command("s1", "goal", "ship the PR")
    await asyncio.sleep(0)  # let drain task finish before second send_prompt
    res = await orch.run_command("s1", "goal", "clear")
    assert res is None
    assert orch._records["s1"].active_goal is None
    assert client.sent == ["/goal ship the PR", "/goal clear"]


@pytest.mark.anyio
async def test_goal_empty_args_clears():
    orch, client = await _orch_with_goal_session()
    await orch.run_command("s1", "goal", "ship the PR")
    await asyncio.sleep(0)  # let drain task finish before second send_prompt
    await orch.run_command("s1", "goal", "")
    assert orch._records["s1"].active_goal is None
