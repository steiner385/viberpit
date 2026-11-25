"""The source daemon's half of a per-host display target (spec §2 "Env injection").

A session born with ``display_target`` decides ``remote`` only while the bridge's
forwarded sockets are live on this host, persists which desktop, and injects the
bridge env — read live, never a dead socket at spawn.
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import patch

import pytest

from server.sessions.desktop_session import DesktopUnavailable
from server.sessions.native_display import NativeDisplay
from server.tests.test_orchestrator_native_display import FakeNative, FakeX11, _orch, _row, _seed
from server.tests.test_orchestrator_native_display import spawn_env as spawn_env  # noqa: F401 — fixture

# Every remote degrade blanks the display vars: the SDK merges extra_env OVER the
# daemon's inherited env, so {} would let an inherited DISPLAY put windows on this
# (source) host's own screen (spec §8).
BLANK_ENV = {"WAYLAND_DISPLAY": "", "DISPLAY": "", "XAUTHORITY": ""}

LIVE_ENV = {"WAYLAND_DISPLAY": "wayland-viberpit-bridge", "XDG_RUNTIME_DIR": "/run/user/1000",
            "DISPLAY": "/run/user/1000/viberpit-bridge/X11",
            "XAUTHORITY": "/run/user/1000/viberpit-bridge/xauth"}


class _Bridge:
    """Stands in for display_bridge_env.remote_env (records every probe) and for this
    host's configured target (``current``, read by ``_current_display_target``)."""
    def __init__(self, state):
        self.state = state
        self.current: str | None = "dobby"
        self.calls: list[dict] = []

    def __call__(self, **kw):
        self.calls.append(kw)
        return self.state


@pytest.fixture
def bridge():
    from server.sessions.orchestrator import SessionOrchestrator
    b = _Bridge(dict(LIVE_ENV))

    async def _current(self):
        return b.current
    with patch("server.sessions.orchestrator.remote_env", b), \
            patch.object(SessionOrchestrator, "_current_display_target", _current):
        yield b


@pytest.mark.asyncio
async def test_create_session_with_target_and_live_bridge_is_remote_and_persists_target_host(
        spawn_env, bridge):
    x11, native = FakeX11(), FakeNative(available=False)
    orch = await _orch(spawn_env, x11, native)
    try:
        rec = await orch.create_session("n", account="acct", client_node="phone",
                                        display_target="dobby")
        assert rec.display_mode == "remote"
        assert rec.display_target_host == "dobby"
        assert rec.display_fallback_reason is None
        assert x11.started == []                     # no Xvfb lease for a remote session
        row = await _row(rec.id)
        assert row.display_mode == "remote" and row.display_target_host == "dobby"
        assert bridge.calls and bridge.calls[0]["uid"] == os.getuid()
        assert bridge.calls[0]["x11"] is True
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_local_client_with_native_still_wins_over_target(spawn_env, bridge):
    orch = await _orch(spawn_env, FakeX11(), FakeNative())
    try:
        rec = await orch.create_session("n", account="acct", client_node="dobby",
                                        display_target="dobby")
        assert rec.display_mode == "native" and rec.display_target_host is None
    finally:
        await orch.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["bridge_unavailable", "bridge_wrong_user"])
async def test_create_session_target_but_dead_bridge_is_virtual_with_reason(
        spawn_env, bridge, reason):
    bridge.state = reason
    x11 = FakeX11()
    orch = await _orch(spawn_env, x11, FakeNative(available=False))
    try:
        rec = await orch.create_session("n", account="acct", client_node="phone",
                                        display_target="dobby")
        assert rec.display_mode == "virtual"
        assert rec.display_fallback_reason == reason
        assert rec.display_target_host is None
        assert x11.started == [rec.id]
        row = await _row(rec.id)
        assert row.display_fallback_reason == reason and row.display_target_host is None
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_kill_switch_off_ignores_target(spawn_env, bridge):
    with patch.dict(os.environ, {"VIBERPIT_DISPLAY_BRIDGE_ENABLED": "false"}):
        orch = await _orch(spawn_env, FakeX11(), FakeNative(available=False))
        try:
            rec = await orch.create_session("n", account="acct", client_node="phone",
                                            display_target="dobby")
            assert rec.display_mode == "virtual"
            assert rec.display_fallback_reason is None     # local, not a fallback
            assert rec.display_target_host is None
            assert bridge.calls == []                       # never probed
        finally:
            await orch.stop()


@pytest.mark.asyncio
async def test_queued_and_held_remote_decision_persists(spawn_env, bridge):
    orch = await _orch(spawn_env, FakeX11(), FakeNative(available=False),
                       max_active_sessions=1)
    try:
        a = await orch.create_session("one", account="acct")
        q = await orch.create_session("q", account="acct", display_target="dobby")
        assert q.status == "queued" and q.display_mode == "remote"
        assert (await _row(q.id)).display_target_host == "dobby"
        bridge.state = "bridge_unavailable"           # promotion keeps the birth decision
        await orch.close_session(a.id)
        assert await orch._maybe_start_queued() == 1
        assert orch._records[q.id].display_mode == "remote"
        assert orch._records[q.id].display_target_host == "dobby"
        assert (await _row(q.id)).display_target_host == "dobby"
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_held_for_approval_remote_decision_persists(spawn_env, bridge):
    orch = await _orch(spawn_env, FakeX11(), FakeNative(available=False))
    try:
        rec = await orch.create_session("n", account="acct", model="fable",
                                        display_target="dobby")
        assert rec.status == "pending_approval"
        assert rec.display_mode == "remote" and rec.display_target_host == "dobby"
        assert (await _row(rec.id)).display_target_host == "dobby"

        async def _boom(*a, **k):
            raise RuntimeError("spawn exploded")
        orch._spawn_session = _boom  # type: ignore[method-assign]
        with pytest.raises(RuntimeError):
            await orch.resolve_model_approval(rec.id, "approve", identity="user@x")
        assert orch._records[rec.id].display_target_host == "dobby"
        assert (await _row(rec.id)).display_target_host == "dobby"
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_resume_keeps_persisted_remote_decision(spawn_env, bridge):
    x11, native = FakeX11(), FakeNative(available=False)
    orch = await _orch(spawn_env, x11, native)
    try:
        rec = await orch.create_session("n", account="acct", display_target="dobby")
        sid = rec.id
    finally:
        await orch.stop()
    x11b = FakeX11()
    orch2 = await _orch(spawn_env, x11b, FakeNative(available=False))
    try:
        await orch2.rehydrate()
        assert orch2._records[sid].display_mode == "remote"
        assert orch2._records[sid].display_target_host == "dobby"
        drv = await orch2.ensure_driver(sid)
        assert drv is not None
        assert x11b.started == []                    # a remote session never leases Xvfb
        assert orch2._display_env(sid) == LIVE_ENV
    finally:
        await orch2.stop()


def _recording_factory(seen):
    def rec_factory(cwd, cfg, cut, mode, resume, extra_env=None, *, on_pre_compact=None):
        seen["extra_env"] = extra_env
        class _F: ...
        return _F()
    return rec_factory


@pytest.mark.asyncio
async def test_display_env_remote_branch_returns_bridge_env(bridge):
    from server.sessions.orchestrator import SessionOrchestrator
    seen: dict = {}
    orch = SessionOrchestrator(sdk_factory=_recording_factory(seen), x11=FakeX11())
    _seed(orch, "s1", display_mode="remote", display_target_host="dobby")
    # The spawn probes the bridge off the loop first, then injects what it found.
    client = orch._session_factory("s1")("/tmp", "/cfg", None, "interactive", None)
    assert asyncio.iscoroutine(client)
    await client
    assert seen["extra_env"] == LIVE_ENV
    assert orch._display_env("s1") == LIVE_ENV
    # Bridge gone at the next spawn: no socket injected (a dead one never is), and the
    # display vars are blanked rather than left to inheritance.
    bridge.state = "bridge_unavailable"
    seen.clear()
    await orch._session_factory("s1")("/tmp", "/cfg", None, "interactive", None)
    assert seen["extra_env"] == BLANK_ENV
    assert orch._display_env("s1") == BLANK_ENV


def test_display_env_remote_with_no_probe_yet_is_blank(bridge):
    from server.sessions.orchestrator import SessionOrchestrator
    orch = SessionOrchestrator(sdk_factory=_recording_factory({}), x11=FakeX11())
    _seed(orch, "s1", display_mode="remote", display_target_host="dobby")
    assert orch._display_env("s1") == BLANK_ENV


@pytest.mark.asyncio
@pytest.mark.parametrize("degrade", ["dead_legs", "wrong_user", "no_probe", "stale_probe",
                                     "target_mismatch", "target_cleared"])
async def test_inherited_display_never_survives_a_remote_degrade(bridge, monkeypatch, degrade):
    """However the remote env degrades, the env the CLI is spawned with (inherited env
    overlaid by extra_env, as the SDK merges it) carries no display of this host."""
    import time as _time

    from server.sessions.orchestrator import SessionOrchestrator
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("XAUTHORITY", "/home/u/.Xauthority")
    orch = SessionOrchestrator(sdk_factory=_recording_factory({}), x11=FakeX11())
    _seed(orch, "s1", display_mode="remote", display_target_host="dobby")
    if degrade == "dead_legs":
        bridge.state = "bridge_unavailable"
    elif degrade == "wrong_user":
        bridge.state = "bridge_wrong_user"
    elif degrade == "target_mismatch":
        bridge.current = "zeta"
    elif degrade == "target_cleared":
        bridge.current = None
    if degrade != "no_probe":
        await orch._refresh_bridge()
    if degrade == "stale_probe":
        ts, state, cur = orch._bridge_probe
        orch._bridge_probe = (_time.monotonic() - 3600, state, cur)
    spawned = {**os.environ, **orch._display_env("s1")}
    assert spawned["DISPLAY"] == "" and spawned["WAYLAND_DISPLAY"] == ""
    assert spawned["XAUTHORITY"] == ""


class _Proc:
    returncode = None


@pytest.mark.asyncio
@pytest.mark.parametrize("state,platform", [
    (dict(LIVE_ENV), "wayland"),
    ({"WAYLAND_DISPLAY": "wayland-viberpit-bridge", "XDG_RUNTIME_DIR": "/run/user/1000"},
     "wayland"),
    ({"WAYLAND_DISPLAY": "", "DISPLAY": "/run/user/1000/viberpit-bridge/X11",
      "XAUTHORITY": "/x"}, "x11"),
])
async def test_open_browser_remote_uses_bridge_env_and_ozone_wayland(
        spawn_env, bridge, tmp_path, state, platform, monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("XAUTHORITY", "/home/u/.Xauthority")
    spawned: list[tuple[tuple, dict]] = []

    async def _spawn(*argv, **kw):
        spawned.append((argv, kw))
        return _Proc()
    native = NativeDisplay(run_dir=str(tmp_path), chromium_cmd="chromium", no_sandbox=False,
                           max_browsers=2, spawn=_spawn,
                           resolve=lambda: DesktopUnavailable(reason="no_graphical_session"))
    orch = await _orch(spawn_env, FakeX11(), native)
    try:
        rec = await orch.create_session("n", account="acct", display_target="dobby")
        assert rec.display_mode == "remote"
        bridge.state = state
        assert await orch.open_browser(rec.id, "https://a") == {"ok": True, "reason": None}
        argv, kw = spawned[-1]
        assert argv[0] == "chromium"
        assert f"--ozone-platform={platform}" in argv
        assert argv[-2:] == ("--", "https://a")
        for k, v in state.items():
            assert kw["env"][k] == v
        if platform == "x11":
            assert not kw["env"].get("WAYLAND_DISPLAY")
        # A leg the state omits is still blanked: nothing inherited (DISPLAY=:0 below)
        # may put the browser on this host's own screen.
        assert kw["env"]["DISPLAY"] == state.get("DISPLAY", "")
        assert kw["env"]["XAUTHORITY"] == state.get("XAUTHORITY", "")
        bridge.state = "bridge_wrong_user"
        assert await orch.open_browser(rec.id, None) == {"ok": False,
                                                          "reason": "bridge_wrong_user"}
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_old_daemon_sheds_display_target_and_stays_local(caplog):
    """The edge sends display_target only to daemons that bind it: an older one rejects
    the kwarg, and the retry drops just that key (the session stays local there)."""
    import logging
    from server.ipc.client import DaemonError
    from server.sessions.remote import RemoteOrchestrator
    caplog.set_level(logging.INFO, logger="server.sessions.remote")

    class _Old:
        def __init__(self): self.calls = []
        async def call(self, method, **params):
            self.calls.append((method, params))
            if "display_target" in params:
                raise DaemonError("TypeError", "create_session() got an unexpected keyword "
                                               "argument 'display_target'")
            return {"session": {"id": "s1", "name": "n", "account": "a",
                                "engine": "claude_code", "engineSessionId": "", "cols": 80,
                                "rows": 24, "cwd": "/tmp"}}
    c = _Old()
    rec = await RemoteOrchestrator(c).create_session(name="n", client_node="phone",
                                                     display_target="dobby")
    assert rec.id == "s1"
    assert len(c.calls) == 2
    assert "display_target" not in c.calls[1][1] and c.calls[1][1]["client_node"] == "phone"
    assert "predates the display bridge" in caplog.text


@pytest.mark.asyncio
async def test_target_moved_degrades_env_and_browser(spawn_env, bridge, tmp_path):
    """The bridge's sockets on this host lead to whatever this host targets NOW. A
    record born for another desktop (the target moved, or another host adopted the
    queued row) must not get them — its windows would land on the wrong desktop."""
    spawned: list = []

    async def _spawn(*argv, **kw):
        spawned.append(argv)
        return _Proc()
    native = NativeDisplay(run_dir=str(tmp_path), chromium_cmd="chromium", no_sandbox=False,
                           max_browsers=2, spawn=_spawn,
                           resolve=lambda: DesktopUnavailable(reason="no_graphical_session"))
    orch = await _orch(spawn_env, FakeX11(), native)
    try:
        rec = await orch.create_session("n", account="acct", display_target="dobby")
        assert rec.display_mode == "remote"
        assert orch._display_env(rec.id) == LIVE_ENV
        bridge.current = "zeta"                       # this host now targets another desktop
        await orch._refresh_bridge()
        assert orch._display_env(rec.id) == BLANK_ENV
        assert await orch.open_browser(rec.id, None) == {"ok": False,
                                                          "reason": "bridge_unavailable"}
        bridge.current = None                         # target cleared (or kill switch)
        await orch._refresh_bridge()
        assert orch._display_env(rec.id) == BLANK_ENV
        assert spawned == []
        # A new session naming a target this host no longer has is not remote either.
        bridge.current = "zeta"
        late = await orch.create_session("m", account="acct", display_target="dobby")
        assert late.display_mode == "virtual"
        assert late.display_fallback_reason == "bridge_unavailable"
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_current_display_target_reads_this_hosts_row(spawn_env):
    """The real reader behind the guard: this host's Host.display_target, parsed."""
    from server.db import db_session
    from server.db.models import Host
    orch = await _orch(spawn_env, FakeX11(), FakeNative(available=False))
    try:
        assert await orch._current_display_target() is None          # no row: local
        async with db_session() as db:
            db.add(Host(name="dobby", endpoint="unix", display_target="host:rocky"))
        assert await orch._current_display_target() == "rocky"
        with patch.dict(os.environ, {"VIBERPIT_DISPLAY_BRIDGE_ENABLED": "false"}):
            assert await orch._current_display_target() is None      # kill switch
    finally:
        await orch.stop()


# --- fix round 1: queued/held rows resolve their remote leg at actual spawn ----------

@pytest.mark.asyncio
@pytest.mark.parametrize("birth_reason", ["bridge_unavailable", "bridge_wrong_user"])
async def test_queued_bridge_fallback_re_resolves_remote_at_dequeue(spawn_env, bridge,
                                                                   birth_reason):
    """Born virtual only because the bridge was not up yet: the actual spawn re-asks the
    remote leg (spec §2 "Queued/held sessions resolve at actual spawn")."""
    x11 = FakeX11()
    orch = await _orch(spawn_env, x11, FakeNative(available=False), max_active_sessions=1)
    try:
        a = await orch.create_session("one", account="acct")
        bridge.state = birth_reason
        q = await orch.create_session("q", account="acct", display_target="dobby")
        assert q.status == "queued" and q.display_mode == "virtual"
        assert q.display_fallback_reason == birth_reason
        bridge.state = dict(LIVE_ENV)                 # the edge brought the bridge up
        await orch.close_session(a.id)
        assert await orch._maybe_start_queued() == 1
        assert orch._records[q.id].display_mode == "remote"
        assert orch._records[q.id].display_target_host == "dobby"
        assert orch._records[q.id].display_fallback_reason is None
        row = await _row(q.id)
        assert row.display_mode == "remote" and row.display_target_host == "dobby"
        assert row.display_fallback_reason is None
        assert q.id not in x11.started
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_queued_bridge_fallback_stays_virtual_when_bridge_still_down(spawn_env, bridge):
    orch = await _orch(spawn_env, FakeX11(), FakeNative(available=False), max_active_sessions=1)
    try:
        a = await orch.create_session("one", account="acct")
        bridge.state = "bridge_unavailable"
        q = await orch.create_session("q", account="acct", display_target="dobby")
        bridge.state = "bridge_wrong_user"
        await orch.close_session(a.id)
        assert await orch._maybe_start_queued() == 1
        assert orch._records[q.id].display_mode == "virtual"
        assert orch._records[q.id].display_fallback_reason == "bridge_wrong_user"
        assert orch._records[q.id].display_target_host is None
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_queued_bridge_fallback_without_target_keeps_prior(spawn_env, bridge):
    orch = await _orch(spawn_env, FakeX11(), FakeNative(available=False), max_active_sessions=1)
    try:
        a = await orch.create_session("one", account="acct")
        bridge.state = "bridge_unavailable"
        q = await orch.create_session("q", account="acct", display_target="dobby")
        bridge.state = dict(LIVE_ENV)
        bridge.current = None                         # target cleared meanwhile
        await orch.close_session(a.id)
        assert await orch._maybe_start_queued() == 1
        assert orch._records[q.id].display_mode == "virtual"
        assert orch._records[q.id].display_fallback_reason == "bridge_unavailable"
    finally:
        await orch.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,reason", [
    ("virtual", "no_graphical_session"),     # a native fallback
    ("virtual", None),                       # explicit virtual / no target
    ("native", None),
    ("remote", None),
])
async def test_only_bridge_fallback_priors_are_re_decided(spawn_env, bridge, mode, reason):
    """Only the remote leg of a bridge-fallback row re-resolves: every other persisted
    decision wins even with a live bridge and a configured target."""
    from types import SimpleNamespace
    orch = await _orch(spawn_env, FakeX11(), FakeNative(available=False))
    try:
        prior = SimpleNamespace(display_mode=mode, display_fallback_reason=reason,
                                display_target_host="dobby" if mode == "remote" else None)
        decision, _host, redecided = await orch._decide_display(prior, None, None, None)
        assert redecided is False
        assert tuple(decision) == (mode, reason)
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_held_bridge_fallback_re_resolves_remote_on_approve(spawn_env, bridge):
    orch = await _orch(spawn_env, FakeX11(), FakeNative(available=False))
    try:
        bridge.state = "bridge_unavailable"
        rec = await orch.create_session("n", account="acct", model="fable",
                                        display_target="dobby")
        assert rec.status == "pending_approval" and rec.display_mode == "virtual"
        bridge.state = dict(LIVE_ENV)
        await orch.resolve_model_approval(rec.id, "approve", identity="user@x")
        assert orch._records[rec.id].display_mode == "remote"
        assert orch._records[rec.id].display_target_host == "dobby"
        assert (await _row(rec.id)).display_mode == "remote"
    finally:
        await orch.stop()


# --- fix round 1: the edge's fallback hint ---------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("hint", ["bridge_not_accepted", "target_desktop_unavailable"])
async def test_fallback_hint_names_the_reason_when_local_check_fails(spawn_env, bridge, hint):
    bridge.state = "bridge_unavailable"
    orch = await _orch(spawn_env, FakeX11(), FakeNative(available=False))
    try:
        rec = await orch.create_session("n", account="acct", display_target="dobby",
                                        display_fallback_hint=hint)
        assert rec.display_mode == "virtual" and rec.display_fallback_reason == hint
        assert (await _row(rec.id)).display_fallback_reason == hint
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_fallback_hint_ignored_when_bridge_is_live(spawn_env, bridge):
    orch = await _orch(spawn_env, FakeX11(), FakeNative(available=False))
    try:
        rec = await orch.create_session("n", account="acct", display_target="dobby",
                                        display_fallback_hint="bridge_not_accepted")
        assert rec.display_mode == "remote" and rec.display_fallback_reason is None
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_unknown_fallback_hint_is_ignored(spawn_env, bridge):
    bridge.state = "bridge_wrong_user"
    orch = await _orch(spawn_env, FakeX11(), FakeNative(available=False))
    try:
        rec = await orch.create_session("n", account="acct", display_target="dobby",
                                        display_fallback_hint="made_up")
        assert rec.display_fallback_reason == "bridge_wrong_user"
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_fallback_hint_persists_on_queued_row(spawn_env, bridge):
    bridge.state = "bridge_unavailable"
    orch = await _orch(spawn_env, FakeX11(), FakeNative(available=False), max_active_sessions=1)
    try:
        await orch.create_session("one", account="acct")
        q = await orch.create_session("q", account="acct", display_target="dobby",
                                      display_fallback_hint="target_desktop_unavailable")
        assert q.status == "queued"
        assert q.display_fallback_reason == "target_desktop_unavailable"
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_old_daemon_sheds_display_fallback_hint(caplog):
    from server.ipc.client import DaemonError
    from server.sessions.remote import RemoteOrchestrator

    class _Old:
        def __init__(self): self.calls = []
        async def call(self, method, **params):
            self.calls.append((method, params))
            if "display_fallback_hint" in params:
                raise DaemonError("TypeError", "create_session() got an unexpected keyword "
                                               "argument 'display_fallback_hint'")
            return {"session": {"id": "s1", "name": "n", "account": "a",
                                "engine": "claude_code", "engineSessionId": "", "cols": 80,
                                "rows": 24, "cwd": "/tmp"}}
    c = _Old()
    await RemoteOrchestrator(c).create_session(name="n", display_target="dobby",
                                               display_fallback_hint="bridge_not_accepted")
    assert len(c.calls) == 2
    assert "display_fallback_hint" not in c.calls[1][1]
    assert c.calls[1][1]["display_target"] == "dobby"
