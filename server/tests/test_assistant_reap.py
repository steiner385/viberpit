"""Task 1.8: idle reap of an assistant drawer session, with resume-on-reopen.

Spec §4.7: an assistant idle for CCSWITCH_ASSISTANT_IDLE_S with no live turn gets its
driver closed (status -> 'ended'), but the row and its claude_session_id survive so the
next POST /api/assistant/session can resume the same thread. A turn that is still live
must NOT be reaped regardless of how long the record has been idle.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

import pytest
from claude_agent_sdk import TextBlock

from ccswitch.selector import Picked
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import ASSISTANT_KIND
from server.tests.fakes import fake_sdk as f
from server.tests.fakes.fake_sdk import FakeSDKClient


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


def _capturing_factory(clients: list[FakeSDKClient], messages=None):
    """Like ``fake_sdk_factory``, but stashes every ``FakeSDKClient`` it builds into
    *clients* — the orchestrator owns the driver/client construction internally, and a
    test that needs to ``.inject()`` an autonomous (query()-less) turn after the driver
    is live has no other way to get a handle back to that instance."""
    msgs = list(messages or [])

    def make(cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *,
             on_pre_compact=None, options_overlay=None):
        c = FakeSDKClient(list(msgs), on_can_use_tool=can_use_tool)
        clients.append(c)
        return c

    return make


async def _boot_orch(tmp_path, monkeypatch, sdk_factory):
    """Shared scaffolding: seed a cred slot, patch account selection/compose/trust
    (mirrors test_assistant_orchestrator.py's ``_make_orch`` / test_assistant_api.py's
    ``_client`` — ``create_session`` calls the real ``select_account`` otherwise, which
    raises ``AllAccountsBusy`` against an empty test account store before the reap code
    under test is ever reached), then build + start a SessionOrchestrator.

    Returns (orch, patches); the caller must stop() the orch and each patch."""
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

    patches = [
        patch("server.sessions.orchestrator.compose", return_value=cfg_dir),
        patch("server.sessions.orchestrator.configdir.trust_project"),
        patch("server.sessions.orchestrator.select_account", return_value=Picked("alpha")),
    ]
    for p in patches:
        p.start()
    orch = SessionOrchestrator(sdk_factory=sdk_factory, poll_interval=9999.0)
    await orch.start()
    return orch, patches


async def _teardown(orch, patches) -> None:
    await orch.stop()
    for p in patches:
        p.stop()


@pytest.mark.asyncio
async def test_reap_after_idle_keeps_row_and_resume_id(isolated_db, tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_ASSISTANT_IDLE_S", "100")   # get_settings() is uncached

    clients: list[FakeSDKClient] = []
    # Script one real turn (SystemMessage subtype=init + a ResultMessage carrying the
    # same session id) so the driver actually captures a claude_session_id via
    # on_session_id (driver.py:929-939) — with no turn ever sent, claude_session_id
    # stays "" for the whole test and the resume-on-reopen assertion below would prove
    # nothing (spec §4.7's actual contract is that a REAL id survives the reap).
    scripted = [f.sys_init("claude-abc"), f.result(session_id="claude-abc")]
    orch, patches = await _boot_orch(tmp_path, monkeypatch, _capturing_factory(clients, scripted))
    try:
        a = await orch.create_session(name="Assistant", cwd=str(tmp_path), identity="u@x",
                                      kind=ASSISTANT_KIND, owner_identity="u@x")
        await orch.send_prompt(a.id, "hi")
        # Wait for BOTH the id capture and the turn to actually finish draining —
        # on_session_id fires from inside the pump's own drain loop, so there is a
        # brief window where the id is already set but drain_alive/_turn_live() has
        # not flipped False yet; reaping while it's still true would (correctly) be
        # skipped by the turn-live guard, which is not what this test is checking.
        for _ in range(200):
            if orch.get_session(a.id).claude_session_id and not orch._drivers[a.id]._turn_live():
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("claude_session_id never captured or turn never finished draining")
        sid_before = orch.get_session(a.id).claude_session_id
        assert sid_before == "claude-abc"

        orch.note_user_interaction(a.id)
        t0 = orch.get_session(a.id).last_activity_at
        assert await orch._reap_idle_assistants_once(now=t0 + 50) == []
        assert await orch._reap_idle_assistants_once(now=t0 + 101) == [a.id]
        rec = orch.get_session(a.id)
        assert rec.status == "ended" and rec.detached is True
        # The actual spec §4.7 contract: the id needed to resume (`resume=`) survives
        # the reap unchanged, not merely "the row wasn't deleted".
        assert rec.claude_session_id == sid_before
        assert await orch.find_assistant("u@x") is not None     # thread survives
    finally:
        await _teardown(orch, patches)


@pytest.mark.asyncio
async def test_reap_skips_assistant_with_a_live_turn(isolated_db, tmp_path, monkeypatch):
    """A turn still in flight must never be torn down, no matter how idle the record
    looks — this is exactly what ``driver._turn_live()`` guards in
    ``_reap_idle_assistants_once``. A regression that flipped that check's polarity
    (or dropped it) would pass every other test in this file, since none of them ever
    put the driver into a live-turn state."""
    monkeypatch.setenv("CCSWITCH_ASSISTANT_IDLE_S", "100")

    clients: list[FakeSDKClient] = []
    orch, patches = await _boot_orch(tmp_path, monkeypatch, _capturing_factory(clients))
    try:
        a = await orch.create_session(name="Assistant", cwd=str(tmp_path), identity="u@x",
                                      kind=ASSISTANT_KIND, owner_identity="u@x")
        orch.note_user_interaction(a.id)
        t0 = orch.get_session(a.id).last_activity_at

        # The CLI starts a turn ON ITS OWN — a background task notification the model
        # keeps working on, with no query() behind it (see
        # test_driver_autonomous_turn.py). `.inject()` delivers it with no closing
        # ResultMessage, so the driver's idle listener opens an autonomous turn
        # (driver.py:1003-1017, `_open_autonomous_turn`) and it never ends —
        # `_turn_live()` stays True indefinitely, exactly the state under test.
        client = clients[-1]
        client.inject([f.assistant(TextBlock(text="still working..."), uuid="bg1")])
        for _ in range(200):
            if orch._drivers[a.id]._turn_live():
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("autonomous turn never opened — _turn_live() stayed False")

        # Far past the idle threshold — but the live turn must still win.
        assert await orch._reap_idle_assistants_once(now=t0 + 10_000) == []
        rec = orch.get_session(a.id)
        # The autonomous turn's own status callback moves it to "in_progress" — the
        # point under test is that it is NOT "ended" and the driver was NOT torn down,
        # not the exact in-flight status label.
        assert rec.status != "ended" and rec.detached is False
        assert a.id in orch._drivers
    finally:
        await _teardown(orch, patches)
