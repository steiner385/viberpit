"""D-23: an opt-in, fail-closed sandbox for Claude sessions (server side).

Plan: docs/superpowers/plans/2026-11-06-desktop-parity-d23-sandbox.md, Task 1. Facts:
docs/superpowers/specs/2026-11-06-d23-sandbox-spike.md (§2.1 settings, §2.3 preflight,
§2.4 the after-spawn check, §2.5 failures). Review Focus:

1. Fail-closed everywhere: spawn failure, a failed check, a missing control request, a
   respawn, a resume — none runs a session that asked for the sandbox without it.
2. Turning on always respawns.
3. The full settings block is always sent; the escape hatches are fixed in code.
4. The toggle needs a person and is audited.
5. The kill switch removes everything.

Only the CLI is faked (fake SDK clients, fake control frames, fake subprocesses): no
``claude``, ``bwrap`` or model call ever runs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import select

from server.db import db_session, init_db
from server.db.models import AppSettings, AuditEvent, Session as DbSession
from server.ipc.client import DaemonError
from server.sessions import claude_sdk_factory
from server.sessions import sandbox as sb
from server.sessions import session_actions
from server.sessions.claude_driver import ClaudeAgentDriver
from server.sessions.engine import AgentEngine, capabilities_to_wire
from server.sessions.facade import InProcessOrchestratorAdapter
from server.sessions.fleet_orchestrator import _MOVE_BLOCKED, _PER_SESSION, FleetOrchestrator
from server.sessions.orchestrator import (_ECHOED_PAUSE_REASONS, _TERMINAL_PAUSE_REASONS,
                                          PAUSE_REASONS, SessionOrchestrator)
from server.sessions.permissions import PermissionRegistry
from server.sessions.remote import RemoteOrchestrator
from server.sessions.session_actions import ActionError
from server.sessions.session_move import SessionMoving
from server.sessions.types import SessionRecord
from server.tests.fakes.fake_sdk import FakeSDKClient
from server.tests.test_sessions_actions_api import _ENV, _H, _audit_events, _make_client

SID = "5a4db0c5-0000-0000-0000-000000000001"

#: The spike §2.1 block, restated here on purpose: the test is the second witness.
SPIKE_BLOCK = {
    "sandbox": {
        "enabled": True,
        "failIfUnavailable": True,
        "autoAllowBashIfSandboxed": False,
        "allowUnsandboxedCommands": False,
        "excludedCommands": [],
        "network": {
            "allowedDomains": ["github.com", "*.github.com", "*.githubusercontent.com"],
            "strictAllowlist": True,
            "allowAllUnixSockets": False,
        },
    }
}

#: A `get_sandbox_dialog` reply meeting every §2.4 condition.
GOOD_DIALOG: dict[str, Any] = {
    "supported": True, "locked": False, "enabled": True, "enabled_in_settings": True,
    "unavailable_reason": None, "mode": "regular", "unsandboxed_fallback": False,
    "dependencies": {"errors": [], "warnings": []}, "excluded_commands": [],
    "restrictions": {"network_allowed_domains": list(sb.ALLOWED_DOMAINS),
                     "allow_all_unix_sockets": False, "fs_allow_write": ["."]},
}


def _no_warnings(caplog) -> list[tuple[str, str]]:
    return [(r.name, r.getMessage()) for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.fixture(autouse=True)
def _fresh_preflight():
    sb.reset_preflight_cache()
    yield
    sb.reset_preflight_cache()


def _ok_preflight() -> sb.Preflight:
    return sb.Preflight(ok=True, reason=None, checks=(sb.PreflightCheck("bwrap", True),),
                        cli_version="2.1.319", checked_at=1.0)


def _bad_preflight(reason: str = "socat is not installed") -> sb.Preflight:
    return sb.Preflight(ok=False, reason=reason,
                        checks=(sb.PreflightCheck("socat", False, reason),), checked_at=1.0)


# ── Review Focus 3: the exact settings block ─────────────────────────────────


def test_settings_block_is_the_spike_block_exactly():
    assert sb.settings_block() == SPIKE_BLOCK


def test_settings_block_is_a_fresh_copy_so_nothing_can_loosen_the_next_spawn():
    first = sb.settings_block()
    first["sandbox"]["allowUnsandboxedCommands"] = True
    first["sandbox"]["network"]["strictAllowlist"] = False
    first["sandbox"]["network"]["allowedDomains"].append("evil.example")
    assert sb.settings_block() == SPIKE_BLOCK


def test_the_spawn_settings_json_carries_the_whole_block_beside_session_flags():
    out = json.loads(claude_sdk_factory.spawn_settings(
        {"fastMode": True, **sb.settings_block()}))
    assert out["sandbox"] == SPIKE_BLOCK["sandbox"]
    assert out["fastMode"] is True and out["enableAllProjectMcpServers"] is True


# ── the after-spawn check (§2.4) ─────────────────────────────────────────────


def test_a_dialog_meeting_every_condition_passes():
    assert sb.check_dialog(GOOD_DIALOG) == sb.DialogVerdict(True)


def _dialog(**over) -> dict:
    d = json.loads(json.dumps(GOOD_DIALOG))
    for key, value in over.items():
        if "__" in key:
            outer, inner = key.split("__")
            d[outer][inner] = value
        else:
            d[key] = value
    return d


@pytest.mark.parametrize("reply,expected", [
    (_dialog(enabled=False), False),
    (_dialog(enabled_in_settings=False), False),
    (_dialog(unavailable_reason="socat not installed"), True),
    (_dialog(dependencies__errors=["socat not installed"]), True),
    (_dialog(dependencies__warnings=["seccomp not available - unix socket access not "
                                     "restricted"]), True),
    (_dialog(mode="auto-allow"), False),
    (_dialog(unsandboxed_fallback=True), False),
    (_dialog(restrictions__allow_all_unix_sockets=True), False),
    ({k: v for k, v in GOOD_DIALOG.items() if k != "restrictions"}, False),
    ({k: v for k, v in GOOD_DIALOG.items() if k != "dependencies"}, False),
    ({}, False),
    (None, False),
    ("nope", False),
])
def test_every_condition_is_required(reply, expected):
    verdict = sb.check_dialog(reply)
    assert verdict.ok is False and verdict.reason
    assert verdict.expected is expected


def test_startup_failure_reads_the_clis_own_refusal():
    text = ("Claude Code returned an error result: Sandbox required but unavailable: socat "
            "not installed. Set sandbox.failIfUnavailable=false to allow unsandboxed "
            "execution.")
    assert sb.startup_failure([None, text]).startswith("Claude Code returned an error")
    assert sb.startup_failure(["noise", "❌ Sandbox Error: bwrap failed"]) == \
        "❌ Sandbox Error: bwrap failed"
    assert sb.startup_failure(["Command failed with exit code 1", None]) is None


# ── host preflight (§2.3): each failure reason ──────────────────────────────


class _Run:
    """A fake ``subprocess.run``: ``bwrap --version`` answers *version*, the namespace
    probe answers *probe_rc* (or raises TimeoutExpired)."""

    def __init__(self, version="bubblewrap 0.11.1", probe_rc: int | None = 0):
        self.version, self.probe_rc = version, probe_rc
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, argv, *, capture_output, text, timeout):
        self.calls.append((list(argv), timeout))
        if "--version" in argv:
            return SimpleNamespace(returncode=0, stdout=self.version, stderr="")
        if self.probe_rc is None:
            raise subprocess.TimeoutExpired(argv, timeout)
        return SimpleNamespace(returncode=self.probe_rc, stdout="",
                               stderr="" if self.probe_rc == 0 else "setting up uid map")


def _gh_dir(tmp_path, name: str, token: bool):
    d = tmp_path / name
    d.mkdir()
    body = "github.com:\n    user: tony\n" + ("    oauth_token: gho_x\n" if token else "")
    (d / "hosts.yml").write_text(body)
    return str(d)


def _preflight(monkeypatch, tmp_path, *, which=None, run=None, cli="2.1.319",
               dedicated_token=True, ambient_token=False):
    monkeypatch.setenv("VIBERPIT_GH_CONFIG_DIR",
                       _gh_dir(tmp_path, "vp-gh", dedicated_token))
    monkeypatch.setenv("GH_CONFIG_DIR", _gh_dir(tmp_path, "gh", ambient_token))
    present = which or {"bwrap": "/usr/bin/bwrap", "socat": "/usr/bin/socat"}
    return sb.run_preflight(which=present.get, run=run or _Run(),
                            cli_version=lambda: cli, sysctls=lambda: {"k": "1"},
                            clock=lambda: 5.0)


def test_preflight_passes_on_a_ready_host_and_bounds_every_subprocess(monkeypatch, tmp_path):
    run = _Run()
    pf = _preflight(monkeypatch, tmp_path, run=run)
    assert pf.ok is True and pf.reason is None and pf.cli_version == "2.1.319"
    assert [c.name for c in pf.checks] == ["bwrap", "bwrap_version", "socat", "bwrap_probe",
                                           "gh", "cli_version"]
    probe = ["/usr/bin/bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--unshare-net", "true"]
    assert (probe, sb.PROBE_TIMEOUT_S) in run.calls
    assert all(t == sb.PROBE_TIMEOUT_S for _, t in run.calls)
    wire = pf.to_wire()
    assert wire["ok"] is True and wire["diagnostics"] == {"k": "1"}
    assert wire["checkedAt"].startswith("1970-01-01T00:00:05")


@pytest.mark.parametrize("kw,reason", [
    ({"which": {"socat": "/usr/bin/socat"}}, "bubblewrap (bwrap) is not installed"),
    ({"run": _Run(version="bubblewrap 0.4.9")}, "bubblewrap 0.4.9 is older than 0.5.0"),
    ({"which": {"bwrap": "/usr/bin/bwrap"}}, "socat is not installed"),
    ({"run": _Run(probe_rc=1)}, "bwrap could not create a network namespace "
                                "(setting up uid map)"),
    ({"run": _Run(probe_rc=None)}, "bwrap could not create a network namespace "
                                   "(timed out after 5s)"),
    ({"dedicated_token": False}, "gh token is keyring-only; sandboxed git cannot "
                                 "authenticate"),
    ({"cli": "2.1.292"}, "claude CLI 2.1.292 is older than the verified 2.1.293"),
    ({"cli": None}, "the claude CLI version could not be read"),
])
def test_each_preflight_failure_is_the_toggles_reason(monkeypatch, tmp_path, kw, reason):
    pf = _preflight(monkeypatch, tmp_path, **kw)
    assert pf.ok is False and pf.reason == reason


def test_an_ambient_file_backed_gh_config_also_passes(monkeypatch, tmp_path):
    pf = _preflight(monkeypatch, tmp_path, dedicated_token=False, ambient_token=True)
    assert pf.ok is True


def test_preflight_is_cached_per_host_and_refreshed_on_demand(monkeypatch):
    runs: list[int] = []

    def fake_run_preflight():
        runs.append(1)
        return _ok_preflight()
    monkeypatch.setattr(sb, "run_preflight", fake_run_preflight)

    async def go():
        a = await sb.preflight()
        b = await sb.preflight()
        c = await sb.preflight(refresh=True)
        return a, b, c
    a, b, c = asyncio.run(go())
    assert len(runs) == 2 and a is b and sb.cached_preflight() is c


def test_session_env_gives_gh_a_file_backed_config_only_when_there_is_one(monkeypatch,
                                                                          tmp_path):
    monkeypatch.setenv("VIBERPIT_GH_CONFIG_DIR", _gh_dir(tmp_path, "a", True))
    env = sb.session_env()
    assert env == {"GH_NO_UPDATE_NOTIFIER": "1",
                   "GH_CONFIG_DIR": str(tmp_path / "a")}
    monkeypatch.setenv("VIBERPIT_GH_CONFIG_DIR", _gh_dir(tmp_path, "b", False))
    assert sb.session_env() == {"GH_NO_UPDATE_NOTIFIER": "1"}
    monkeypatch.delenv("VIBERPIT_GH_CONFIG_DIR")
    assert sb.session_env() == {"GH_NO_UPDATE_NOTIFIER": "1"}


# ── capability, Codex, kill switch (Review Focus 5) ──────────────────────────


def test_sandbox_is_a_claude_capability_only():
    from server.codex.capabilities import CODEX_CAPABILITIES
    assert ClaudeAgentDriver.capabilities.sandbox is True
    assert CODEX_CAPABILITIES.sandbox is False
    assert capabilities_to_wire(ClaudeAgentDriver.capabilities)["sandbox"] is True
    assert capabilities_to_wire(CODEX_CAPABILITIES)["sandbox"] is False


def _rec(**kw) -> SessionRecord:
    base = dict(id=SID, name="t", account="acct1", engine=AgentEngine.CLAUDE_CODE,
                engine_session_id="csid", cols=80, rows=24, cwd="/tmp/x", sandbox=True)
    base.update(kw)
    return SessionRecord(**base)


def test_codex_sessions_never_spawn_sandboxed():
    from server.codex.capabilities import CODEX_CAPABILITIES
    rec = _rec(engine=AgentEngine.CODEX)
    assert sb.spawn_requested(rec, CODEX_CAPABILITIES) is False
    assert sb.session_view(rec, CODEX_CAPABILITIES)["state"] == sb.STATE_OFF


def test_a_legacy_row_with_no_recorded_setting_spawns_unsandboxed():
    assert sb.spawn_requested(_rec(sandbox=None), ClaudeAgentDriver.capabilities) is False
    assert sb.spawn_requested(_rec(sandbox=True), ClaudeAgentDriver.capabilities) is True
    assert sb.spawn_requested(_rec(sandbox=True, kind="assistant"),
                              ClaudeAgentDriver.capabilities) is False


def test_the_kill_switch_removes_everything(monkeypatch):
    monkeypatch.setenv("VIBERPIT_SANDBOX_ENABLED", "false")
    caps = ClaudeAgentDriver.capabilities
    assert sb.sandbox_enabled() is False
    assert capabilities_to_wire(caps)["sandbox"] is False
    assert sb.spawn_requested(_rec(sandbox=True), caps) is False
    assert sb.session_view(_rec(sandbox=True, sandbox_state="active"), caps) == {
        "enabled": False, "state": "off", "reason": None}


def test_the_session_record_carries_its_sandbox_across_the_rpc():
    rec = _rec(sandbox=True, sandbox_state="failed", sandbox_reason="socat not installed")
    back = SessionRecord.from_dict(rec.to_dict())
    assert (back.sandbox, back.sandbox_state, back.sandbox_reason) == (
        True, "failed", "socat not installed")
    assert sb.session_view(back, ClaudeAgentDriver.capabilities) == {
        "enabled": True, "state": "failed", "reason": "socat not installed"}


def test_sandbox_failed_is_a_terminal_quiet_pause_reason():
    assert sb.SANDBOX_PAUSE_REASON in PAUSE_REASONS
    assert sb.SANDBOX_PAUSE_REASON in _TERMINAL_PAUSE_REASONS
    assert sb.SANDBOX_PAUSE_REASON in _ECHOED_PAUSE_REASONS


# ── the driver: verify after spawn, fail closed (Review Focus 1) ─────────────


class FakeControl:
    """The SDK ``Query`` control channel: answers each request from a script. An
    exception in the script is raised (the SDK raises a control ``error`` reply)."""

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.sent: list[dict] = []

    async def _send_control_request(self, request: dict, timeout: float = 60.0):
        self.sent.append(request)
        reply = self.replies.pop(0) if self.replies else {}
        if isinstance(reply, BaseException):
            raise reply
        return reply


class _Client(FakeSDKClient):
    def __init__(self, control: FakeControl | None, enter_error: Exception | None = None,
                 stderr=None, stderr_lines: tuple[str, ...] = ()):
        super().__init__([], startup_frames=[])
        if control is not None:
            self._query = control
        self.enter_error = enter_error
        self.exited = 0
        self._stderr = stderr
        self._stderr_lines = stderr_lines

    async def __aenter__(self):
        if self.enter_error is not None:
            for line in self._stderr_lines:
                if self._stderr is not None:
                    self._stderr(line)
            raise self.enter_error
        return await super().__aenter__()

    async def __aexit__(self, *exc):
        self.exited += 1
        return await super().__aexit__(*exc)


def _sandboxed_driver(control: FakeControl | None, *, enter_error=None,
                      stderr_lines=(), sandbox=True):
    calls: list[dict] = []
    clients: list[_Client] = []
    states: list[tuple[str, str | None]] = []

    def make(cwd, config_dir, can_use_tool, mode, resume, *, on_pre_compact=None,
             **kw):
        calls.append(dict(kw))
        c = _Client(control, enter_error, stderr=kw.get("stderr"),
                    stderr_lines=stderr_lines)
        clients.append(c)
        return c

    async def on_state(session_id, state, reason):
        states.append((state, reason))

    d = ClaudeAgentDriver(SID, "acct1", "/tmp/r", "/tmp/cfg", mode="full-auto",
                          sdk_factory=make, registry=PermissionRegistry(), identity="u",
                          sandbox=sandbox, on_sandbox_state=on_state)
    return d, calls, clients, states


@pytest.mark.asyncio
async def test_a_verified_sandbox_starts_and_reports_active(caplog):
    control = FakeControl(GOOD_DIALOG)
    d, calls, clients, states = _sandboxed_driver(control)
    await d.start()
    try:
        assert calls[0]["sandbox"] is True and callable(calls[0]["stderr"])
        assert control.sent[0] == {"subtype": "get_sandbox_dialog"}
        assert states == [("pending", None), ("active", None)]
        assert clients[0].exited == 0
    finally:
        await d.aclose()
    assert _no_warnings(caplog) == []


@pytest.mark.asyncio
async def test_an_unsandboxed_driver_sends_nothing_sandbox_related():
    control = FakeControl()
    d, calls, _, states = _sandboxed_driver(control, sandbox=False)
    await d.start()
    await d.aclose()
    assert "sandbox" not in calls[0] and "stderr" not in calls[0]
    assert not [r for r in control.sent if r.get("subtype") == "get_sandbox_dialog"]
    assert states == []


@pytest.mark.asyncio
@pytest.mark.parametrize("control,expected,warns", [
    (FakeControl(_dialog(enabled=False)), "the sandbox did not start", True),
    (FakeControl(_dialog(unavailable_reason="socat not installed")),
     "sandbox unavailable: socat not installed", False),
    (FakeControl(Exception("get_sandbox_dialog is not available on this connection")),
     "get_sandbox_dialog failed: get_sandbox_dialog is not available on this connection",
     True),
    (None, "the SDK client has no control channel, so the sandbox could not be verified",
     True),
])
async def test_a_failed_absent_or_erroring_check_holds_and_never_runs(control, expected,
                                                                      warns, caplog):
    caplog.set_level(logging.INFO)
    d, calls, clients, states = _sandboxed_driver(control)
    with pytest.raises(sb.SandboxError) as ei:
        await d.start()
    assert ei.value.code == sb.SANDBOX_UNVERIFIED and ei.value.reason == expected
    assert len(calls) == 1, "never retried — least of all unsandboxed"
    assert clients[0].exited == 1, "the CLI is closed before start() returns"
    assert states[-1] == ("failed", expected)
    with pytest.raises(RuntimeError):
        await d.send_prompt("go")
    warnings = _no_warnings(caplog)
    if warns:
        assert [m for _, m in warnings if "sandbox.verify_failed" in m]
    else:
        assert warnings == []
    await d.aclose()


@pytest.mark.asyncio
async def test_a_startup_refusal_fails_the_session_and_is_never_retried_unsandboxed(caplog):
    caplog.set_level(logging.INFO)
    refusal = Exception(
        "Claude Code returned an error result: Sandbox required but unavailable: socat "
        "not installed. Set sandbox.failIfUnavailable=false to allow unsandboxed execution.")
    d, calls, clients, states = _sandboxed_driver(FakeControl(), enter_error=refusal)
    with pytest.raises(sb.SandboxError) as ei:
        await d.start()
    assert ei.value.code == sb.SANDBOX_START_FAILED
    assert "socat not installed" in ei.value.reason
    assert len(calls) == 1 and calls[0]["sandbox"] is True
    assert states[-1][0] == "failed"
    assert _no_warnings(caplog) == []
    await d.aclose()


@pytest.mark.asyncio
async def test_a_sandbox_error_on_stderr_is_a_startup_failure():
    d, calls, _, states = _sandboxed_driver(
        FakeControl(), enter_error=Exception("Command failed with exit code 1"),
        stderr_lines=("starting", "❌ Sandbox Error: bwrap: setting up uid map"))
    with pytest.raises(sb.SandboxError) as ei:
        await d.start()
    assert ei.value.code == sb.SANDBOX_START_FAILED
    assert ei.value.reason == "❌ Sandbox Error: bwrap: setting up uid map"
    await d.aclose()


@pytest.mark.asyncio
async def test_an_unrelated_start_failure_is_left_as_it_was():
    d, calls, _, _ = _sandboxed_driver(FakeControl(),
                                       enter_error=RuntimeError("token expired"))
    with pytest.raises(RuntimeError, match="token expired"):
        await d.start()
    assert len(calls) == 1
    await d.aclose()


# ── the orchestrator: spawn, resume, respawn ────────────────────────────────


class _World:
    """One fake CLI per spawn. ``dialogs`` scripts each spawn's ``get_sandbox_dialog``
    reply (the last one repeats); every spawn's factory kwargs are recorded."""

    def __init__(self, *dialogs: Any) -> None:
        self.dialogs = list(dialogs) or [GOOD_DIALOG]
        self.spawns: list[dict] = []
        self.controls: list[FakeControl] = []

    def factory(self, cwd, cfg, can_use_tool, mode, resume, *, on_pre_compact=None,
                options_overlay=None, flag_settings=None, extra_env=None, stderr=None):
        self.spawns.append({"flag_settings": flag_settings, "extra_env": extra_env,
                            "stderr": stderr, "resume": resume})
        reply = self.dialogs.pop(0) if len(self.dialogs) > 1 else self.dialogs[0]
        control = FakeControl(reply)
        self.controls.append(control)
        return _Client(control)

    def sandboxed(self, i: int = -1) -> bool:
        return bool((self.spawns[i]["flag_settings"] or {}).get("sandbox"))


def _fake_usage(account: str):
    from datetime import datetime, timedelta, timezone
    from viberpit.quota import anthropic_snapshot
    reset = datetime.now(timezone.utc) + timedelta(hours=5)
    return anthropic_snapshot(10.0, reset, 10.0, reset, False)


@pytest.fixture
def orch_env(tmp_path, monkeypatch):
    """Account plumbing for a real orchestrator on fake CLIs (as test_session_fork)."""
    import time
    from unittest.mock import patch as _patch
    from viberpit import paths
    from viberpit.creds import write_json_secure
    from viberpit.selector import Picked
    store = tmp_path / "store"
    store.mkdir()
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / ".claude.json").write_text("{}")
    monkeypatch.setenv("VIBERPIT_HOME", str(store))
    patches = [_patch("server.sessions.orchestrator.compose", return_value=cfg),
               _patch("server.sessions.orchestrator.configdir.trust_project"),
               _patch("server.sessions.orchestrator.select_account",
                      return_value=Picked("acct1"))]
    for p in patches:
        p.start()
    write_json_secure(paths.slot_path("acct1"), {
        "accessToken": "t", "refreshToken": "r",
        "expiresAt": int(time.time() * 1000) + 9_000_000, "subscriptionType": "max"})
    if not paths.shared_mcp_path().exists():
        write_json_secure(paths.shared_mcp_path(), {})
    yield cfg
    for p in patches:
        p.stop()


async def _orch(tmp_path, world: _World, *, sandbox: bool | None = True,
                status: str = "idle", engine: AgentEngine = AgentEngine.CLAUDE_CODE,
                default: bool | None = None, paused_reason: str | None = None):
    cwd = tmp_path / "wt"
    cwd.mkdir(exist_ok=True)
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with db_session() as db:
        db.add(DbSession(id=SID, name="t", account_name="acct1", engine_session_id="csid",
                         status="idle", cwd=str(cwd), sandbox=sandbox,
                         paused_reason=paused_reason))
        if default is not None:
            row = await db.get(AppSettings, "singleton")
            if row is None:
                db.add(AppSettings(id="singleton", sandbox_default=default))
            else:
                row.sandbox_default = default
    orch = SessionOrchestrator(sdk_factory=world.factory, repo_root=str(tmp_path / "root"),
                               poll_interval=9999.0, usage_fn=_fake_usage)
    await orch.rehydrate()
    orch._records[SID].status = status
    # Set on the record, not the row: this host has no Codex runner to rehydrate onto.
    orch._records[SID].engine = engine
    if paused_reason:
        orch._records[SID].paused_reason = paused_reason
    return orch


async def _row(sid: str = SID) -> DbSession:
    async with db_session() as db:
        row = await db.get(DbSession, sid)
        assert row is not None
        return row


async def _audits(type_: str) -> list[tuple[str, dict, str | None]]:
    async with db_session() as db:
        rows = (await db.execute(select(AuditEvent).where(
            AuditEvent.type == type_))).scalars().all()
        return [(r.identity, r.detail, r.via) for r in rows]


def test_a_sandboxed_spawn_sends_the_block_and_the_env_and_is_verified(orch_env, tmp_path,
                                                                       monkeypatch):
    monkeypatch.setenv("VIBERPIT_GH_CONFIG_DIR", _gh_dir(tmp_path, "vp", True))
    world = _World()

    async def go():
        orch = await _orch(tmp_path, world)
        await orch.ensure_driver(SID)
        row = await _row()
        rec = orch._records[SID]
        await orch.stop()
        return row, rec
    row, rec = asyncio.run(go())
    spawn = world.spawns[0]
    assert spawn["flag_settings"]["sandbox"] == SPIKE_BLOCK["sandbox"]
    assert spawn["extra_env"]["GH_NO_UPDATE_NOTIFIER"] == "1"
    assert spawn["extra_env"]["GH_CONFIG_DIR"] == str(tmp_path / "vp")
    assert callable(spawn["stderr"])
    assert world.controls[0].sent[0] == {"subtype": "get_sandbox_dialog"}
    assert rec.sandbox_state == "active" and row.sandbox_state == "active"


def test_an_unsandboxed_session_spawns_exactly_as_before(orch_env, tmp_path):
    world = _World()

    async def go():
        orch = await _orch(tmp_path, world, sandbox=False)
        await orch.ensure_driver(SID)
        await orch.stop()
    asyncio.run(go())
    assert not world.sandboxed(0)
    assert world.spawns[0]["stderr"] is None
    assert "GH_NO_UPDATE_NOTIFIER" not in (world.spawns[0]["extra_env"] or {})
    assert world.controls[0].sent == []


def test_the_kill_switch_spawns_a_recorded_sandbox_session_with_nothing(orch_env, tmp_path,
                                                                        monkeypatch):
    monkeypatch.setenv("VIBERPIT_SANDBOX_ENABLED", "false")
    world = _World()

    async def go():
        orch = await _orch(tmp_path, world, sandbox=True)
        await orch.ensure_driver(SID)
        await orch.stop()
    asyncio.run(go())
    assert not world.sandboxed(0)
    assert world.controls[0].sent == []


def test_a_resume_whose_check_fails_is_held_and_not_rebuilt_until_a_person_acts(orch_env, 
        tmp_path, caplog):
    caplog.set_level(logging.INFO)
    world = _World(_dialog(unavailable_reason="socat not installed"))

    async def go():
        orch = await _orch(tmp_path, world)
        with pytest.raises(sb.SandboxError) as first:
            await orch.ensure_driver(SID)
        rec = orch._records[SID]
        with pytest.raises(sb.SandboxError) as second:
            await orch.ensure_driver(SID)
        row = await _row()
        await orch.stop()
        return first.value, second.value, rec, row, orch
    first, second, rec, row, orch = asyncio.run(go())
    assert first.code == sb.SANDBOX_UNVERIFIED
    assert second.code == sb.SANDBOX_HELD
    assert len(world.spawns) == 1, "the hold is not retried, sandboxed or not"
    assert rec.status == "needs_attention" and rec.paused_reason == sb.SANDBOX_PAUSE_REASON
    assert row.sandbox_state == "failed"
    assert row.sandbox_reason == "sandbox unavailable: socat not installed"
    assert SID not in orch._drivers
    assert _no_warnings(caplog) == []


def test_a_respawn_whose_check_fails_parks_with_the_terminal_reason(orch_env, tmp_path):
    world = _World(GOOD_DIALOG, _dialog(enabled=False))

    async def go():
        orch = await _orch(tmp_path, world)
        await orch.ensure_driver(SID)
        rec = orch._records[SID]
        result = await orch._reassign(rec, rec.account, reason="reload",
                                      replay_last_prompt=False)
        await orch.stop()
        return result, rec, orch
    result, rec, orch = asyncio.run(go())
    assert result == "failed"
    assert [world.sandboxed(i) for i in range(2)] == [True, True]
    assert rec.paused_reason == sb.SANDBOX_PAUSE_REASON
    assert rec.sandbox_state == "failed" and SID not in orch._drivers


def test_a_new_session_records_the_default_and_spawns_sandboxed(orch_env, tmp_path, monkeypatch):
    world = _World()
    monkeypatch.setattr(sb, "preflight", _async(_ok_preflight()))

    async def go():
        orch = await _orch(tmp_path, world, default=True)
        rec = await orch.create_session(name="n", account="acct1",
                                        cwd=str(tmp_path / "wt"))
        row = await _row(rec.id)
        await orch.stop()
        return rec, row
    rec, row = asyncio.run(go())
    assert row.sandbox is True and rec.sandbox is True
    assert world.sandboxed(-1) and row.sandbox_state == "active"


def test_a_new_session_with_the_default_off_records_off(orch_env, tmp_path):
    world = _World()

    async def go():
        orch = await _orch(tmp_path, world, default=None)
        rec = await orch.create_session(name="n", account="acct1",
                                        cwd=str(tmp_path / "wt"))
        row = await _row(rec.id)
        await orch.stop()
        return row
    row = asyncio.run(go())
    assert row.sandbox is False and not world.sandboxed(-1)


def test_a_new_session_whose_sandbox_cannot_start_fails_and_registers_nothing(orch_env, tmp_path):
    world = _World(_dialog(unavailable_reason="socat not installed"))

    async def go():
        orch = await _orch(tmp_path, world, default=True)
        with pytest.raises(sb.SandboxError) as ei:
            await orch.create_session(name="n", account="acct1",
                                      cwd=str(tmp_path / "wt"))
        drivers = dict(orch._drivers)
        await orch.stop()
        return ei.value, drivers
    err, drivers = asyncio.run(go())
    assert err.code == sb.SANDBOX_UNVERIFIED and drivers == {}


# ── the toggle (Review Focus 2, 4) ──────────────────────────────────────────


def _async(value):
    async def f(*a, **k):
        return value
    return f


def test_turning_on_respawns_verified_and_is_audited(orch_env, tmp_path, monkeypatch):
    world = _World()
    monkeypatch.setattr(sb, "preflight", _async(_ok_preflight()))

    async def go():
        orch = await _orch(tmp_path, world, sandbox=False)
        await orch.ensure_driver(SID)
        first = orch._drivers[SID]
        out = await orch.set_session_sandbox(SID, True, identity="u@x", via="tailnet")
        second = orch._drivers[SID]
        row = await _row()
        audits = await _audits("session.sandbox")
        await orch.stop()
        return out, first, second, row, audits
    out, first, second, row, audits = asyncio.run(go())
    assert first is not second, "turning on always respawns — no live enable"
    assert [world.sandboxed(i) for i in range(2)] == [False, True]
    assert out["enabled"] is True and out["state"] == "active" and out["respawned"] is True
    assert row.sandbox is True and row.sandbox_state == "active"
    assert [r for r in world.controls[0].sent if r.get("subtype") == "apply_flag_settings"
            and "sandbox" in r.get("settings", {})] == []
    assert audits == [("u@x", {"sessionId": SID, "enabled": True, "state": "active",
                               "reason": None, "respawned": True}, "tailnet")]


def test_turning_on_a_detached_session_respawns_it_too(orch_env, tmp_path, monkeypatch):
    world = _World()
    monkeypatch.setattr(sb, "preflight", _async(_ok_preflight()))

    async def go():
        orch = await _orch(tmp_path, world, sandbox=False)
        out = await orch.set_session_sandbox(SID, True, identity="u@x", via=None)
        await orch.stop()
        return out
    out = asyncio.run(go())
    assert world.sandboxed(0) and out["state"] == "active"


def test_turning_on_where_the_check_fails_holds_and_says_why(orch_env, tmp_path, monkeypatch):
    world = _World(GOOD_DIALOG, _dialog(enabled=False))
    monkeypatch.setattr(sb, "preflight", _async(_ok_preflight()))

    async def go():
        orch = await _orch(tmp_path, world, sandbox=False)
        await orch.ensure_driver(SID)
        out = await orch.set_session_sandbox(SID, True, identity="u@x", via=None)
        rec = orch._records[SID]
        audits = await _audits("session.sandbox")
        await orch.stop()
        return out, rec, audits
    out, rec, audits = asyncio.run(go())
    assert out["state"] == "failed" and out["reason"] == "the sandbox did not start"
    assert out["held"] is True and rec.paused_reason == sb.SANDBOX_PAUSE_REASON
    assert audits[0][1]["state"] == "failed"


def test_turn_off_and_continue_releases_a_held_session_unsandboxed(orch_env, tmp_path):
    world = _World(_dialog(enabled=False), GOOD_DIALOG)

    async def go():
        orch = await _orch(tmp_path, world)
        with pytest.raises(sb.SandboxError):
            await orch.ensure_driver(SID)
        out = await orch.set_session_sandbox(SID, False, identity="u@x", via=None)
        rec = orch._records[SID]
        row = await _row()
        await orch.stop()
        return out, rec, row
    out, rec, row = asyncio.run(go())
    assert [world.sandboxed(i) for i in range(2)] == [True, False]
    assert out == {"sessionId": SID, "enabled": False, "state": "off", "reason": None,
                   "held": False, "respawned": True}
    assert rec.paused_reason is None and row.sandbox is False


def test_retry_respawns_a_held_session_sandboxed(orch_env, tmp_path, monkeypatch):
    world = _World(_dialog(enabled=False), GOOD_DIALOG)
    monkeypatch.setattr(sb, "preflight", _async(_ok_preflight()))

    async def go():
        orch = await _orch(tmp_path, world)
        with pytest.raises(sb.SandboxError):
            await orch.ensure_driver(SID)
        out = await orch.set_session_sandbox(SID, True, identity="u@x", via=None)
        rec = orch._records[SID]
        await orch.stop()
        return out, rec
    out, rec = asyncio.run(go())
    assert [world.sandboxed(i) for i in range(2)] == [True, True]
    assert out["state"] == "active" and rec.paused_reason is None


@pytest.mark.parametrize("setup,code", [
    ({"status": "in_progress"}, sb.SANDBOX_SESSION_BUSY),
    ({"status": "archived"}, sb.SANDBOX_SESSION_ENDED),
    ({"engine": AgentEngine.CODEX}, sb.SANDBOX_UNSUPPORTED),
])
def test_the_toggle_is_refused_while_busy_ended_or_on_codex(orch_env, tmp_path, monkeypatch, setup,
                                                            code):
    world = _World()
    monkeypatch.setattr(sb, "preflight", _async(_ok_preflight()))

    async def go():
        orch = await _orch(tmp_path, world, sandbox=False, **setup)
        with pytest.raises(sb.SandboxError) as ei:
            await orch.set_session_sandbox(SID, True, identity="u@x", via=None)
        audits = await _audits("session.sandbox")
        await orch.stop()
        return ei.value, audits
    err, audits = asyncio.run(go())
    assert err.code == code and world.spawns == [] and audits == []


def test_turning_on_where_the_preflight_fails_is_refused_with_its_reason(orch_env, tmp_path,
                                                                         monkeypatch):
    world = _World()
    monkeypatch.setattr(sb, "preflight", _async(_bad_preflight()))

    async def go():
        orch = await _orch(tmp_path, world, sandbox=False)
        with pytest.raises(sb.SandboxError) as ei:
            await orch.set_session_sandbox(SID, True, identity="u@x", via=None)
        await orch.stop()
        return ei.value
    err = asyncio.run(go())
    assert err.code == sb.SANDBOX_UNAVAILABLE and err.reason == "socat is not installed"
    assert world.spawns == []


def test_the_toggle_is_refused_with_the_kill_switch_off(orch_env, tmp_path, monkeypatch):
    monkeypatch.setenv("VIBERPIT_SANDBOX_ENABLED", "false")
    world = _World()

    async def go():
        orch = await _orch(tmp_path, world, sandbox=False)
        with pytest.raises(sb.SandboxError) as ei:
            await orch.set_session_sandbox(SID, True, identity="u@x", via=None)
        facts = await orch.sandbox_facts(SID)
        await orch.stop()
        return ei.value, facts
    err, facts = asyncio.run(go())
    assert err.code == sb.SANDBOX_DISABLED
    assert facts["capability"] is False and facts["canToggle"] is False


def test_sandbox_facts_carry_the_state_and_the_preflight(orch_env, tmp_path, monkeypatch):
    world = _World()
    monkeypatch.setattr(sb, "preflight", _async(_bad_preflight()))

    async def go():
        orch = await _orch(tmp_path, world, sandbox=False)
        facts = await orch.sandbox_facts(SID, refresh=True)
        await orch.stop()
        return facts
    facts = asyncio.run(go())
    assert facts["sessionId"] == SID and facts["capability"] is True
    assert facts["enabled"] is False and facts["state"] == "off"
    assert facts["available"] is False and facts["unavailableReason"] == "socat is not installed"
    assert facts["preflight"]["ok"] is False
    assert facts["busy"] is False and facts["held"] is False
    assert facts["canToggle"] is False   # turning ON needs the preflight
    assert facts["allowedDomains"] == list(sb.ALLOWED_DOMAINS)


# ── routes: person-only, audited by the owning daemon ───────────────────────


class _ToggleOrch:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.calls: list[tuple] = []

    async def set_session_sandbox(self, sid, enabled, *, identity="system", via=None):
        self.calls.append((sid, enabled, identity, via))
        if self.error is not None:
            raise self.error
        return {"sessionId": sid, "enabled": enabled, "state": "active", "reason": None,
                "held": False, "respawned": True}

    async def sandbox_facts(self, sid, *, refresh=False):
        self.calls.append(("facts", sid, refresh))
        if self.error is not None:
            raise self.error
        return {"sessionId": sid}


def _route_client(error: Exception | None = None):
    client, fake = _make_client()
    orch = _ToggleOrch(error)
    fake.set_session_sandbox = orch.set_session_sandbox
    fake.sandbox_facts = orch.sandbox_facts
    return client, orch


def test_the_toggle_route_hands_the_person_to_the_daemon_and_writes_no_audit():
    client, orch = _route_client()
    with patch.dict(os.environ, _ENV), client:
        r = client.post(f"/api/sessions/{SID}/sandbox", json={"enabled": True}, headers=_H)
        events = client.portal.call(_audit_events)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "active"
    assert orch.calls[0][:3] == (SID, True, "testuser@example.com")
    assert not [e for e in events if e[0] == "session.sandbox"]


def test_a_token_caller_cannot_flip_the_toggle(monkeypatch):
    from server.auth import identity
    monkeypatch.setattr(identity, "attested_front_end_hop", lambda _conn: False)
    client, orch = _route_client()
    with patch.dict(os.environ, _ENV), client:
        r = client.post(f"/api/sessions/{SID}/sandbox", json={"enabled": False}, headers=_H)
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "SESSION_CONTROL_HUMAN_ONLY"
    assert orch.calls == []


def test_session_actions_refuse_the_system_identity():
    orch = _ToggleOrch()
    with pytest.raises(ActionError) as ei:
        asyncio.run(session_actions.set_sandbox(orch, session_id=SID, enabled=True,
                                                identity="system", via=None))
    assert ei.value.http == 403 and orch.calls == []


@pytest.mark.parametrize("error,status,code", [
    (sb.SandboxError(sb.SANDBOX_SESSION_BUSY), 409, sb.SANDBOX_SESSION_BUSY),
    (sb.SandboxError(sb.SANDBOX_UNAVAILABLE, "socat is not installed"), 409,
     sb.SANDBOX_UNAVAILABLE),
    (KeyError(SID), 404, None),
    (DaemonError("UnknownMethod", "set_session_sandbox"), 409, "daemon_outdated"),
])
def test_the_toggle_route_maps_refusals(error, status, code):
    client, _ = _route_client(error)
    with patch.dict(os.environ, _ENV), client:
        r = client.post(f"/api/sessions/{SID}/sandbox", json={"enabled": True}, headers=_H)
    assert r.status_code == status, r.text
    if code:
        assert r.json()["detail"]["code"] == code
    if code == sb.SANDBOX_UNAVAILABLE:
        assert r.json()["detail"]["reason"] == "socat is not installed"


def test_the_facts_route_reads_the_owning_daemon_and_can_refresh():
    client, orch = _route_client()
    with patch.dict(os.environ, _ENV), client:
        r = client.get(f"/api/sessions/{SID}/sandbox?refresh=true", headers=_H)
    assert r.status_code == 200 and r.json() == {"sessionId": SID}
    assert orch.calls == [("facts", SID, True)]


def test_the_session_payload_carries_the_sandbox_view():
    from server.api.sessions import _record_to_dict
    out = _record_to_dict(_rec(sandbox=True, sandbox_state="active"))
    assert out["sandbox"] == {"enabled": True, "state": "active", "reason": None}


@pytest.mark.parametrize("error", [
    sb.SandboxError(sb.SANDBOX_HELD, "the sandbox did not start"),
    DaemonError("SandboxError", "x", {"code": sb.SANDBOX_HELD,
                                      "reason": "the sandbox did not start"}),
])
def test_a_prompt_to_a_held_session_is_a_409_not_a_crash(error):
    client, fake = _make_client()
    fake._record = _rec(sandbox=True, sandbox_state="failed",
                        paused_reason=sb.SANDBOX_PAUSE_REASON)

    async def ensure_driver(sid, identity="system"):
        raise error
    fake.ensure_driver = ensure_driver
    with patch.dict(os.environ, _ENV), client:
        r = client.post(f"/api/sessions/{SID}/prompt", json={"text": "go"}, headers=_H)
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == {"code": sb.SANDBOX_HELD, "message": sb.MESSAGES[sb.SANDBOX_HELD],
                                  "reason": "the sandbox did not start"}


def test_create_maps_a_sandbox_start_failure_to_409():
    client, fake = _make_client()

    async def create_session(**kw):
        raise sb.SandboxError(sb.SANDBOX_START_FAILED, "Sandbox required but unavailable")
    fake.create_session = create_session
    with patch.dict(os.environ, _ENV), client:
        r = client.post("/api/sessions", json={"name": "x"}, headers=_H)
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == sb.SANDBOX_START_FAILED
    assert r.json()["error"]["reason"] == "Sandbox required but unavailable"


def test_settings_carry_the_default_and_the_capability():
    client, _ = _make_client()
    with patch.dict(os.environ, _ENV), client:
        before = client.get("/api/settings", headers=_H).json()
        after = client.put("/api/settings", json={"sandboxDefault": True}, headers=_H).json()
    assert before["sandboxDefault"] is False and before["sandboxCapability"] is True
    assert after["sandboxDefault"] is True


def test_settings_hide_the_capability_with_the_kill_switch_off():
    client, _ = _make_client()
    with patch.dict(os.environ, {**_ENV, "VIBERPIT_SANDBOX_ENABLED": "false"}), client:
        out = client.get("/api/settings", headers=_H).json()
    assert out["sandboxCapability"] is False


def test_a_token_caller_cannot_change_the_default(monkeypatch):
    from server.auth import identity
    monkeypatch.setattr(identity, "attested_front_end_hop", lambda _conn: False)
    client, _ = _make_client()
    with patch.dict(os.environ, _ENV), client:
        r = client.put("/api/settings", json={"sandboxDefault": True}, headers=_H)
    assert r.status_code == 403


# ── RPC / remote / facade / fleet ───────────────────────────────────────────


class _RecordingClient:
    def __init__(self, result=None, error: Exception | None = None):
        self.result, self.error = result, error
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method, *, timeout=None, **kwargs):
        self.calls.append((method, kwargs))
        if self.error is not None:
            raise self.error
        return self.result


@pytest.mark.asyncio
async def test_remote_forwards_and_rebuilds_refusals():
    client = _RecordingClient(result={"ok": 1})
    ro = RemoteOrchestrator(client)
    await ro.sandbox_facts(SID, refresh=True)
    await ro.set_session_sandbox(SID, True, identity="u@x", via="tailnet")
    assert client.calls == [
        ("sandbox_facts", {"session_id": SID, "refresh": True}),
        ("set_session_sandbox", {"session_id": SID, "enabled": True, "identity": "u@x",
                                 "via": "tailnet"}),
    ]
    with pytest.raises(sb.SandboxError) as ei:
        await RemoteOrchestrator(_RecordingClient(error=DaemonError(
            "SandboxError", "x", {"code": sb.SANDBOX_UNAVAILABLE,
                                  "reason": "socat is not installed"}))
        ).set_session_sandbox(SID, True)
    assert ei.value.code == sb.SANDBOX_UNAVAILABLE
    assert ei.value.reason == "socat is not installed"
    with pytest.raises(KeyError):
        await RemoteOrchestrator(_RecordingClient(
            error=DaemonError("KeyError", "nope"))).sandbox_facts("nope")
    with pytest.raises(DaemonError):  # an unknown code is never invented
        await RemoteOrchestrator(_RecordingClient(error=DaemonError(
            "SandboxError", "WAT", {"code": "WAT"}))).set_session_sandbox(SID, False)


def test_a_create_refusal_is_rebuilt_from_the_envelope():
    from server.ipc.client import reconstruct_create_session_error
    exc = reconstruct_create_session_error(DaemonError(
        "SandboxError", "x", {"code": sb.SANDBOX_START_FAILED, "reason": "r"}))
    assert isinstance(exc, sb.SandboxError) and exc.code == sb.SANDBOX_START_FAILED


@pytest.mark.asyncio
async def test_a_refusal_crosses_the_real_daemon_envelope(tmp_path):
    from server.ipc.client import DaemonClient
    from server.ipc.daemon import create_daemon_app
    from server.ipc.orchestrator_rpc import register_orchestrator_rpc
    from server.tests.ipc_harness import serve_daemon_uds

    class _O(SessionOrchestrator):
        async def set_session_sandbox(self, session_id, enabled, *, identity="system",
                                      via=None):
            raise sb.SandboxError(sb.SANDBOX_SESSION_BUSY)

        async def sandbox_facts(self, session_id, *, refresh=False):
            return {"sessionId": session_id, "refresh": refresh}

    app = create_daemon_app()
    register_orchestrator_rpc(app, _O(sdk_factory=_World().factory,
                                      repo_root=str(tmp_path)))
    async with serve_daemon_uds(app) as sock:
        client = DaemonClient(sock)
        try:
            ro = RemoteOrchestrator(client)
            facts = await ro.sandbox_facts(SID, refresh=True)
            with pytest.raises(sb.SandboxError) as ei:
                await ro.set_session_sandbox(SID, True, identity="u@x", via=None)
        finally:
            await client.aclose()
    assert facts == {"sessionId": SID, "refresh": True}
    assert ei.value.code == sb.SANDBOX_SESSION_BUSY


@pytest.mark.asyncio
async def test_fleet_routes_to_the_owner_and_refuses_the_toggle_during_a_move():
    assert {"sandbox_facts", "set_session_sandbox"} <= _PER_SESSION
    assert "set_session_sandbox" in _MOVE_BLOCKED and "sandbox_facts" not in _MOVE_BLOCKED

    class _HostOrch:
        def __init__(self, name):
            self.name, self.calls = name, []

        async def sandbox_facts(self, sid, **kw):
            self.calls.append("facts")
            return {"host": self.name}

        async def set_session_sandbox(self, sid, enabled, **kw):
            self.calls.append("toggle")
            return {"host": self.name}

    class _Router:
        def __init__(self, orchs):
            self._orchs = orchs

        def get(self, name):
            return self._orchs[name]

        def names(self):
            return sorted(self._orchs)

    dobby, rocky = _HostOrch("dobby"), _HostOrch("rocky")
    fo = FleetOrchestrator(_Router({"dobby": dobby, "rocky": rocky}), primary="dobby")  # type: ignore[arg-type]
    fo._host_cache[SID] = "rocky"
    assert (await fo.sandbox_facts(SID))["host"] == "rocky"
    assert (await fo.set_session_sandbox(SID, True))["host"] == "rocky"
    fo._moving[SID] = "dobby"
    with pytest.raises(SessionMoving):
        await fo.set_session_sandbox(SID, True)
    assert (await fo.sandbox_facts(SID))["host"] == "rocky"
    assert rocky.calls == ["facts", "toggle", "facts"] and dobby.calls == []
    adapter = InProcessOrchestratorAdapter(dobby)  # type: ignore[arg-type]
    assert (await adapter.set_session_sandbox(SID, False))["host"] == "dobby"
    assert (await adapter.sandbox_facts(SID))["host"] == "dobby"


def test_every_code_has_copy_and_a_status():
    for code in sb.SANDBOX_CODES:
        assert sb.MESSAGES[code] and sb.http_status(code) == 409
    with pytest.raises(ValueError):
        sb.SandboxError("NOPE")
