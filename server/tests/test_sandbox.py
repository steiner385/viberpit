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

#: The spike §2.1 block plus the loosening keys pinned in fix round 1 (schema keys the
#: 2.1.319 binary knows, spike §1.1), restated on purpose: the test is the second witness.
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
        "filesystem": {"disabled": False},
        "enableWeakerNestedSandbox": False,
        "allowNestedUserNamespaces": False,
        "ignoreViolations": {},
    }
}

#: A `get_sandbox_dialog` reply meeting every §2.4 condition.
GOOD_DIALOG: dict[str, Any] = {
    "supported": True, "locked": False, "enabled": True, "enabled_in_settings": True,
    "unavailable_reason": None, "mode": "regular", "unsandboxed_fallback": False,
    "dependencies": {"errors": [], "warnings": []}, "excluded_commands": [],
    "restrictions": {"network_allowed_domains": list(sb.ALLOWED_DOMAINS),
                     "allow_all_unix_sockets": False, "fs_allow_write": [],
                     "fs_deny_write": [], "fs_deny_read": [], "fs_allow_read": [],
                     "network_denied_domains": []},
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


def test_codex_sessions_never_spawn_sandboxed_and_a_recorded_one_is_refused():
    from server.codex.capabilities import CODEX_CAPABILITIES
    assert sb.spawn_requested(_rec(engine=AgentEngine.CODEX, sandbox=False),
                              CODEX_CAPABILITIES) is False
    assert sb.session_view(_rec(engine=AgentEngine.CODEX),
                           CODEX_CAPABILITIES)["state"] == sb.STATE_OFF
    # A Codex driver would ignore the flag and run plain: refused instead.
    with pytest.raises(sb.SandboxError) as ei:
        sb.spawn_requested(_rec(engine=AgentEngine.CODEX, sandbox=True), CODEX_CAPABILITIES)
    assert ei.value.code == sb.SANDBOX_UNSUPPORTED


def test_a_legacy_row_with_no_recorded_setting_spawns_unsandboxed():
    assert sb.spawn_requested(_rec(sandbox=None), ClaudeAgentDriver.capabilities) is False
    assert sb.spawn_requested(_rec(sandbox=True), ClaudeAgentDriver.capabilities) is True
    assert sb.spawn_requested(_rec(sandbox=True, kind="assistant"),
                              ClaudeAgentDriver.capabilities) is False


def test_the_kill_switch_hides_the_offer_but_never_turns_a_recorded_sandbox_off(
        monkeypatch):
    monkeypatch.setenv("VIBERPIT_SANDBOX_ENABLED", "false")
    caps = ClaudeAgentDriver.capabilities
    assert sb.sandbox_enabled() is False
    assert capabilities_to_wire(caps)["sandbox"] is False
    assert sb.offers_sandbox(_rec(sandbox=False), caps) is False
    # Fail closed: still requested, so the driver refuses it (held), never plain.
    assert sb.spawn_requested(_rec(sandbox=True), caps) is True
    assert sb.session_view(_rec(sandbox=True, sandbox_state="failed",
                                sandbox_reason="the sandbox is turned off on this host"),
                           caps)["enabled"] is True


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


def _echo(reply: Any, cwd: str | None, config_dir: str | None) -> Any:
    """A CLI that applied the block reports its lists back: fill the restriction lists a
    scripted reply left at their empty defaults with what this spawn's block sends."""
    if not isinstance(reply, dict) or not isinstance(reply.get("restrictions"), dict):
        return reply
    out = json.loads(json.dumps(reply))
    r = out["restrictions"]
    deny_read, allow_read = sb.read_policy(config_dir, cwd)
    for key, want in (("fs_deny_read", deny_read), ("fs_allow_read", allow_read),
                      ("fs_deny_write", sb.protected_git_paths(cwd))):
        if r.get(key) == []:
            r[key] = want
    return out


def _sandboxed_driver(control: FakeControl | None, *, enter_error=None,
                      stderr_lines=(), sandbox=True):
    if control is not None:
        control.replies = [_echo(r, "/tmp/r", "/tmp/cfg") for r in control.replies]
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
        reply = _echo(self.dialogs.pop(0) if len(self.dialogs) > 1 else self.dialogs[0],
                      cwd, cfg)
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
    sent = spawn["flag_settings"]["sandbox"]
    fs = sent.pop("filesystem")
    want = dict(SPIKE_BLOCK["sandbox"])
    want_fs = want.pop("filesystem")
    assert sent == want
    deny_read, allow_read = sb.read_policy(str(orch_env), str(tmp_path / "wt"))
    assert fs == {**want_fs, "denyRead": deny_read, "allowRead": allow_read}
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


def test_the_kill_switch_refuses_and_holds_a_recorded_sandbox_session(orch_env, tmp_path,
                                                                       monkeypatch, caplog):
    """Fix round 1, item 2: with the switch off, a row asking for the sandbox is refused
    and held (SANDBOX_DISABLED) — no CLI starts — and stays held; turning the sandbox
    off is still allowed (the human exit) and respawns it plain."""
    caplog.set_level(logging.INFO)
    monkeypatch.setenv("VIBERPIT_SANDBOX_ENABLED", "false")
    world = _World()

    async def go():
        orch = await _orch(tmp_path, world, sandbox=True)
        with pytest.raises(sb.SandboxError) as first:
            await orch.ensure_driver(SID)
        rec = orch._records[SID]
        held = (rec.status, rec.paused_reason, rec.sandbox_reason)
        with pytest.raises(sb.SandboxError) as second:
            await orch.ensure_driver(SID)
        with pytest.raises(sb.SandboxError) as on:
            await orch.set_session_sandbox(SID, True, identity="u@x", via=None)
        out = await orch.set_session_sandbox(SID, False, identity="u@x", via=None)
        await orch.stop()
        return first.value, held, second.value, on.value, out
    first, held, second, on, out = asyncio.run(go())
    assert first.code == sb.SANDBOX_DISABLED
    assert held == ("needs_attention", sb.SANDBOX_PAUSE_REASON,
                    "the sandbox is turned off on this host")
    assert second.code == sb.SANDBOX_HELD
    assert on.code == sb.SANDBOX_DISABLED
    assert len(world.spawns) == 1 and not world.sandboxed(0), "only the plain respawn"
    assert out["enabled"] is False and out["state"] == "off" and out["held"] is False
    assert _no_warnings(caplog) == []


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


def test_a_new_session_whose_sandbox_cannot_start_fails_and_registers_nothing(orch_env, tmp_path,
                                                                             monkeypatch):
    world = _World(_dialog(unavailable_reason="socat not installed"))
    monkeypatch.setattr(sb, "preflight", _async(_ok_preflight()))

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


# ══ fix round 1 (security review of 25f12457) ════════════════════════════════

CHILD = "5a4db0c5-0000-0000-0000-0000000000c1"


# ── item 4: the dialog check verifies the applied restrictions ───────────────


@pytest.mark.parametrize("over,needle", [
    ({"excluded_commands": ["git push"]}, "excluded"),
    ({"restrictions__network_allowed_domains": ["github.com", "pypi.org"]}, "pypi.org"),
    ({"restrictions__fs_allow_write": [".", "/home/tony/.ssh"]}, "/home/tony/.ssh"),
    ({"excluded_commands": "git"}, "did not report excluded commands"),
    ({"restrictions__network_allowed_domains": "github.com"}, "did not report the network"),
    ({"restrictions__fs_allow_write": ["."]}, "extra writable paths are allowed: ."),
])
def test_a_reply_wider_than_the_block_fails_the_check(over, needle, tmp_path):
    verdict = sb.check_dialog(_dialog(**over), cwd=str(tmp_path / "wt"))
    assert verdict.ok is False and needle in verdict.reason
    assert verdict.expected is False   # a contradiction of what was sent: WARNING


def _linked_worktree(tmp_path):
    clone = tmp_path / "clone"
    (clone / ".git" / "worktrees" / "w1").mkdir(parents=True)
    wt = tmp_path / "wt"
    wt.mkdir(exist_ok=True)
    (wt / ".git").write_text(f"gitdir: {clone}/.git/worktrees/w1\n")
    return clone, wt


def test_any_extra_writable_path_fails_even_inside_the_worktree(tmp_path):
    """Fix round 2, N2: ``fs_allow_write`` lists only EXTRA writes, so it must be []."""
    clone, wt = _linked_worktree(tmp_path)
    for extra in ([str(wt)], [str(clone / ".git")], ["/tmp/claude-1000/x"], ["/tmp/claudeX"]):
        assert sb.check_dialog(_dialog(restrictions__fs_allow_write=extra),
                               cwd=str(tmp_path / "plain")).ok is False, extra


@pytest.mark.parametrize("drop", [("excluded_commands",), ("restrictions", "fs_allow_write"),
                                  ("restrictions", "network_allowed_domains"),
                                  ("restrictions", "fs_deny_write")])
def test_an_absent_restriction_list_fails(drop):
    reply = json.loads(json.dumps(GOOD_DIALOG))
    (reply if len(drop) == 1 else reply[drop[0]]).pop(drop[-1])
    verdict = sb.check_dialog(reply)
    assert verdict.ok is False and "did not report" in verdict.reason


def test_git_hooks_and_config_must_be_write_denied_and_are_sent(tmp_path):
    clone, wt = _linked_worktree(tmp_path)
    want = [str(clone / ".git" / "hooks"), str(clone / ".git" / "config")]
    assert sb.protected_git_paths(str(wt)) == want
    assert sb.settings_block(str(wt))["sandbox"]["filesystem"] == {
        "disabled": False, "denyWrite": want}
    assert sb.check_dialog(GOOD_DIALOG, cwd=str(wt)).ok is False
    ok = _dialog(restrictions__fs_deny_write=want)
    assert sb.check_dialog(ok, cwd=str(wt)).ok is True
    half = _dialog(restrictions__fs_deny_write=want[:1])
    assert "config" in sb.check_dialog(half, cwd=str(wt)).reason


def test_both_verified_clis_emit_the_restriction_lists():
    """N2: the 2.1.293 floor stands — its binary carries every list the check reads
    (checked on rocky's 2.1.293: excluded_commands, fs_allow_write, fs_deny_write,
    network_allowed_domains all present), so MIN_CLI_VERSION was not raised."""
    assert sb.MIN_CLI_VERSION == (2, 1, 293)


def test_the_block_pins_every_loosening_key():
    block = sb.settings_block()["sandbox"]
    assert block["enableWeakerNestedSandbox"] is False
    assert block["allowNestedUserNamespaces"] is False
    assert block["ignoreViolations"] == {}
    assert block["filesystem"] == {"disabled": False}


@pytest.mark.asyncio
async def test_the_driver_checks_the_restrictions_against_its_own_worktree():
    reply = _dialog(restrictions__fs_allow_write=["/etc"])
    d, calls, clients, states = _sandboxed_driver(FakeControl(reply))
    with pytest.raises(sb.SandboxError) as ei:
        await d.start()
    assert ei.value.code == sb.SANDBOX_UNVERIFIED and "/etc" in ei.value.reason
    assert clients[0].exited == 1
    await d.aclose()


# ── item 5: the default read fails closed ───────────────────────────────────


def test_an_unreadable_default_raises_instead_of_reading_off(monkeypatch):
    import contextlib
    import server.db as db_mod

    @contextlib.asynccontextmanager
    async def broken():
        raise RuntimeError("db down")
        yield  # pragma: no cover
    monkeypatch.setattr(db_mod, "db_session", broken)
    with pytest.raises(RuntimeError, match="db down"):
        asyncio.run(sb.read_default())


def test_a_create_whose_default_cannot_be_read_fails_and_records_nothing(orch_env, tmp_path,
                                                                         monkeypatch):
    world = _World()

    async def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(sb, "read_default", boom)

    async def go():
        orch = await _orch(tmp_path, world, sandbox=False)
        with pytest.raises(RuntimeError, match="db down"):
            await orch.create_session(name="n", account="acct1", cwd=str(tmp_path / "wt"))
        ids = set(orch._records)
        await orch.stop()
        return ids
    assert asyncio.run(go()) == {SID}
    assert world.spawns == []


# ── item 1: children and forks inherit the sandbox ──────────────────────────


def test_a_child_of_a_sandboxed_parent_is_sandboxed_even_with_the_default_off(
        orch_env, tmp_path, monkeypatch):
    world = _World()
    monkeypatch.setattr(sb, "preflight", _async(_ok_preflight()))

    async def go():
        orch = await _orch(tmp_path, world, sandbox=True, default=False)
        out = await orch.dispatch_child_session(SID, name="kid", prompt="go", model=None,
                                                permission_mode="full-auto",
                                                identity="session:x")
        child = orch._records[out["id"]]
        row = await _row(out["id"])
        await orch.stop()
        return child, row
    child, row = asyncio.run(go())
    assert child.sandbox is True and row.sandbox is True
    assert world.sandboxed(-1), "the child's spawn carries the block"
    assert row.sandbox_state == "active"


@pytest.mark.parametrize("env,preflight,code", [
    ({}, _bad_preflight(), sb.SANDBOX_UNAVAILABLE),
    ({"VIBERPIT_SANDBOX_ENABLED": "false"}, _ok_preflight(), sb.SANDBOX_DISABLED),
])
def test_a_sandboxed_parent_cannot_dispatch_where_its_child_cannot_be_sandboxed(
        orch_env, tmp_path, monkeypatch, env, preflight, code):
    world = _World()
    monkeypatch.setattr(sb, "preflight", _async(preflight))
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    async def go():
        orch = await _orch(tmp_path, world, sandbox=True)
        with pytest.raises(sb.SandboxError) as ei:
            await orch.dispatch_child_session(SID, name="kid", prompt="go", model=None,
                                              permission_mode="full-auto",
                                              identity="session:x")
        ids = set(orch._records)
        await orch.stop()
        return ei.value, ids
    err, ids = asyncio.run(go())
    assert err.code == code and ids == {SID} and world.spawns == []


def test_any_spawn_with_a_sandboxed_parent_inherits_it(orch_env, tmp_path, monkeypatch):
    """The lineage rule lives in the spawn, so every path that names a parent — a
    dispatched child, a fork (``fork_session`` passes ``parent_session_id``), a queued
    child started later on another daemon (read from the parent's ROW) — inherits."""
    world = _World()
    monkeypatch.setattr(sb, "preflight", _async(_ok_preflight()))

    async def go():
        orch = await _orch(tmp_path, world, sandbox=True, default=False)
        orch._records[SID].sandbox = None   # forces the row read
        rec = await orch.create_session(name="c", account="acct1",
                                        cwd=str(tmp_path / "wt"), parent_session_id=SID)
        await orch.stop()
        return rec
    rec = asyncio.run(go())
    assert rec.sandbox is True and world.sandboxed(-1)


def test_a_child_of_an_unsandboxed_parent_follows_the_default(orch_env, tmp_path):
    world = _World()

    async def go():
        orch = await _orch(tmp_path, world, sandbox=False, default=False)
        out = await orch.dispatch_child_session(SID, name="kid", prompt="go", model=None,
                                                permission_mode=None, identity="session:x")
        child = orch._records[out["id"]]
        await orch.stop()
        return child
    assert asyncio.run(go()).sandbox is False and not world.sandboxed(-1)


@pytest.mark.asyncio
async def test_the_dispatch_tool_reports_a_sandbox_refusal_without_a_warning(caplog):
    from server.tests.test_dispatch_session_tool import _ctx, _tool
    from server.sessions.tools.tools import build_session_tools
    caplog.set_level(logging.INFO)

    async def dispatch_child(**kw):
        raise sb.SandboxError(sb.SANDBOX_UNAVAILABLE, "socat is not installed")
    tool = _tool(build_session_tools(_ctx(dispatch=dispatch_child)), "dispatch_session")
    out = await tool.handler({"name": "kid", "prompt": "go"})
    assert out.get("is_error") is True
    assert "socat is not installed" in json.dumps(out)
    assert not [m for _, m in _no_warnings(caplog) if "dispatch_session" in m]


# ── item 3: a move to a host that cannot sandbox ────────────────────────────


def _subject(sandbox: bool):
    from server.sessions.session_move import MoveSubject
    return MoveSubject(session_id=SID, engine=AgentEngine.CLAUDE_CODE, kind=None,
                       display_mode=None, status="idle", source="dobby", sandbox=sandbox)


def _candidate():
    from server.sessions.fleet import HostCandidate
    return HostCandidate(name="rocky", enabled=True, healthy=True, weight=1,
                         active_sessions=0, max_sessions=None, load_avg=None,
                         mem_available_mb=None)


@pytest.mark.parametrize("readiness,ok", [
    ({"ok": True, "reason": None}, True),
    ({"ok": False, "reason": "socat is not installed"}, False),
    (None, False),
])
def test_a_sandboxed_session_moves_only_to_a_host_that_can_sandbox(readiness, ok):
    from server.sessions import session_move as sm
    caps = ClaudeAgentDriver.capabilities
    v = sm.movable(_subject(True), "rocky", _candidate(), caps=caps, enabled=True,
                   target_sandbox=readiness)
    assert v.ok is ok
    if not ok:
        assert v.code == sm.MOVE_TARGET_SANDBOX and v.extra["sandboxReason"]
        if readiness:
            assert "socat is not installed" in v.message
    # An unsandboxed session never asks.
    assert sm.movable(_subject(False), "rocky", _candidate(), caps=caps, enabled=True).ok


@pytest.mark.asyncio
async def test_the_targets_readiness_is_asked_and_an_unanswered_one_refuses():
    from server.sessions import session_move as sm

    class _Fleet:
        def __init__(self, orch):
            self.orch = orch

        def orch_for_host(self, name):
            return self.orch

    class _Ready:
        async def sandbox_host_readiness(self):
            return {"ok": False, "reason": "the sandbox is turned off on this host"}

    class _Old:
        async def sandbox_host_readiness(self):
            raise DaemonError("UnknownMethod", "sandbox_host_readiness")

    assert await sm._target_sandbox(_Fleet(_Ready()), "rocky") == {
        "ok": False, "reason": "the sandbox is turned off on this host"}
    assert (await sm._target_sandbox(_Fleet(_Old()), "rocky"))["ok"] is False


def test_host_readiness_reports_the_kill_switch_and_the_preflight(monkeypatch):
    monkeypatch.setattr(sb, "preflight", _async(_bad_preflight()))
    assert asyncio.run(sb.host_readiness()) == {"ok": False,
                                                "reason": "socat is not installed"}
    monkeypatch.setenv("VIBERPIT_SANDBOX_ENABLED", "false")
    assert asyncio.run(sb.host_readiness())["ok"] is False


@pytest.mark.asyncio
async def test_remote_asks_the_daemon_for_its_readiness():
    client = _RecordingClient(result={"ok": True, "reason": None})
    assert await RemoteOrchestrator(client).sandbox_host_readiness() == {"ok": True,
                                                                         "reason": None}
    assert client.calls == [("sandbox_host_readiness", {})]


def test_a_target_imports_sandbox_failure_holds_nothing_and_the_source_restores(
        orch_env, tmp_path, caplog):
    """A move's target import whose sandbox fails must not write the hold (or the
    failed state) to the shared row; the source's restore (a local import) resumes."""
    caplog.set_level(logging.INFO)
    world = _World(_dialog(enabled=False), GOOD_DIALOG)

    async def go():
        orch = await _orch(tmp_path, world)
        orch.__dict__.setdefault("_transfer_importing", set()).add(SID)
        with pytest.raises(sb.SandboxError):
            await orch.ensure_driver(SID)
        orch._transfer_importing.discard(SID)
        rec = orch._records[SID]
        mid = (rec.paused_reason, (await _row()).paused_reason,
               (await _row()).sandbox_state)
        out = await orch.transfer_import(SID, None, source="rocky", local=True)
        live = SID in orch._drivers
        await orch.stop()
        return mid, out, live
    mid, out, live = asyncio.run(go())
    assert mid[0] is None and mid[1] is None, "no hold on the shared row"
    assert mid[2] != "failed"
    assert live and out
    assert [world.sandboxed(i) for i in range(2)] == [True, True]


# ── item 6: expected refusals log no WARNING ────────────────────────────────


def test_a_queued_session_refused_by_its_sandbox_logs_info(orch_env, tmp_path, caplog,
                                                          monkeypatch):
    caplog.set_level(logging.INFO)
    world = _World(_dialog(unavailable_reason="socat not installed"))
    monkeypatch.setattr(sb, "preflight", _async(_bad_preflight()))

    async def go():
        orch = await _orch(tmp_path, world, sandbox=False, default=True)
        rec = await orch.create_session(name="q", account="acct1",
                                        cwd=str(tmp_path / "wt"), queue_only=True)
        await orch._maybe_start_queued(force=rec.id)
        await orch.stop()
        return rec
    asyncio.run(go())
    assert world.spawns == [], "refused up front: no CLI started"
    msgs = [(r.levelno, r.getMessage()) for r in caplog.records]
    assert any("session_queue.start_refused_sandbox" in m and lvl == logging.INFO
               for lvl, m in msgs)
    assert not [m for lvl, m in msgs if lvl >= logging.WARNING
                and "session_queue.start_failed" in m]


def test_a_notice_to_a_parent_held_on_its_sandbox_logs_info(orch_env, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    world = _World()

    async def go():
        orch = await _orch(tmp_path, world, sandbox=True,
                           paused_reason=sb.SANDBOX_PAUSE_REASON)
        rearmed: list = []

        async def rearm(child_id, status, prior):
            rearmed.append(child_id)
        orch._rearm_child_notice = rearm  # type: ignore[method-assign]
        await orch._notify_parent(SID, CHILD, "done", "<child-session-notification/>")
        await orch.stop()
        return rearmed
    rearmed = asyncio.run(go())
    assert rearmed == [CHILD] and world.spawns == []
    msgs = [(r.levelno, r.getMessage()) for r in caplog.records]
    assert any("child_notify.parent_sandbox_held" in m and lvl == logging.INFO
               for lvl, m in msgs)
    assert not [m for lvl, m in msgs if lvl >= logging.WARNING and "child_notify" in m]


# ── item 7: the operator's local-host opt-in does not reach the sandbox ─────


def test_a_local_trusted_caller_cannot_flip_the_toggle():
    from server.auth.identity import LOCAL_TRUSTED_VIA
    orch = _ToggleOrch()
    with pytest.raises(ActionError) as ei:
        asyncio.run(session_actions.set_sandbox(orch, session_id=SID, enabled=False,
                                                identity="tony@x", via=LOCAL_TRUSTED_VIA))
    assert ei.value.http == 403 and orch.calls == []


def test_a_local_trusted_caller_cannot_change_the_default(monkeypatch):
    from server.auth import identity
    monkeypatch.setattr(identity, "human_gate_via", lambda _req: identity.LOCAL_TRUSTED_VIA)
    client, _ = _make_client()
    with patch.dict(os.environ, _ENV), client:
        r = client.put("/api/settings", json={"sandboxDefault": True}, headers=_H)
    assert r.status_code == 403


# ══ fix round 2 (re-review of ab357e7c) ══════════════════════════════════════

MODES = ["interactive", "acceptEdits", "full-auto", "bypassPermissions"]


# ── N4 (a)(b): non-Bash tools are confined in sandboxed sessions ────────────


@pytest.mark.parametrize("tool,inp,denied", [
    ("Edit", {"file_path": "src/a.py"}, False),
    ("Write", {"file_path": "{wt}/src/new.py"}, False),
    ("MultiEdit", {"file_path": "README.md"}, False),
    ("NotebookEdit", {"notebook_path": "nb.ipynb"}, False),
    ("Write", {"file_path": "/etc/passwd"}, True),
    ("Edit", {"file_path": "../other/x.py"}, True),
    ("Edit", {"file_path": "~/.bashrc"}, True),
    ("Write", {"file_path": ".git/hooks/pre-commit"}, True),
    ("Edit", {"file_path": ".claude/settings.json"}, True),
    ("Write", {"file_path": "sub/.claude/hooks/x.sh"}, True),
    ("Write", {"file_path": "link-out/x"}, True),           # symlink leaving the worktree
    ("Write", {"file_path": "link-git/hooks/x"}, True),     # symlink into the clone's .git
    ("Edit", {"file_path": "{common}/config"}, True),       # the common git dir itself
    ("Edit", {"file_path": "a.py", "notebook_path": "b"}, True),   # ambiguous
    ("Edit", {}, True),
    ("mcp__github__create_issue", {}, True),
    ("mcp__viberpit__session_context", {}, False),
    ("Read", {"file_path": "/etc/hosts"}, True),            # reads are confined (round 3)
    ("Bash", {"command": "ls"}, False),                     # the sandbox's own job
])
def test_confine_tool(tmp_path, tool, inp, denied):
    clone, wt = _linked_worktree(tmp_path)
    (tmp_path / "outside").mkdir()
    os.symlink(tmp_path / "outside", wt / "link-out")
    os.symlink(clone / ".git", wt / "link-git")
    fmt = {"wt": str(wt), "common": str(clone / ".git")}
    inp = {k: v.format(**fmt) for k, v in inp.items()}
    why = sb.confine_tool(tool, inp, str(wt))
    assert (why is not None) is denied, why


@pytest.mark.asyncio
async def test_the_confinement_hook_denies_with_the_reason(tmp_path):
    hook = sb.confinement_hook(str(tmp_path))
    out = await hook({"tool_name": "Write", "tool_input": {"file_path": "/etc/x"}}, "t1", None)
    spec = out["hookSpecificOutput"]
    assert spec["hookEventName"] == "PreToolUse" and spec["permissionDecision"] == "deny"
    assert "outside" in spec["permissionDecisionReason"]
    assert await hook({"tool_name": "Write", "tool_input": {"file_path": "ok.py"}},
                      "t2", None) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
async def test_the_spawn_registers_the_confinement_hook_in_every_mode(mode, tmp_path):
    """The binding layer: an SDK PreToolUse callback with no matcher (sees every tool),
    registered whatever the permission mode — the CLI runs PreToolUse hooks before the
    permission pipeline and honours their deny before reading the mode (2.1.319
    ``Nds`` @296090015), bypassPermissions included."""
    hook = sb.confinement_hook(str(tmp_path))
    client = await claude_sdk_factory.real_sdk_factory(
        str(tmp_path), str(tmp_path / "cfg"), None, mode, None, sandbox_confine=hook,
        flag_settings=sb.spawn_settings_overlay(str(tmp_path)))
    matchers = client.options.hooks["PreToolUse"]
    ours = [m for m in matchers if hook in m.hooks]
    assert len(ours) == 1 and ours[0].matcher is None
    settings = json.loads(client.options.settings)
    assert settings["disableAllHooks"] is True
    assert settings["enableAllProjectMcpServers"] is False


@pytest.mark.asyncio
async def test_an_unsandboxed_spawn_has_no_confinement_and_keeps_project_mcp_trust(tmp_path):
    client = await claude_sdk_factory.real_sdk_factory(
        str(tmp_path), str(tmp_path / "cfg"), None, "full-auto", None)
    assert len(client.options.hooks["PreToolUse"]) == 1     # the Bash guards only
    settings = json.loads(client.options.settings)
    assert settings["enableAllProjectMcpServers"] is True and "disableAllHooks" not in settings


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
async def test_the_driver_also_denies_in_can_use_tool_in_every_mode(mode, tmp_path):
    from claude_agent_sdk import PermissionResultDeny
    d = ClaudeAgentDriver(SID, "acct1", str(tmp_path), "/tmp/cfg", mode=mode,
                          sdk_factory=lambda *a, **k: _Client(FakeControl(GOOD_DIALOG)),
                          registry=PermissionRegistry(), identity="u", sandbox=True)
    for tool, inp in [("Write", {"file_path": "/etc/x"}),
                      ("Edit", {"file_path": ".git/config"}),
                      ("mcp__other__tool", {})]:
        # Bounded: without the check the call would park on a permission card.
        res = await asyncio.wait_for(d._can_use_tool(tool, inp), timeout=5)
        assert isinstance(res, PermissionResultDeny), (mode, tool)


def test_the_orchestrator_spawn_wires_the_hook_and_the_overlay(orch_env, tmp_path, monkeypatch):
    world = _World()
    seen: dict = {}
    real = world.factory

    def factory(cwd, cfg, can_use_tool, mode, resume, *, on_pre_compact=None,
                options_overlay=None, flag_settings=None, extra_env=None, stderr=None,
                sandbox_confine=None, sandbox_post_write=None):
        seen["confine"] = sandbox_confine
        seen["post"] = sandbox_post_write
        return real(cwd, cfg, can_use_tool, mode, resume, flag_settings=flag_settings,
                    extra_env=extra_env, stderr=stderr)
    world.factory = factory  # type: ignore[method-assign]

    async def go():
        orch = await _orch(tmp_path, world)
        await orch.ensure_driver(SID)
        await orch.stop()
    asyncio.run(go())
    assert callable(seen["confine"]) and callable(seen["post"])
    flags = world.spawns[0]["flag_settings"]
    assert flags["disableAllHooks"] is True and flags["enableAllProjectMcpServers"] is False


# ── N1: a sandboxed parent cannot steer a plain child ───────────────────────


def test_check_reply_refuses_a_plain_child_of_a_sandboxed_parent():
    from server.sessions import child_sessions as cs
    plain = SimpleNamespace(parent_session_id="p1", status="handed_back", sandbox=False)
    boxed = SimpleNamespace(parent_session_id="p1", status="handed_back", sandbox=True)
    with pytest.raises(cs.ChildSessionLimit, match="without the sandbox"):
        cs.check_reply(parent_id="p1", child=plain, child_id="c", message="go",
                       parent_sandboxed=True)
    cs.check_reply(parent_id="p1", child=boxed, child_id="c", message="go",
                   parent_sandboxed=True)
    cs.check_reply(parent_id="p1", child=plain, child_id="c", message="go")


def test_reply_to_child_passes_the_parents_sandbox(orch_env, tmp_path):
    from server.sessions import child_sessions as cs
    world = _World()

    async def go():
        orch = await _orch(tmp_path, world, sandbox=True)
        orch._records[CHILD] = _rec(id=CHILD, sandbox=False, parent_session_id=SID,
                                    status="handed_back")
        with pytest.raises(cs.ChildSessionLimit, match="without the sandbox"):
            await orch.reply_to_child_session(SID, child_session_id=CHILD, message="go",
                                              identity="session:x")
        await orch.stop()
    asyncio.run(go())
    assert world.spawns == []


# ── N3: a queued child is stamped at dispatch ───────────────────────────────


def _queue_everything(orch, monkeypatch):
    async def full(**kw):
        return "global_cap"
    monkeypatch.setattr(orch, "_launch_queue_reason", full)


def test_a_queued_child_is_stamped_sandboxed_in_its_params_and_row(orch_env, tmp_path,
                                                                    monkeypatch):
    from server.sessions import session_queue as sq
    world = _World()
    monkeypatch.setattr(sb, "preflight", _async(_ok_preflight()))

    async def go():
        orch = await _orch(tmp_path, world, sandbox=True)
        _queue_everything(orch, monkeypatch)
        out = await orch.dispatch_child_session(SID, name="kid", prompt="go", model=None,
                                                permission_mode=None, identity="session:x")
        row = await _row(out["id"])
        entries = {out["id"]: (await sq.entry_for(out["id"])).params}
        rec = orch._records[out["id"]]
        await orch.stop()
        return out, row, entries, rec
    out, row, entries, rec = asyncio.run(go())
    assert out["status"] == "queued" and world.spawns == []
    assert row.sandbox is True and rec.sandbox is True
    assert entries[out["id"]]["sandbox"] is True


def test_an_unsandboxed_parents_queued_child_carries_no_key(orch_env, tmp_path, monkeypatch):
    from server.sessions import session_queue as sq
    world = _World()

    async def go():
        orch = await _orch(tmp_path, world, sandbox=False)
        _queue_everything(orch, monkeypatch)
        out = await orch.dispatch_child_session(SID, name="kid", prompt="go", model=None,
                                                permission_mode=None, identity="session:x")
        entries = {out["id"]: (await sq.entry_for(out["id"])).params}
        await orch.stop()
        return out, entries
    out, entries = asyncio.run(go())
    assert "sandbox" not in entries[out["id"]]


# ── mutation survivors from round 1 ─────────────────────────────────────────


@pytest.mark.parametrize("env,preflight,code", [
    ({}, _bad_preflight(), sb.SANDBOX_UNAVAILABLE),
    ({"VIBERPIT_SANDBOX_ENABLED": "false"}, _ok_preflight(), sb.SANDBOX_DISABLED),
])
def test_a_queued_dispatch_is_refused_up_front_and_never_queued(orch_env, tmp_path,
                                                                monkeypatch, env, preflight,
                                                                code):
    """Kills ``dispatch_no_precheck`` and ``require_here_no_switch``: on the queue path
    nothing downstream would refuse — only the dispatch's own check stands."""
    from server.sessions import session_queue as sq
    world = _World()
    monkeypatch.setattr(sb, "preflight", _async(preflight))
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    async def go():
        orch = await _orch(tmp_path, world, sandbox=True)
        _queue_everything(orch, monkeypatch)
        with pytest.raises(sb.SandboxError) as ei:
            await orch.dispatch_child_session(SID, name="kid", prompt="go", model=None,
                                              permission_mode=None, identity="session:x")
        ids = set(orch._records)
        entries = await sq.positions()
        await orch.stop()
        return ei.value, ids, entries
    err, ids, entries = asyncio.run(go())
    assert err.code == code and ids == {SID} and entries == {}


def test_a_queued_child_whose_host_cannot_sandbox_is_refused_at_start(orch_env, tmp_path,
                                                                      monkeypatch):
    """Kills ``inherit_no_require_here``: a child queued while the host could sandbox,
    started after it no longer can, is refused at spawn — no driver, no CLI."""
    world = _World()
    monkeypatch.setattr(sb, "preflight", _async(_ok_preflight()))

    async def go():
        orch = await _orch(tmp_path, world, sandbox=True)
        rec = await orch.create_session(name="c", account="acct1", cwd=str(tmp_path / "wt"),
                                        parent_session_id=SID, queue_only=True)
        monkeypatch.setattr(sb, "preflight", _async(_bad_preflight()))
        await orch._maybe_start_queued(force=rec.id)
        live = rec.id in orch._drivers
        await orch.stop()
        return live
    assert asyncio.run(go()) is False
    assert world.spawns == []


def test_a_fork_of_a_sandboxed_session_is_refused_before_anything_is_made(orch_env, tmp_path,
                                                                          monkeypatch):
    world = _World()
    monkeypatch.setattr(sb, "preflight", _async(_bad_preflight()))

    async def go():
        orch = await _orch(tmp_path, world, sandbox=True)
        made = []

        async def no_worktree(*a, **k):
            made.append(a)
            raise AssertionError("the fork worktree must not be made")
        orch._make_fork_worktree = no_worktree  # type: ignore[method-assign]
        with pytest.raises(sb.SandboxError) as ei:
            await orch.fork_session(SID, at_turn_id=None, mode="message", request_id="r1",
                                    prefill="hi")
        ids = set(orch._records)
        await orch.stop()
        return ei.value, ids, made
    err, ids, made = asyncio.run(go())
    assert err.code == sb.SANDBOX_UNAVAILABLE and ids == {SID} and made == []


def test_a_fork_of_a_sandboxed_session_spawns_sandboxed(orch_env, tmp_path, monkeypatch):
    world = _World()
    monkeypatch.setattr(sb, "preflight", _async(_ok_preflight()))
    seen: dict = {}

    async def go():
        orch = await _orch(tmp_path, world, sandbox=True)

        from viberpit import worktrees as wt_mod
        monkeypatch.setattr(wt_mod, "fork_worktree_dir",
                            lambda cwd, root, child: str(tmp_path / "fork"))

        async def fake_worktree(cwd, sid, dest):
            return SimpleNamespace(cwd=str(tmp_path / "wt"), branch="b")
        orch._make_fork_worktree = fake_worktree  # type: ignore[method-assign]

        async def spawn(child_id, **kw):
            seen.update(kw)
        orch._spawn_session = spawn  # type: ignore[method-assign]
        await orch.fork_session(SID, at_turn_id=None, mode="message", request_id="r1",
                                prefill="hi")
        await orch.stop()
    asyncio.run(go())
    assert seen["sandbox"] is True and seen["parent_session_id"] == SID


def test_a_notice_for_an_answerable_child_of_a_held_parent_escalates(orch_env, tmp_path):
    """Kills ``notify_no_escalate``."""
    world = _World()

    async def go():
        orch = await _orch(tmp_path, world, sandbox=True,
                           paused_reason=sb.SANDBOX_PAUSE_REASON)
        escalated: list = []

        async def rearm(*a):
            return None

        async def escalate(child_id, parent_id, why):
            escalated.append((child_id, parent_id, why))
        orch._rearm_child_notice = rearm  # type: ignore[method-assign]
        orch._escalate_child_to_human = escalate  # type: ignore[method-assign]
        await orch._notify_parent(SID, CHILD, "handed_back", "<n/>")
        await orch._notify_parent(SID, CHILD, "done", "<n/>")
        await orch.stop()
        return escalated
    assert asyncio.run(go()) == [(CHILD, SID, "its parent is held on a failed sandbox")]


def test_a_target_import_maps_a_sandbox_failure_and_holds_nothing(orch_env, tmp_path,
                                                                  monkeypatch, caplog):
    """Kills ``import_mark_dropped`` and ``import_no_moveerror``: a real (non-local)
    ``transfer_import`` whose sandbox fails answers MOVE_TARGET_SANDBOX and writes no
    hold or failed state to the shared row."""
    from viberpit import worktree_transfer as wtx
    from server.sessions import session_move as sm
    caplog.set_level(logging.INFO)
    world = _World(_dialog(enabled=False))
    wt = tmp_path / "wt"

    async def go():
        orch = await _orch(tmp_path, world)
        monkeypatch.setattr(wtx.Bundle, "from_dict",
                            classmethod(lambda cls, d: SimpleNamespace(branch="b")))
        monkeypatch.setattr(wtx, "import_",
                            lambda bundle, clone, root: SimpleNamespace(path=str(wt),
                                                                        is_child=False))

        async def clone_for_move(*a, **k):
            return str(tmp_path / "clone")

        async def nothing(*a, **k):
            return None

        async def claimed(*a, **k):
            return True
        orch._clone_for_move = clone_for_move  # type: ignore[method-assign]
        orch._verify_moved_transcript = nothing  # type: ignore[method-assign]
        orch._claim_session = claimed  # type: ignore[method-assign]
        with pytest.raises(sm.MoveError) as ei:
            await orch.transfer_import(SID, {"bundle": {"x": 1}}, source="rocky")
        row = await _row()
        rec = orch._records.get(SID)
        importing = set(orch.__dict__.get("_transfer_importing", set()))
        await orch.stop()
        return ei.value, row, rec, importing
    err, row, rec, importing = asyncio.run(go())
    assert err.code == sm.MOVE_TARGET_SANDBOX and "did not start" in err.message
    assert row.paused_reason is None and row.sandbox_state != "failed"
    assert getattr(rec, "paused_reason", None) is None
    assert importing == set()
    assert world.sandboxed(0)


def test_the_move_subject_reads_the_rows_sandbox(orch_env, tmp_path):
    """Kills ``move_subject_no_sandbox``."""
    from server.sessions import session_move as sm

    class _Fleet:
        primary_name = "dobby"

        async def get_session(self, sid):
            return None

        def moving_target(self, sid):
            return None

    async def go():
        orch = await _orch(tmp_path, _World(), sandbox=True)
        boxed = await sm._context(_Fleet(), SID)
        async with db_session() as db:
            (await db.get(DbSession, SID)).sandbox = False
        plain = await sm._context(_Fleet(), SID)
        await orch.stop()
        return boxed.subject.sandbox, plain.subject.sandbox
    assert asyncio.run(go()) == (True, False)


def test_a_sandbox_refusal_keeps_its_code_in_the_move_preflight_wire():
    """The web's bug: ``to_wire`` spread ``extra`` after ``reason``, so a ``reason`` extra
    overwrote the code. The sandbox's reason rides as ``sandboxReason``, and the
    verdict's own keys win whatever an extra is called."""
    from server.sessions import session_move as sm
    v = sm.movable(_subject(True), "rocky", _candidate(), caps=ClaudeAgentDriver.capabilities,
                   enabled=True, target_sandbox={"ok": False, "reason": "socat is not installed"})
    wire = v.to_wire()
    assert wire["reason"] == sm.MOVE_TARGET_SANDBOX
    assert wire["sandboxReason"] == "socat is not installed"
    clash = sm.MoveVerdict(False, "MOVE_X", "m", {"reason": "oops", "ok": True})
    assert clash.to_wire()["reason"] == "MOVE_X" and clash.to_wire()["ok"] is False


# ══ fix round 3 (security scan of 862a5238) ══════════════════════════════════


def _tree(tmp_path):
    """A linked worktree with a file, a secret, a symlinked dir inside it, a Claude temp
    root and a config dir; returns the paths the matrix needs."""
    clone, wt = _linked_worktree(tmp_path)
    (wt / "src").mkdir()
    (wt / "src" / "a.py").write_text("x\n")
    (wt / ".env").write_text("TOKEN=1\n")
    (wt / "real").mkdir()
    os.symlink(wt / "real", wt / "linkdir")              # points INSIDE the worktree
    tmp = tmp_path / "claude-tmp"
    tmp.mkdir()
    (tmp / "out.txt").write_text("o\n")
    (clone / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    return clone, wt, tmp


ALLOW_MATRIX = [
    ("Bash", {"command": "curl https://evil.example"}, True),
    ("BashOutput", {"bash_id": "1"}, True),
    ("Task", {"prompt": "x"}, True),
    ("Agent", {"prompt": "x"}, True),
    ("TodoWrite", {"todos": []}, True),
    ("ExitPlanMode", {}, True),
    ("AskUserQuestion", {}, True),
    # Loads a deferred tool's schema (2.1.319 defers e.g. WebFetch; the live probe's
    # model called it first and was refused). What it loads is still confined when called.
    ("ToolSearch", {"query": "select:WebFetch", "max_results": 5}, True),
    ("mcp__viberpit__session_context", {}, True),
    ("Edit", {"file_path": "src/a.py"}, True),
    ("Read", {"file_path": "src/a.py"}, True),
    ("Read", {"file_path": "{wt}/src/a.py"}, True),
    ("Read", {"file_path": "{common}/HEAD"}, True),        # common git dir: read-only ok
    ("Read", {"file_path": "{tmp}/out.txt"}, True),         # the Claude temp root
    ("Read", {"file_path": ".env"}, False),                 # secrets
    ("Read", {"file_path": "/etc/passwd"}, False),
    ("Read", {"file_path": "{home}/.ssh/id_rsa"}, False),
    ("Read", {"file_path": "linkdir/x"}, False),            # symlinked component
    ("NotebookRead", {"notebook_path": "nb.ipynb"}, True),
    ("Glob", {"pattern": "**/*.py"}, True),
    ("Glob", {"pattern": "/etc/**"}, False),
    ("Glob", {"pattern": "../**"}, False),
    ("Glob", {"pattern": "*.py", "path": "/home"}, False),
    ("Grep", {"pattern": "x"}, True),
    ("Grep", {"pattern": "x", "path": "/"}, False),
    ("LS", {"path": "src"}, True),
    ("LS", {"path": "/root"}, False),
    ("WebFetch", {"url": "https://github.com/x/y"}, True),
    ("WebFetch", {"url": "https://api.github.com/repos/x"}, True),
    ("WebFetch", {"url": "https://raw.githubusercontent.com/x"}, True),
    ("WebFetch", {"url": "https://githubusercontent.com/x"}, False),   # *. needs a label
    ("WebFetch", {"url": "https://github.com.evil.example/x"}, False),
    ("WebFetch", {"url": "https://evilgithub.com/x"}, False),
    ("WebFetch", {"url": "http://github.com/x"}, False),
    ("WebFetch", {"url": "https://user:pw@github.com/x"}, False),
    ("WebFetch", {"url": "https://140.82.112.3/x"}, False),
    ("WebFetch", {"url": "https://[::1]/x"}, False),
    ("WebFetch", {"url": "https://github.com:8443/x"}, False),
    ("WebFetch", {"url": "https://GitHub.COM./x"}, True),   # normalised host
    ("WebSearch", {"query": "x"}, False),
    ("Skill", {"skill": "x"}, False),
    ("Monitor", {"command": "x"}, False),
    ("mcp__github__create_issue", {}, False),
    ("SomeFutureTool", {}, False),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
async def test_the_allowlist_matrix_in_every_mode(mode, tmp_path):
    """Deny-by-default, the same verdicts in every permission mode, from both layers:
    the spawn's PreToolUse hook and the driver's can_use_tool."""
    from claude_agent_sdk import PermissionResultDeny
    from server.sessions import claude_sdk_factory as csf
    clone, wt, tmp = _tree(tmp_path)
    fmt = {"wt": str(wt), "common": str(clone / ".git"), "tmp": str(tmp),
           "home": os.path.expanduser("~")}
    hook = sb.confinement_hook(str(wt), tmpdir=str(tmp))
    with patch.object(csf, "session_tmpdir_path", lambda: str(tmp)):
        d = ClaudeAgentDriver(SID, "acct1", str(wt), "/tmp/cfg", mode=mode,
                              sdk_factory=lambda *a, **k: _Client(FakeControl()),
                              registry=PermissionRegistry(), identity="u", sandbox=True)
        for tool, inp, allowed in ALLOW_MATRIX:
            inp = {k: (v.format(**fmt) if isinstance(v, str) else v) for k, v in inp.items()}
            out = await hook({"tool_name": tool, "tool_input": inp}, "t", None)
            assert (out == {}) is allowed, (mode, tool, inp, out)
            if not allowed:
                res = await asyncio.wait_for(d._can_use_tool(tool, inp), timeout=5)
                assert isinstance(res, PermissionResultDeny), (mode, tool, inp)


# ── item 2 (b): symlink components and hard links are refused at check time ──


def test_a_write_through_a_symlinked_component_is_refused_even_inside(tmp_path):
    clone, wt, tmp = _tree(tmp_path)
    why = sb.confine_tool("Write", {"file_path": "linkdir/new.py"}, str(wt))
    assert why and "symlink" in why
    os.symlink(wt / "src" / "a.py", wt / "alias.py")
    assert "symlink" in sb.confine_tool("Edit", {"file_path": "alias.py"}, str(wt))
    assert sb.confine_tool("Write", {"file_path": "real/new.py"}, str(wt)) is None


def test_a_write_to_a_multiply_linked_file_is_refused(tmp_path):
    clone, wt, tmp = _tree(tmp_path)
    os.link(wt / "src" / "a.py", tmp_path / "outside-hardlink")
    why = sb.confine_tool("Edit", {"file_path": "src/a.py"}, str(wt))
    assert why and "hard links" in why


def test_paths_through_a_symlinked_worktree_parent_are_rebased(tmp_path):
    clone, wt, tmp = _tree(tmp_path)
    os.symlink(tmp_path, tmp_path.parent / f"{tmp_path.name}-alias")
    alias_wt = str(tmp_path.parent / f"{tmp_path.name}-alias" / "wt")
    assert sb.confine_tool("Write", {"file_path": f"{alias_wt}/src/b.py"}, alias_wt) is None


# ── item 2 (a) → round 4: the gate serialises writes against Bash ───────────


def _known_gate() -> sb.SandboxGate:
    g = sb.SandboxGate()
    g.known = True
    return g


def test_the_gate_refuses_writes_while_any_bash_runs_and_bash_while_a_write_does():
    g = _known_gate()
    assert g.admit("Bash", {"command": "sleep 5"}, "main-bash") is None
    # A parallel sub-agent's Edit while the main thread's Bash runs: refused, retryable.
    assert g.admit("Edit", {"file_path": "a"}, "sub-edit") == sb.WRITE_WAITS_FOR_COMMAND
    g.done("main-bash")
    assert g.admit("Edit", {"file_path": "a"}, "sub-edit") is None
    # While the admitted write is pending, any thread's Bash is refused.
    assert g.admit("Bash", {"command": "ln -sf /etc x"}, "sub-bash") == \
        sb.COMMAND_WAITS_FOR_WRITE
    assert g.admit("Write", {"file_path": "b"}, "main-write") is None   # writes coexist
    g.done("sub-edit")
    assert g.admit("Bash", {}, "b2") == sb.COMMAND_WAITS_FOR_WRITE
    g.done("main-write")
    assert g.admit("Bash", {}, "b2") is None
    assert g.admit("Read", {}, "r") is None and g.admit("Grep", {}, "g") is None


def test_unknown_or_lost_state_refuses_writes():
    g = sb.SandboxGate()
    assert g.admit("Write", {"file_path": "a"}, "w") == sb.WRITE_STATE_UNKNOWN
    alive = {"v": True}
    g = sb.SandboxGate(alive=lambda: alive["v"])
    g.known = True
    assert g.admit("Write", {"file_path": "a"}, "w") is None
    g.done("w")
    alive["v"] = False                       # the event stream was lost
    assert g.admit("Write", {"file_path": "a"}, "w2") == sb.WRITE_STATE_UNKNOWN


def test_turn_end_clears_stale_foreground_entries_but_not_background_work():
    g = _known_gate()
    g.admit("Bash", {"command": "x"}, "lost")          # its Post event never arrived
    g.admit("Bash", {"command": "srv", "run_in_background": True}, "bg")
    g.done("bg")                                         # the launch returned at once
    assert g.write_blocked() == sb.WRITE_WAITS_FOR_COMMAND
    g.end_turn()
    assert g.bash == set() and g.background == {"bg"}
    assert g.write_blocked() == sb.WRITE_WAITS_FOR_COMMAND   # still running: refused
    g.background_ended("bg")
    assert g.write_blocked() is None


@pytest.mark.asyncio
async def test_the_hooks_drive_the_gate_for_every_thread(tmp_path):
    """Pre registers (main thread and sub-agent ids alike), Post and PostToolUseFailure
    clear; a denied static check never registers."""
    clone, wt, tmp = _tree(tmp_path)
    g = _known_gate()
    pre = sb.confinement_hook(str(wt), tmpdir=str(tmp), gate=g)
    post = sb.post_write_hook(str(wt), lambda t, p: None, gate=g)
    fail = sb.done_hook(g)
    assert await pre({"tool_name": "Bash", "tool_input": {"command": "x"}}, "sub-1", None) == {}
    out = await pre({"tool_name": "Edit", "tool_input": {"file_path": "src/a.py"}}, "e1", None)
    assert out["hookSpecificOutput"]["permissionDecisionReason"] == sb.WRITE_WAITS_FOR_COMMAND
    await fail({"tool_name": "Bash"}, "sub-1", None)
    assert await pre({"tool_name": "Edit", "tool_input": {"file_path": "src/a.py"}},
                     "e1", None) == {}
    out = await pre({"tool_name": "Bash", "tool_input": {"command": "y"}}, "b2", None)
    assert out["hookSpecificOutput"]["permissionDecisionReason"] == sb.COMMAND_WAITS_FOR_WRITE
    await post({"tool_name": "Edit", "tool_input": {"file_path": "src/a.py"}}, "e1", None)
    assert g.writes == set()
    denied = await pre({"tool_name": "Bash", "tool_input": {}}, "b3", None)
    assert denied == {} and g.bash == {"b3"}
    await pre({"tool_name": "WebSearch", "tool_input": {}}, "ws", None)
    assert "ws" not in g.bash


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
async def test_the_driver_feeds_and_reads_the_gate_in_every_mode(mode, tmp_path):
    from claude_agent_sdk import PermissionResultDeny
    from server.sessions import events as ev
    clone, wt, tmp = _tree(tmp_path)
    d = ClaudeAgentDriver(SID, "acct1", str(wt), "/tmp/cfg", mode=mode,
                          sdk_factory=lambda *a, **k: _Client(FakeControl()),
                          registry=PermissionRegistry(), identity="u", sandbox=True)
    gate = d._sandbox_gate
    assert gate is not None and gate.known is False
    # Before the sandbox is verified the state is unknown: writes refused.
    res = await asyncio.wait_for(d._can_use_tool("Write", {"file_path": "src/b.py"}), 5)
    assert isinstance(res, PermissionResultDeny)
    gate.known = True
    d._client = object()                                  # "live" for the alive probe
    d._note_background(ev.BackgroundTaskStarted(
        tool_use_id="tu1", task_id="t1", description="d", task_type="local_bash",
        backgrounded=True))
    d._note_background(ev.BackgroundTaskStarted(
        tool_use_id="tu2", task_id="t2", description="d", task_type="local_bash",
        backgrounded=False))
    assert gate.background == {"tu1"}
    res = await asyncio.wait_for(d._can_use_tool("Write", {"file_path": "src/b.py"}), 5)
    assert isinstance(res, PermissionResultDeny) and res.message == sb.WRITE_WAITS_FOR_COMMAND
    d._note_background(ev.BackgroundTaskEnded(tool_use_id="tu1", task_id="t1",
                                              status="completed"))
    assert gate.background == set()
    d._client = None


def test_a_sandboxed_driver_hands_its_gate_to_the_spawn_and_opens_it_once_verified():
    control = FakeControl(GOOD_DIALOG)
    d, calls, clients, states = _sandboxed_driver(control)
    assert d._sandbox_gate is not None and d._sandbox_gate.known is False
    asyncio.run(_start_and_close(d))
    assert calls[0]["sandbox_gate"] is d._sandbox_gate
    assert d._sandbox_gate.known is True


async def _start_and_close(d):
    await d.start()
    await d.aclose()


@pytest.mark.asyncio
async def test_turn_end_and_interrupt_clear_the_gate(tmp_path):
    d = ClaudeAgentDriver(SID, "acct1", str(tmp_path), "/tmp/cfg", mode="full-auto",
                          sdk_factory=lambda *a, **k: _Client(FakeControl()),
                          registry=PermissionRegistry(), identity="u", sandbox=True)
    g = d._sandbox_gate
    g.known = True
    g.admit("Bash", {}, "stale")
    await d.interrupt()
    assert g.bash == set()
    g.admit("Bash", {}, "stale2")
    await d._end_turn()
    assert g.bash == set()


@pytest.mark.asyncio
async def test_a_tool_result_clears_its_gate_entry(tmp_path):
    """A call the permission flow denied after our hook admitted it gets no PostToolUse,
    but always a result — a sub-agent's too: the driver's pump clears it from the gate."""
    from server.tests.fakes.fake_sdk import user_tool_result
    d = ClaudeAgentDriver(SID, "acct1", str(tmp_path), "/tmp/cfg", mode="full-auto",
                          sdk_factory=lambda *a, **k: _Client(FakeControl()),
                          registry=PermissionRegistry(), identity="u", sandbox=True)
    g = d._sandbox_gate
    g.known = True
    g.admit("Bash", {}, "main")
    g.admit("Bash", {}, "sub")
    await d._handle_msg(user_tool_result("main", "denied", is_error=True))
    await d._handle_msg(user_tool_result("sub", "ok", pid="task-1"))
    assert g.bash == set()


def test_the_orchestrator_shares_one_gate_across_the_spawns_hooks(orch_env, tmp_path):
    world = _World()
    seen: dict = {}
    real = world.factory

    def factory(cwd, cfg, can_use_tool, mode, resume, *, on_pre_compact=None,
                options_overlay=None, flag_settings=None, extra_env=None, stderr=None,
                sandbox_confine=None, sandbox_post_write=None, sandbox_done=None):
        seen.update(confine=sandbox_confine, post=sandbox_post_write, done=sandbox_done)
        return real(cwd, cfg, can_use_tool, mode, resume, flag_settings=flag_settings,
                    extra_env=extra_env, stderr=stderr)
    world.factory = factory  # type: ignore[method-assign]

    async def go():
        orch = await _orch(tmp_path, world)
        driver = await orch.ensure_driver(SID)
        gate = driver._sandbox_gate
        out = await seen["confine"]({"tool_name": "Bash", "tool_input": {}}, "b1", None)
        bash_seen = set(gate.bash)
        await seen["done"]({"tool_name": "Bash"}, "b1", None)
        await orch.stop()
        return out, bash_seen, gate
    out, bash_seen, gate = asyncio.run(go())
    assert out == {} and bash_seen == {"b1"} and gate.bash == set()


# ── item 2 (d): a write that resolved outside holds the session ─────────────


@pytest.mark.asyncio
async def test_the_post_write_hook_reports_an_escaped_write(tmp_path):
    clone, wt, tmp = _tree(tmp_path)
    seen: list = []
    hook = sb.post_write_hook(str(wt), lambda tool, path: seen.append((tool, path)))
    assert await hook({"tool_name": "Write", "tool_input": {"file_path": "src/a.py"}},
                      "t", None) == {}
    assert seen == []
    # The race: after the check, a sandboxed process swapped the target for a link out.
    (tmp_path / "victim").write_text("v\n")
    os.remove(wt / "src" / "a.py")
    os.symlink(tmp_path / "victim", wt / "src" / "a.py")
    await hook({"tool_name": "Write", "tool_input": {"file_path": "src/a.py"}}, "t", None)
    assert seen == [("Write", str(tmp_path / "victim"))]
    await hook({"tool_name": "Read", "tool_input": {"file_path": "src/a.py"}}, "t", None)
    assert len(seen) == 1


def test_an_escape_holds_the_session_closes_its_cli_and_files_one_warning(orch_env, tmp_path,
                                                                          caplog):
    caplog.set_level(logging.INFO)
    world = _World()

    async def go():
        orch = await _orch(tmp_path, world)
        driver = await orch.ensure_driver(SID)
        orch._on_sandbox_escape(SID, "Write", "/home/x/.bashrc")
        for _ in range(100):
            if SID not in orch._drivers and orch._records[SID].paused_reason:
                break
            await asyncio.sleep(0.02)
        rec = orch._records[SID]
        with pytest.raises(sb.SandboxError) as ei:
            await orch.ensure_driver(SID)
        facts = await orch.sandbox_facts(SID)
        await orch.stop()
        return rec, ei.value, driver, facts
    rec, err, driver, facts = asyncio.run(go())
    assert rec.paused_reason == sb.SANDBOX_ESCAPE_REASON and rec.status == "needs_attention"
    assert err.code == sb.SANDBOX_HELD and facts["held"] is True
    assert driver._client is None or driver._closing
    warns = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert warns == ["sandbox.escape_suspected"]
    assert sb.SANDBOX_ESCAPE_REASON in _TERMINAL_PAUSE_REASONS


@pytest.mark.asyncio
async def test_the_spawn_registers_the_post_write_hook(tmp_path):
    post = sb.post_write_hook(str(tmp_path), lambda t, p: None)
    done = sb.done_hook(sb.SandboxGate())
    client = await claude_sdk_factory.real_sdk_factory(
        str(tmp_path), str(tmp_path / "cfg"), None, "bypassPermissions", None,
        sandbox_post_write=post, sandbox_done=done)
    assert [m.hooks for m in client.options.hooks["PostToolUse"]] == [[post]]
    assert [m.hooks for m in client.options.hooks["PostToolUseFailure"]] == [[done]]


# ── item 1: shell read denials, sent and verified ───────────────────────────


def test_the_read_policy_denies_home_and_re_allows_only_what_the_session_needs(
        tmp_path, monkeypatch):
    from viberpit import paths
    from server import hostpaths
    home = os.path.realpath(os.path.expanduser("~"))
    clone, wt, tmp = _tree(tmp_path)
    cfg = tmp_path / "accts" / "cfg" / "acct1"
    cfg.mkdir(parents=True)
    monkeypatch.setenv("VIBERPIT_GH_CONFIG_DIR", _gh_dir(tmp_path, "vp-gh", True))
    deny, allow = sb.read_policy(str(cfg), str(wt))
    assert deny[0] == home
    for d in (hostpaths.ETC_DIR, hostpaths.RUN_DIR, hostpaths.STATE_DIR, hostpaths.LOG_DIR,
              str(paths.root())):
        assert os.path.abspath(d) in deny
    assert allow[:4] == [str(wt.resolve()), str((clone / ".git").resolve()),
                         str(cfg.resolve() / "shell-snapshots"),
                         str((tmp_path / "vp-gh").resolve())]
    assert allow[4:] == [os.path.join(home, r) for r in sb.TOOLCHAIN_READS]
    # The CLI's own writable temp dir (<CLAUDE_CODE_TMPDIR>/claude-<uid>) is restored
    # read-write over the home denial; a re-allow at or above it would be ro-bound ON TOP
    # and make every sandboxed Bash fail (live probe 2026-11-07).
    cli_tmp = os.path.join(str(tmp.resolve()), f"claude-{os.getuid()}")
    assert not any(os.path.commonpath([cli_tmp, a]) == a for a in allow)
    for secret in (".ssh", ".claude-accounts", ".claude", ".config/gh", ".aws"):
        full = os.path.join(home, secret)
        assert not any(os.path.commonpath([full, a]) == a for a in allow), secret
    # rocky: the ambient file-backed ~/.config/gh is the one git uses — re-allowed.
    monkeypatch.delenv("VIBERPIT_GH_CONFIG_DIR")
    monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    ambient = os.path.join(home, ".config", "gh")
    os.makedirs(ambient, exist_ok=True)
    with open(os.path.join(ambient, "hosts.yml"), "w") as fh:
        fh.write("github.com:\n    oauth_token: gho_x\n")
    assert os.path.realpath(ambient) in sb.read_policy(str(cfg), str(wt))[1]


def test_toolchain_reads_are_home_relative_re_allows():
    assert all(not os.path.isabs(p) and ".." not in p for p in sb.TOOLCHAIN_READS)
    assert {".local/bin", ".cargo", ".gitconfig", ".config/git"} <= set(sb.TOOLCHAIN_READS)
    assert ".ssh" not in sb.TOOLCHAIN_READS and ".config/gh" not in sb.TOOLCHAIN_READS


def test_the_block_sends_the_read_rules_and_the_check_requires_them_exactly(tmp_path):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    wt = tmp_path / "wt"
    wt.mkdir()
    deny, allow = sb.read_policy(str(cfg), str(wt))
    fs = sb.settings_block(str(wt), str(cfg))["sandbox"]["filesystem"]
    assert fs["denyRead"] == deny and fs["allowRead"] == allow
    kw = {"cwd": str(wt), "config_dir": str(cfg)}
    good = _dialog(restrictions__fs_deny_read=deny, restrictions__fs_allow_read=allow)
    assert sb.check_dialog(good, **kw).ok is True
    short = _dialog(restrictions__fs_deny_read=deny[1:], restrictions__fs_allow_read=allow)
    assert "not read-denied" in sb.check_dialog(short, **kw).reason
    more = _dialog(restrictions__fs_deny_read=[*deny, "/opt"],
                   restrictions__fs_allow_read=allow)
    assert "read denials differ" in sb.check_dialog(more, **kw).reason
    wide = _dialog(restrictions__fs_deny_read=deny,
                   restrictions__fs_allow_read=[*allow, os.path.expanduser("~/.ssh")])
    assert "re-allows differ" in sb.check_dialog(wide, **kw).reason
    gone = json.loads(json.dumps(good))
    gone["restrictions"].pop("fs_deny_read")
    assert "did not report the read rules" in sb.check_dialog(gone, **kw).reason


@pytest.mark.parametrize("url,why", [
    ("https://140.82.112.3/x", "IP address"),
    ("https://[::1]/x", "IP address"),
    ("https://user:pw@github.com/x", "credentials"),
    ("http://github.com/x", "https-only"),
    ("https://github.com:8443/x", "https port"),
    ("https://evil.example/x", "limited to"),
])
def test_each_webfetch_refusal_names_its_reason(url, why, tmp_path):
    reason = sb.confine_tool("WebFetch", {"url": url}, str(tmp_path))
    assert reason and why in reason


# ══ fix round 5: a per-session temp root ══════════════════════════════════════


def test_the_session_temp_root_is_private_beside_the_shared_one(tmp_path, monkeypatch):
    import stat
    from server.sessions import claude_sdk_factory as csf
    monkeypatch.setenv("VIBERPIT_SESSION_TMPDIR", str(tmp_path / "cache" / "claude-tmp"))
    monkeypatch.delenv("CLAUDE_CODE_TMPDIR", raising=False)
    path = sb.session_tmp_path(SID)
    assert path == str(tmp_path / "cache" / "viberpit-sbx" / SID)
    assert os.path.dirname(os.path.dirname(path)) == os.path.dirname(csf.session_tmpdir_path())
    assert sb.prepare_session_tmp(SID) == path
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o700
    os.chmod(path, 0o755)
    sb.prepare_session_tmp(SID)                    # re-made private
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o700
    sb.remove_session_tmp(SID)
    assert not os.path.exists(path)
    for bad in ("", "../x", "a/b", ".."):
        with pytest.raises(ValueError):
            sb.session_tmp_path(bad)


def test_a_planted_symlink_is_never_used_as_the_temp_root(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBERPIT_SESSION_TMPDIR", str(tmp_path / "cache" / "claude-tmp"))
    monkeypatch.delenv("CLAUDE_CODE_TMPDIR", raising=False)
    path = sb.session_tmp_path(SID)
    os.makedirs(os.path.dirname(path))
    (tmp_path / "elsewhere").mkdir()
    os.symlink(tmp_path / "elsewhere", path)
    with pytest.raises(sb.SandboxError) as ei:
        sb.prepare_session_tmp(SID)
    assert ei.value.code == sb.SANDBOX_START_FAILED
    sb.remove_session_tmp(SID)                     # unlinks the link, not its target
    assert not os.path.lexists(path) and (tmp_path / "elsewhere").is_dir()


def test_the_spawn_uses_its_own_temp_root_and_never_re_allows_one_read_only(orch_env, tmp_path,
                                                                            monkeypatch):
    monkeypatch.setenv("VIBERPIT_SESSION_TMPDIR", str(tmp_path / "cache" / "claude-tmp"))
    monkeypatch.delenv("CLAUDE_CODE_TMPDIR", raising=False)
    world = _World()

    async def go():
        orch = await _orch(tmp_path, world)
        driver = await orch.ensure_driver(SID)
        own = sb.session_tmp_path(SID)
        existed = os.path.isdir(own)
        await orch.close_session(SID)
        gone = not os.path.exists(own)
        await orch.stop()
        return own, existed, gone, driver
    own, existed, gone, driver = asyncio.run(go())
    spawn = world.spawns[0]
    assert spawn["extra_env"]["CLAUDE_CODE_TMPDIR"] == own and existed
    # The flag layer pins the CLI's TMPDIR (os.tmpdir()) to the session's root, so its
    # child-process tmpdir is <root>/claude-<uid> and not the shared fallback (below).
    assert spawn["flag_settings"]["env"] == {"TMPDIR": own}
    allow = spawn["flag_settings"]["sandbox"]["filesystem"]["allowRead"]
    # Neither the session's root nor the shared one is re-allowed: the CLI restores its
    # writable <root>/claude-<uid> itself, and an ro re-allow above it would cover it.
    shared = os.path.realpath(str(tmp_path / "cache" / "claude-tmp"))
    for root in (os.path.realpath(own), shared):
        cli_tmp = os.path.join(root, f"claude-{os.getuid()}")
        assert not any(os.path.commonpath([cli_tmp, a]) == a for a in allow), root
    assert gone, "closing the session removes its temp root"


def test_the_overlay_pins_the_clis_tmpdir_to_the_sessions_root(tmp_path):
    """2.1.319's child-process tmpdir (``j$e()`` @282251389) is
    ``<CLAUDE_CODE_TMPDIR>/claude-<uid>`` only when that path is at most 44 bytes, else
    ``<os.tmpdir()>/claude-<uid>``; the per-session root is always longer, and the shared
    account settings point TMPDIR at a host-wide dir — so the live probe (2026-11-07) saw
    the sandboxed shell's TMPDIR be the SHARED dir, writable and listing other sessions'
    files. ``env.TMPDIR`` in the flag layer (above the user layer) makes the fallback the
    session's own root."""
    own = str(tmp_path / "sbx" / SID)
    assert sb.spawn_settings_overlay(str(tmp_path), None, own)["env"] == {"TMPDIR": own}
    assert "env" not in sb.spawn_settings_overlay(str(tmp_path), None)


def test_the_check_fails_when_a_temp_root_is_re_allowed(tmp_path):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    own = str(tmp_path / "sbx" / SID)
    deny, allow = sb.read_policy(str(cfg), None)
    good = _dialog(restrictions__fs_deny_read=deny, restrictions__fs_allow_read=allow)
    assert sb.check_dialog(good, config_dir=str(cfg)).ok is True
    wrong = _dialog(restrictions__fs_deny_read=deny, restrictions__fs_allow_read=[*allow, own])
    assert "re-allows differ" in sb.check_dialog(wrong, config_dir=str(cfg)).reason


@pytest.mark.asyncio
async def test_the_driver_confines_reads_to_its_own_temp_root(tmp_path, monkeypatch):
    from claude_agent_sdk import PermissionResultDeny
    monkeypatch.setenv("VIBERPIT_SESSION_TMPDIR", str(tmp_path / "cache" / "claude-tmp"))
    monkeypatch.delenv("CLAUDE_CODE_TMPDIR", raising=False)
    clone, wt, _tmp = _tree(tmp_path)
    d = ClaudeAgentDriver(SID, "acct1", str(wt), "/tmp/cfg", mode="full-auto",
                          sdk_factory=lambda *a, **k: _Client(FakeControl()),
                          registry=PermissionRegistry(), identity="u", sandbox=True)
    d._sandbox_tmp = sb.prepare_session_tmp(SID)
    (tmp_path / "cache" / "claude-tmp").mkdir(parents=True, exist_ok=True)
    (tmp_path / "cache" / "claude-tmp" / "other.txt").write_text("x")
    ok = await asyncio.wait_for(d._can_use_tool("Read", {"file_path": f"{d._sandbox_tmp}/o"}),
                                5)
    assert not isinstance(ok, PermissionResultDeny)
    other = await asyncio.wait_for(d._can_use_tool(
        "Read", {"file_path": str(tmp_path / "cache" / "claude-tmp" / "other.txt")}), 5)
    assert isinstance(other, PermissionResultDeny)
    sb.remove_session_tmp(SID)
