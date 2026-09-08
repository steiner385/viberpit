"""Task 1.8: idle reap of an assistant drawer session, with resume-on-reopen.

Spec §4.7: an assistant idle for CCSWITCH_ASSISTANT_IDLE_S with no live turn gets its
driver closed (status -> 'ended'), but the row and its engine_session_id survive so the
next POST /api/assistant/session can resume the same thread. A turn that is still live
must NOT be reaped regardless of how long the record has been idle.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

import pytest
from claude_agent_sdk import TextBlock

from ccswitch import brand
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


def _capturing_factory(clients: list[FakeSDKClient], messages=None,
                       overlays: list | None = None):
    """Like ``fake_sdk_factory``, but stashes every ``FakeSDKClient`` it builds into
    *clients* — the orchestrator owns the driver/client construction internally, and a
    test that needs to ``.inject()`` an autonomous (query()-less) turn after the driver
    is live has no other way to get a handle back to that instance.

    When *overlays* is given, the ``options_overlay`` each build was handed is appended
    to it (``None`` for a build that got none) — the SDK factory is the ONLY place that
    observes what actually made a session an assistant."""
    msgs = list(messages or [])

    def make(cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *,
             on_pre_compact=None, options_overlay=None):
        if overlays is not None:
            overlays.append(options_overlay)
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
    # same session id) so the driver actually captures an engine_session_id via
    # on_session_id — with no turn ever sent, engine_session_id
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
        # brief window where the id is already set but drain_alive/turn_active have
        # not flipped False yet; reaping while it's still true would (correctly) be
        # skipped by the turn-live guard, which is not what this test is checking.
        for _ in range(200):
            d = orch._drivers[a.id]
            if (orch.get_session(a.id).engine_session_id
                    and not (d.turn_active or d.drain_alive)):
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("engine_session_id never captured or turn never finished draining")
        sid_before = orch.get_session(a.id).engine_session_id
        assert sid_before == "claude-abc"

        orch.note_user_interaction(a.id)
        t0 = orch.get_session(a.id).last_activity_at
        assert await orch._reap_idle_assistants_once(now=t0 + 50) == []
        assert await orch._reap_idle_assistants_once(now=t0 + 101) == [a.id]
        rec = orch.get_session(a.id)
        assert rec.status == "ended" and rec.detached is True
        # The actual spec §4.7 contract: the id needed to resume (`resume=`) survives
        # the reap unchanged, not merely "the row wasn't deleted".
        assert rec.engine_session_id == sid_before
        assert await orch.find_assistant("u@x") is not None     # thread survives
    finally:
        await _teardown(orch, patches)


@pytest.mark.asyncio
async def test_reap_skips_assistant_with_a_live_turn(isolated_db, tmp_path, monkeypatch):
    """A turn still in flight must never be torn down, no matter how idle the record
    looks — this is exactly what the ``turn_active or drain_alive`` guard covers in
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
        # (claude_driver.py, `_open_autonomous_turn`) and it never ends —
        # `turn_active` stays True indefinitely, exactly the state under test.
        client = clients[-1]
        client.inject([f.assistant(TextBlock(text="still working..."), uuid="bg1")])
        for _ in range(200):
            if orch._drivers[a.id].turn_active or orch._drivers[a.id].drain_alive:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("autonomous turn never opened — turn_active stayed False")

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


@pytest.mark.asyncio
async def test_resume_after_reap_rebuilds_the_assistant_overlay(
        isolated_db, tmp_path, monkeypatch):
    """The security-relevant half of the reap: reopening the drawer after an idle reap
    must come back as an ASSISTANT, not as an unrestricted coding agent.

    The options overlay is the only thing supplying `tools=[]`, the built-in denylist,
    `strict_mcp_config` (which hides the operator's ~10 MCP servers) and `max_turns` —
    and the reap drops it with the driver. ensure_driver did not re-register it, so
    after the first 15-minute idle window the drawer resumed with the full coding-agent
    tool surface AND no `mcp__ccswitch__*` tools of its own.

    Asserted where it actually matters: on the overlay the SDK FACTORY was handed for
    the RESUMED driver, not on the existence of a helper.
    """
    monkeypatch.setenv("CCSWITCH_ASSISTANT_IDLE_S", "100")

    clients: list[FakeSDKClient] = []
    overlays: list = []
    orch, patches = await _boot_orch(
        tmp_path, monkeypatch, _capturing_factory(clients, overlays=overlays))
    try:
        a = await orch.create_session(name="Assistant", cwd=str(tmp_path), identity="u@x",
                                      kind=ASSISTANT_KIND, owner_identity="u@x")
        spawn_overlay = overlays[-1]
        assert spawn_overlay is not None, "spawn built the assistant with no overlay"

        orch.note_user_interaction(a.id)
        t0 = orch.get_session(a.id).last_activity_at
        assert await orch._reap_idle_assistants_once(now=t0 + 101) == [a.id]
        assert a.id not in orch._overlays, "reap must drop the stale overlay"

        driver = await orch.ensure_driver(a.id, "u@x")
        assert driver is not None
        resumed = overlays[-1]
        assert resumed is not None, (
            "the RESUMED driver's factory got no options_overlay — the drawer came back "
            "as an unrestricted coding agent")
        assert resumed["tools"] == []
        assert "Bash" in resumed["disallowed_tools"] and "Write" in resumed["disallowed_tools"]
        assert resumed["strict_mcp_config"] is True
        assert set(resumed["mcp_servers"]) == {brand.MCP_SERVER_NAME}
        assert resumed["system_prompt"] == spawn_overlay["system_prompt"]
        assert resumed["max_turns"] == spawn_overlay["max_turns"]

        # REBUILT, not preserved: the spawn's MCP tool handlers close over an
        # AssistantApi whose httpx client driver.aclose() shut down during the reap, so
        # reusing the old overlay would resume with dead tools.
        assert (resumed["mcp_servers"][brand.MCP_SERVER_NAME]
                is not spawn_overlay["mcp_servers"][brand.MCP_SERVER_NAME])
        assert driver._assistant_api is not None
        assert driver._assistant_api._client.is_closed is False
    finally:
        await _teardown(orch, patches)


@pytest.mark.asyncio
async def test_resumed_assistant_token_is_bound_to_the_reopening_identity(
        isolated_db, tmp_path, monkeypatch):
    """`POST /api/assistant/session` resumes with `ensure_driver(id, identity)`. With no
    identity the freshly minted loopback token would be bound to SYSTEM_IDENTITY, and
    every tool call the drawer makes would act as "system" rather than as the user who
    opened it. (Re-minting is required in the first place because the token's max-age is
    only `assistant_idle_s * 4` — it can expire while the row is still resumable.)"""
    monkeypatch.setenv("CCSWITCH_ASSISTANT_IDLE_S", "100")
    from server.assistant.token import verify_token

    clients: list[FakeSDKClient] = []
    orch, patches = await _boot_orch(tmp_path, monkeypatch, _capturing_factory(clients))
    try:
        a = await orch.create_session(name="Assistant", cwd=str(tmp_path), identity="u@x",
                                      kind=ASSISTANT_KIND, owner_identity="u@x")
        orch.note_user_interaction(a.id)
        t0 = orch.get_session(a.id).last_activity_at
        assert await orch._reap_idle_assistants_once(now=t0 + 101) == [a.id]

        driver = await orch.ensure_driver(a.id, "u@x")
        assert driver is not None
        auth = driver._assistant_api._client.headers["Authorization"]
        claims = verify_token(auth.removeprefix("Bearer "), max_age_s=3600)
        assert claims is not None
        assert claims.identity == "u@x" and claims.session_id == a.id
    finally:
        await _teardown(orch, patches)


class _SlowX11:
    """An X11Manager stand-in whose stop_display awaits — the real one does, and that
    await is what opened the reap-vs-prompt window."""

    def __init__(self):
        self.during_stop = None

    def env_for(self, session_id):
        return {}

    async def start_display(self, session_id):
        return None

    async def stop_display(self, session_id):
        # Runs INSIDE the reap's teardown, i.e. exactly the window a user's prompt
        # could land in.
        if self.during_stop is not None:
            await self.during_stop(session_id)


@pytest.mark.asyncio
async def test_prompt_racing_the_reap_is_rejected_not_silently_lost(
        isolated_db, tmp_path, monkeypatch):
    """The reap's X11 teardown awaits, so control yields mid-teardown. A prompt landing
    in that window used to pass send_prompt's `self._drivers.get(...)` check, start a
    turn, and return "sent" — and then the reap tore the driver down under it and the
    user's message was gone, with no promptRejected.

    Deterministic because the race window IS the stop_display await: the fake calls
    send_prompt from inside it. The fix is ordering — `_teardown_driver_state` pops the
    driver BEFORE the display stop, so the prompt sees no driver and is REJECTED.
    """
    monkeypatch.setenv("CCSWITCH_ASSISTANT_IDLE_S", "100")

    clients: list[FakeSDKClient] = []
    orch, patches = await _boot_orch(tmp_path, monkeypatch, _capturing_factory(clients))
    x11 = _SlowX11()
    orch._x11 = x11
    try:
        a = await orch.create_session(name="Assistant", cwd=str(tmp_path), identity="u@x",
                                      kind=ASSISTANT_KIND, owner_identity="u@x")
        orch.note_user_interaction(a.id)
        t0 = orch.get_session(a.id).last_activity_at

        raced: list[str] = []

        async def _prompt_during_stop(sid):
            await asyncio.sleep(0)      # a real yield, like the real stop_display
            raced.append(await orch.send_prompt(sid, "are you still there?"))

        x11.during_stop = _prompt_during_stop
        assert await orch._reap_idle_assistants_once(now=t0 + 101) == [a.id]

        assert raced == ["no_driver"], (
            "a prompt racing the reap was accepted as 'sent' and then torn down "
            "mid-turn — silently lost")
    finally:
        await _teardown(orch, patches)


@pytest.mark.asyncio
async def test_reap_publishes_its_teardown_in_releasing(isolated_db, tmp_path, monkeypatch):
    """Unlike close/archive, the reap KEEPS the record — so a resume during its aclose()
    is the expected user action (reopening the drawer right at the reap boundary).
    ensure_driver waits on `_releasing` before building a replacement driver with the
    same `resume=`, precisely so the new `claude` process cannot race the old one's
    transcript flush and lose its tail. The reap must therefore publish its teardown
    there instead of awaiting aclose() inline behind no lock anyone respects.

    WHEN it publishes is the whole contract, so the claim is inspected from INSIDE the
    slow `stop_display` — not merely after the sweep returns. The real teardown budgets
    ~10 s for the display stop, and publishing after it left a multi-second window in
    which the driver was gone, the old `claude` process alive, `_releasing` empty and
    the status not yet "ended" — so a user reopening the drawer at the idle boundary got
    a SECOND claude process on the same claude_session_id, whose overlay the sweep then
    deleted and whose session it then stamped "ended".
    """
    monkeypatch.setenv("CCSWITCH_ASSISTANT_IDLE_S", "100")

    clients: list[FakeSDKClient] = []
    orch, patches = await _boot_orch(tmp_path, monkeypatch, _capturing_factory(clients))
    x11 = _SlowX11()
    orch._x11 = x11
    try:
        a = await orch.create_session(name="Assistant", cwd=str(tmp_path), identity="u@x",
                                      kind=ASSISTANT_KIND, owner_identity="u@x")
        orch.note_user_interaction(a.id)
        t0 = orch.get_session(a.id).last_activity_at

        gate = asyncio.Event()
        driver = orch._drivers[a.id]
        real_aclose = driver.aclose

        async def _gated_aclose():
            await gate.wait()
            await real_aclose()

        driver.aclose = _gated_aclose

        # Sampled mid-teardown: the display stop is the slow await the claim must
        # already have crossed.
        during: dict = {}

        async def _sample_during_stop(sid):
            await asyncio.sleep(0)      # a real yield, like the real stop_display
            during["releasing"] = orch._releasing.get(sid)
            during["overlay"] = sid in orch._overlays
            during["status"] = orch.get_session(sid).status
            during["driver"] = sid in orch._drivers

        x11.during_stop = _sample_during_stop

        assert await orch._reap_idle_assistants_once(now=t0 + 101) == [a.id]

        assert during["releasing"] is not None, (
            "`_releasing` was still empty during the display stop — ensure_driver would "
            "have built a second claude process on the same claude_session_id")
        assert during["overlay"] is False, (
            "the overlay was still registered during the display stop — a resume in that "
            "window registers its own, which the sweep then deletes")
        assert during["status"] == "ended", (
            "the record still looked live during the display stop — the sweep would go "
            "on to stamp a just-resumed session 'ended'")
        assert during["driver"] is False
        assert orch._releasing.get(a.id) is not None, "reap did not publish its release"

        resume = asyncio.create_task(orch.ensure_driver(a.id, "u@x"))
        await asyncio.sleep(0.1)
        assert not resume.done(), "ensure_driver rebuilt while the old driver was closing"

        gate.set()
        rebuilt = await asyncio.wait_for(resume, timeout=10)
        assert rebuilt is not None and rebuilt is not driver
        # The release always clears its own entry, so a stuck teardown can never make
        # the drawer permanently unopenable.
        assert a.id not in orch._releasing
    finally:
        await _teardown(orch, patches)


def test_all_three_driver_teardowns_route_through_one_helper():
    """close_session, archive_session and the assistant reap ran three VERBATIM copies
    of the same "stop the X11 display + six dict pops + aclose()" block. The third copy
    had already drifted harmfully (it additionally dropped `_overlays`, which
    is what turned every post-reap resume into an unrestricted coding session).

    Consolidated into `_teardown_driver_state`. This guards the consolidation the only
    way a fourth copy could be caught: no other teardown site may pop `_drivers` itself.
    """
    import inspect

    from server.sessions.orchestrator import SessionOrchestrator

    for name in ("close_session", "archive_session", "_reap_idle_assistants_once"):
        src = inspect.getsource(getattr(SessionOrchestrator, name))
        assert "_teardown_driver_state(" in src, f"{name} does not use the shared teardown"
        assert "self._drivers.pop(" not in src, (
            f"{name} pops _drivers itself — that is the copy this helper replaced")
        assert "stop_display(" not in src, (
            f"{name} stops the X11 display itself — _teardown_driver_state owns that")
