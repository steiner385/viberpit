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
async def test_command_catalog_serves_cached_when_detached():
    """A detached session (record present, no live driver) serves its last-known
    catalog so the slash-command palette works on view without a driver resume."""
    from server.sessions.types import SessionRecord
    orch, _ = _orch_with_driver()
    cached = [{"name": "review", "description": "Review a PR", "argumentHint": "<pr>", "kind": "passthrough"}]
    orch._records["det"] = SessionRecord(
        id="det", name="Detached", account="a", claude_session_id="c",
        cols=80, rows=24, cwd="/tmp", detached=True, command_catalog=cached,
    )
    assert await orch.command_catalog("det") == cached


@pytest.mark.anyio
async def test_command_catalog_persists_live_build_onto_record():
    """Building the catalog from a live driver caches it on the record so a later
    detached view can replay it."""
    from server.sessions.claude_driver import ClaudeAgentDriver
    from server.sessions.permissions import PermissionRegistry
    from server.sessions.types import SessionRecord
    orch, client = _orch_with_driver(server_info={"commands": [{"name": "review", "description": "Review a PR"}]})
    orch._records["s1"] = SessionRecord(
        id="s1", name="Live", account="a", claude_session_id="c",
        cols=80, rows=24, cwd="/tmp",
    )
    d = ClaudeAgentDriver("s1", "a", "/tmp", "/tmp/c", mode="interactive",
                      sdk_factory=lambda *a, **k: client, registry=PermissionRegistry(),
                      identity="t")
    await d.start()
    orch._drivers["s1"] = d
    catalog = await orch.command_catalog("s1")
    assert any(c["name"] == "review" for c in catalog)
    assert orch._records["s1"].command_catalog == catalog


@pytest.mark.anyio
async def test_run_command_unknown_raises():
    orch, client = _orch_with_driver(server_info={"commands": [{"name": "help"}]})
    from server.sessions.claude_driver import ClaudeAgentDriver
    from server.sessions.permissions import PermissionRegistry
    d = ClaudeAgentDriver("s1", "a", "/tmp", "/tmp/c", mode="interactive",
                      sdk_factory=lambda *a, **k: client, registry=PermissionRegistry(),
                      identity="t")
    await d.start()
    orch._drivers["s1"] = d
    with pytest.raises(KeyError):
        await orch.run_command("s1", "nope", "")


@pytest.mark.anyio
async def test_run_command_not_live_session_returns_notice():
    orch, _ = _orch_with_driver()  # no driver registered
    # Use a driver-bound command (/review): /account AND /model are handled BEFORE
    # the driver check so they work on driverless sessions (see
    # test_driverless_reassign and test_model_switch_restart).
    res = await orch.run_command("ghost", "review", "")
    assert res is not None and res["render"] == "notice"
    assert "live" in res["data"]["text"].lower()


@pytest.mark.anyio
async def test_run_command_dispatches_passthrough():
    orch, client = _orch_with_driver(server_info={"commands": [{"name": "review"}]})
    from server.sessions.claude_driver import ClaudeAgentDriver
    from server.sessions.permissions import PermissionRegistry
    d = ClaudeAgentDriver("s1", "a", "/tmp", "/tmp/c", mode="interactive",
                      sdk_factory=lambda *a, **k: client, registry=PermissionRegistry(),
                      identity="t")
    await d.start()
    orch._drivers["s1"] = d
    res = await orch.run_command("s1", "review", "64")
    assert res is None
    assert client.sent == ["/review 64"]
