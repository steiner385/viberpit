"""Task 1.8: idle reap of an assistant drawer session, with resume-on-reopen.

Spec §4.7: an assistant idle for CCSWITCH_ASSISTANT_IDLE_S with no live turn gets its
driver closed (status -> 'ended'), but the row and its claude_session_id survive so the
next POST /api/assistant/session can resume the same thread.
"""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from ccswitch.selector import Picked
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import ASSISTANT_KIND
from server.tests.fakes.fake_factory import fake_sdk_factory


def _seed_account(name: str) -> None:
    """Mirrors test_assistant_orchestrator.py's helper: a real cred slot so
    ``select_account`` (unpatched paths in ``_isolated_dir_for``) has something
    to isolate."""
    from ccswitch import paths
    from ccswitch.creds import write_json_secure
    write_json_secure(paths.slot_path(name), {
        "accessToken": f"{name}-tok", "refreshToken": f"{name}-ref",
        "expiresAt": int(time.time() * 1000) + 9_000_000, "subscriptionType": "max",
    })
    if not paths.shared_mcp_path().exists():
        write_json_secure(paths.shared_mcp_path(), {})


@pytest.mark.asyncio
async def test_reap_after_idle_keeps_row_and_resume_id(isolated_db, tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_ASSISTANT_IDLE_S", "100")   # get_settings() is uncached

    store_home = tmp_path / "store"
    store_home.mkdir(exist_ok=True)
    shared = tmp_path / "dot-claude"
    shared.mkdir(exist_ok=True)
    (shared / "settings.json").write_text("{}")
    (shared / "projects").mkdir(exist_ok=True)
    cfg_dir = tmp_path / "cfg" / "alpha"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / ".claude.json").write_text("{}")

    monkeypatch.setenv("CCSWITCH_HOME", str(store_home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(shared))
    _seed_account("alpha")

    with patch("server.sessions.orchestrator.compose", return_value=cfg_dir), \
         patch("server.sessions.orchestrator.configdir.trust_project"), \
         patch("server.sessions.orchestrator.select_account", return_value=Picked("alpha")):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)
        await orch.start()
        try:
            a = await orch.create_session(name="Assistant", cwd=str(tmp_path), identity="u@x",
                                          kind=ASSISTANT_KIND, owner_identity="u@x")
            orch.note_user_interaction(a.id)
            t0 = orch.get_session(a.id).last_activity_at
            assert await orch._reap_idle_assistants_once(now=t0 + 50) == []
            assert await orch._reap_idle_assistants_once(now=t0 + 101) == [a.id]
            rec = orch.get_session(a.id)
            assert rec.status == "ended" and rec.detached is True
            assert await orch.find_assistant("u@x") is not None     # thread survives
        finally:
            await orch.stop()
