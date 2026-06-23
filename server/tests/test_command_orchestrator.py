# server/tests/test_command_orchestrator.py
import pytest
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes import fake_sdk


def _orch_with_driver(session_id="s1", server_info=None):
    client = fake_sdk.FakeSDKClient(messages=[], server_info=server_info)
    orch = SessionOrchestrator(sdk_factory=lambda *a, **k: client)
    return orch, client


@pytest.mark.anyio
async def test_command_catalog_empty_when_no_driver():
    orch, _ = _orch_with_driver()
    assert await orch.command_catalog("missing") == []


@pytest.mark.anyio
async def test_run_command_unknown_raises():
    orch, client = _orch_with_driver(server_info={"commands": [{"name": "help"}]})
    from server.sessions.driver import SessionDriver
    from server.sessions.permissions import PermissionRegistry
    d = SessionDriver("s1", "a", "/tmp", "/tmp/c", mode="interactive",
                      sdk_factory=lambda *a, **k: client, registry=PermissionRegistry(),
                      identity="t")
    await d.start()
    orch._drivers["s1"] = d
    with pytest.raises(KeyError):
        await orch.run_command("s1", "nope", "")


@pytest.mark.anyio
async def test_run_command_not_live_session_returns_notice():
    orch, _ = _orch_with_driver()  # no driver registered
    res = await orch.run_command("ghost", "account", "primary")
    assert res is not None and res["render"] == "notice"
    assert "live" in res["data"]["text"].lower()


@pytest.mark.anyio
async def test_run_command_dispatches_passthrough():
    orch, client = _orch_with_driver(server_info={"commands": [{"name": "review"}]})
    from server.sessions.driver import SessionDriver
    from server.sessions.permissions import PermissionRegistry
    d = SessionDriver("s1", "a", "/tmp", "/tmp/c", mode="interactive",
                      sdk_factory=lambda *a, **k: client, registry=PermissionRegistry(),
                      identity="t")
    await d.start()
    orch._drivers["s1"] = d
    res = await orch.run_command("s1", "review", "64")
    assert res is None
    assert client.sent == ["/review 64"]
