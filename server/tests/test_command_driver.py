# server/tests/test_command_driver.py
import pytest
from server.sessions.driver import SessionDriver
from server.sessions.permissions import PermissionRegistry
from server.tests.fakes import fake_sdk


def _driver(client):
    def factory(cwd, config_dir, can_use_tool, mode, resume):
        client.last_resume = resume
        return client
    return SessionDriver(
        "sess-1", "acct", "/tmp", "/tmp/cfg",
        mode="interactive", sdk_factory=factory,
        registry=PermissionRegistry(), identity="tester",
    )


@pytest.mark.anyio
async def test_set_model_delegates():
    client = fake_sdk.FakeSDKClient(messages=[])
    d = _driver(client)
    await d.start()
    await d.set_model("sonnet")
    assert client.model == "sonnet"


@pytest.mark.anyio
async def test_server_info_and_context_usage_delegate():
    client = fake_sdk.FakeSDKClient(
        messages=[], server_info={"commands": [{"name": "help"}]},
        context_usage={"percentage": 12.5},
    )
    d = _driver(client)
    await d.start()
    assert (await d.server_info())["commands"][0]["name"] == "help"
    assert (await d.context_usage())["percentage"] == 12.5


@pytest.mark.anyio
async def test_reconnect_starts_fresh_and_resets_state():
    client = fake_sdk.FakeSDKClient(messages=[])
    d = _driver(client)
    await d.start()
    d.claude_session_id = "old-sid"
    d._titled = True
    await d.reconnect()
    assert client.last_resume is None          # fresh session, resume dropped
    assert d.claude_session_id is None
    assert d._titled is False
    assert d.resume is None


@pytest.mark.anyio
async def test_reconnect_calls_sync_back():
    """reconnect() must invoke on_sync_back to capture any token rotation mid-turn."""
    client = fake_sdk.FakeSDKClient(messages=[])
    sync_back_called = []

    def factory(cwd, config_dir, can_use_tool, mode, resume):
        client.last_resume = resume
        return client

    d = SessionDriver(
        "sess-sync", "acct", "/tmp", "/tmp/cfg",
        mode="interactive", sdk_factory=factory,
        registry=PermissionRegistry(), identity="tester",
        on_sync_back=lambda: sync_back_called.append(True),
    )
    await d.start()
    await d.reconnect()
    assert sync_back_called, "on_sync_back must be called during reconnect()"
