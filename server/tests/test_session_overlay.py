"""A coding session's ccswitch MCP overlay is armed at every driver build and released
by the shared teardown (session tools spec §3.3; fix-pattern
docs/superpowers/fix-patterns/2026-09-04-per-process-state-armed-only-on-the-create-path.md).

``get_settings()`` is uncached (``server/config.py`` builds a fresh ``Settings`` per
call), so ``monkeypatch.setenv`` alone flips the flag — no cache reset is needed.
"""
from __future__ import annotations

import pytest

from server.sessions.types import ASSISTANT_KIND
from server.tests.fakes.fake_sdk import FakeSDKClient
from server.tests.test_assistant_reap import _boot_orch, _capturing_factory, _teardown


@pytest.mark.asyncio
async def test_spawn_mounts_only_mcp_servers(isolated_db, tmp_path, monkeypatch):
    clients: list[FakeSDKClient] = []
    overlays: list = []
    orch, patches = await _boot_orch(tmp_path, monkeypatch,
                                     _capturing_factory(clients, overlays=overlays))
    try:
        s = await orch.create_session(name="Code", cwd=str(tmp_path), identity="u@x")
        ov = overlays[-1]
        assert ov is not None and set(ov) == {"mcp_servers"}, ov
        assert set(ov["mcp_servers"]) == {"ccswitch"}
        assert "strict_mcp_config" not in ov and "system_prompt" not in ov
        assert s.id in orch._overlays
    finally:
        await _teardown(orch, patches)


@pytest.mark.asyncio
async def test_flag_off_mounts_nothing(isolated_db, tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_SESSION_TOOLS_ENABLED", "false")
    clients: list[FakeSDKClient] = []
    overlays: list = []
    orch, patches = await _boot_orch(tmp_path, monkeypatch,
                                     _capturing_factory(clients, overlays=overlays))
    try:
        s = await orch.create_session(name="Code", cwd=str(tmp_path), identity="u@x")
        assert overlays[-1] is None and s.id not in orch._overlays
    finally:
        await _teardown(orch, patches)


@pytest.mark.asyncio
async def test_assistant_kind_still_gets_the_assistant_overlay(isolated_db, tmp_path,
                                                               monkeypatch):
    clients: list[FakeSDKClient] = []
    overlays: list = []
    orch, patches = await _boot_orch(tmp_path, monkeypatch,
                                     _capturing_factory(clients, overlays=overlays))
    try:
        await orch.create_session(name="A", cwd=str(tmp_path), identity="u@x",
                                  kind=ASSISTANT_KIND, owner_identity="u@x")
        ov = overlays[-1]
        assert ov["strict_mcp_config"] is True and ov["tools"] == []
    finally:
        await _teardown(orch, patches)


@pytest.mark.asyncio
async def test_close_releases_the_overlay(isolated_db, tmp_path, monkeypatch):
    clients: list[FakeSDKClient] = []
    orch, patches = await _boot_orch(tmp_path, monkeypatch, _capturing_factory(clients))
    try:
        s = await orch.create_session(name="Code", cwd=str(tmp_path), identity="u@x")
        assert s.id in orch._overlays
        await orch.close_session(s.id)
        assert s.id not in orch._overlays
    finally:
        await _teardown(orch, patches)


@pytest.mark.asyncio
async def test_resume_rebuilds_the_overlay(isolated_db, tmp_path, monkeypatch):
    """Mirror of test_resume_after_reap_rebuilds_the_assistant_overlay for a coding
    session: a detach + ensure_driver must hand the SDK factory a fresh overlay."""
    clients: list[FakeSDKClient] = []
    overlays: list = []
    orch, patches = await _boot_orch(tmp_path, monkeypatch,
                                     _capturing_factory(clients, overlays=overlays))
    try:
        s = await orch.create_session(name="Code", cwd=str(tmp_path), identity="u@x")
        first = orch._overlays[s.id]
        driver = await orch._teardown_driver_state(s.id)
        await driver.aclose()
        assert s.id not in orch._overlays
        rec = orch._records[s.id]
        rec.engine_session_id = rec.engine_session_id or "claude-sid"
        assert await orch.ensure_driver(s.id, "u@x") is not None
        assert overlays[-1] is not None and overlays[-1] is not first
        assert set(overlays[-1]) == {"mcp_servers"}
    finally:
        await _teardown(orch, patches)


@pytest.mark.asyncio
async def test_adopt_arms_the_overlay(tmp_path, monkeypatch):
    """The FOURTH driver-build site. An adopted process is a coding session, and the
    system-prompt append tells every Claude session its ccswitch tools exist — so
    adopting without arming the surface would advertise tools that are not mounted."""
    from datetime import datetime, timezone

    from server.db import init_db
    from server.sessions.adopt import AdoptionCandidate
    from server.sessions.orchestrator import SessionOrchestrator

    await init_db(db_url="sqlite+aiosqlite:///:memory:")
    cwd = tmp_path / "repo"
    cwd.mkdir()
    sid = "66666666-6666-6666-6666-666666666666"
    cand = AdoptionCandidate(
        pid=999, account="kindash", cwd=str(cwd), claude_session_id=sid,
        transcript_path="/x.jsonl", name="Adopted", mapped=True, tracked=False,
        skip_reason=None, last_activity=datetime.now(timezone.utc), pid_alive=True,
    )
    clients: list[FakeSDKClient] = []
    overlays: list = []
    orch = SessionOrchestrator(sdk_factory=_capturing_factory(clients, overlays=overlays),
                               repo_root=str(tmp_path))
    monkeypatch.setattr(orch, "_resolve_candidate", lambda s: cand)
    monkeypatch.setattr("server.sessions.orchestrator.compose", lambda a: tmp_path / "cfg")
    monkeypatch.setattr("server.sessions.orchestrator.configdir.trust_project",
                        lambda *a, **k: None)
    monkeypatch.setattr("server.sessions.orchestrator._adopt.terminate_process_tree",
                        lambda pid, killer: True)

    rec, _ = await orch.adopt_session(sid, identity="t@e.com")
    assert rec.id in orch._overlays
    assert overlays[-1] is not None and set(overlays[-1]) == {"mcp_servers"}
    assert set(overlays[-1]["mcp_servers"]) == {"ccswitch"}


@pytest.mark.asyncio
async def test_codex_engine_registers_the_codex_surface(isolated_db, monkeypatch):
    """The Codex branch of the same registry (Task 7). Exercised on a bare
    orchestrator instance rather than a booted one: a Codex-capable orchestrator
    needs a live ``CodexRunnerManager``, and what is under test here is only which
    overlay TYPE the engine selects — the driver half has its own suite
    (``test_codex_session_tools.py``)."""
    from types import SimpleNamespace

    from server.sessions.orchestrator import SessionOrchestrator
    from server.sessions.tools.codex import CodexSessionTools

    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._overlays = {}
    orch._records = {"s1": SimpleNamespace(id="s1", name="n", repo="o/n", branch="b",
                                           cwd="/w", engine="codex", model=None,
                                           account="a", host="h", origin=None,
                                           origin_ref=None, active_goal=None,
                                           inferred_goal=None, permission_mode="default")}
    await orch._setup_session_overlay("s1", identity="u@x", engine="codex")
    surface = orch._overlays["s1"]
    assert isinstance(surface, CodexSessionTools)
    assert {s.name for s in surface.dynamic_tools} == {"session_context",
                                                       "report_feedback"}
    # And the same fence as the Claude branch: the flag off registers nothing.
    monkeypatch.setenv("CCSWITCH_SESSION_TOOLS_ENABLED", "false")
    await orch._setup_session_overlay("s1", identity="u@x", engine="codex")
    assert "s1" not in orch._overlays


def test_append_string_carries_the_instruction_when_enabled(monkeypatch):
    from server.sessions.claude_sdk_factory import _build_append_string
    from server.sessions.tools.instruction import SESSION_TOOLS_INSTRUCTION
    monkeypatch.delenv("CCSWITCH_SESSION_TOOLS_ENABLED", raising=False)
    assert SESSION_TOOLS_INSTRUCTION in _build_append_string()
    monkeypatch.setenv("CCSWITCH_SESSION_TOOLS_ENABLED", "false")
    assert SESSION_TOOLS_INSTRUCTION not in _build_append_string()
