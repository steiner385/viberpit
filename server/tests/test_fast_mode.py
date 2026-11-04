"""D-14 server side: the live flag-settings channel (``apply_flag_settings``), fast mode
and live Claude effort.

- The driver writes the CLI's flag layer through the SDK's private control channel (the
  D-9 rewind seam), awaits each reply before the next write, and maps every failure onto
  one ``SETTINGS_CODES`` code logged at INFO.
- Fast mode is available only for the model the CLI's ``initialize`` list marks
  ``supportsFastMode``, only on an engine with ``flag_settings`` (Claude). The choice is
  persisted (``sessions.fast_mode``), re-applied at every spawn through the ``--settings``
  JSON, and cleared when the session moves to a model without it (Review Focus 1, 2).
- The D-1 effort path changes a live Claude session's effort through the same channel,
  mid-turn included, without interrupting the turn (Review Focus 5).

The model rows are the ones the installed CLI 2.1.318 answered in the D-14 spike
(``docs/superpowers/specs/2026-11-04-d14-session-settings-verification.md`` §1.3); the
contract test ``test_settings_cli_contract`` re-checks them against the binary. Only the
CLI is faked — no ``claude`` ever runs.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from claude_agent_sdk._errors import CLIConnectionError
from fastapi.testclient import TestClient
from sqlalchemy import select

from viberpit.selector import Picked
from server.app import create_app
from server.db import db_session, dispose_db, init_db
from server.db.models import Session as DbSession
from server.ipc.client import DaemonClient, DaemonError
from server.ipc.daemon import create_daemon_app
from server.ipc.orchestrator_rpc import register_orchestrator_rpc
from server.sessions import claude_sdk_factory
from server.sessions import flag_settings as fs
from server.sessions import session_actions
from server.sessions.claude_driver import ClaudeAgentDriver
from server.sessions.commands import dispatcher
from server.sessions.commands.catalog import APP, app_command, build_catalog
from server.sessions.engine import EFFORT_UNCHANGED, AgentCapabilities, AgentEngine
from server.sessions.facade import InProcessOrchestratorAdapter
from server.sessions.fleet_orchestrator import _PER_SESSION, FleetOrchestrator
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.permissions import PermissionRegistry
from server.sessions.remote import RemoteOrchestrator
from server.sessions.session_actions import ActionError
from server.sessions.types import SessionRecord
from server.tests.fakes.fake_sdk import FakeSDKClient
from server.tests.ipc_harness import serve_daemon_uds

_H = {"Tailscale-User-Login": "testuser@example.com"}

# `initialize.models` as CLI 2.1.318 answers it (descriptions dropped).
_LEVELS = ["low", "medium", "high", "xhigh", "max"]
MODELS = [
    {"value": "default", "resolvedModel": "claude-opus-5-5", "supportsEffort": True,
     "supportedEffortLevels": _LEVELS, "supportsFastMode": True},
    {"value": "opus", "resolvedModel": "claude-opus-5-5", "supportsEffort": True,
     "supportedEffortLevels": _LEVELS, "supportsFastMode": True},
    {"value": "fable", "resolvedModel": "claude-fable-5-1", "supportsEffort": True,
     "supportedEffortLevels": _LEVELS},
    {"value": "sonnet", "resolvedModel": "claude-sonnet-5-5", "supportsEffort": True,
     "supportedEffortLevels": _LEVELS},
    {"value": "haiku", "resolvedModel": "claude-haiku-5-5", "supportsEffort": True,
     "supportedEffortLevels": _LEVELS},
]
INFO = {"commands": [{"name": "fast", "description": "Toggle fast mode"},
                     {"name": "compact", "description": "Compact"}],
        "models": MODELS, "fast_mode_state": "off",
        "fast_mode_disabled_reason": "sdk_opt_in_required"}
FAST_IDS = ["claude-opus-5-5", "default", "opus"]


def _no_warnings(caplog: pytest.LogCaptureFixture) -> list[tuple[str, str]]:
    return [(r.name, r.getMessage()) for r in caplog.records if r.levelno >= logging.WARNING]


class FakeControl:
    """The SDK ``Query`` control channel: records each request, answers ``{}`` or the
    next scripted reply. An ``Exception`` in the script is raised, as the SDK raises a
    control ``error`` reply. ``gate``, when set, holds every reply until released."""

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.sent: list[dict] = []
        self.timeouts: list[float] = []
        self.gate: asyncio.Event | None = None
        self.in_flight = 0
        self.max_in_flight = 0

    @property
    def patches(self) -> list[dict]:
        return [r["settings"] for r in self.sent if r.get("subtype") == "apply_flag_settings"]

    async def _send_control_request(self, request: dict, timeout: float = 60.0) -> dict:
        self.sent.append(request)
        self.timeouts.append(timeout)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.gate is not None:
                await self.gate.wait()
            reply = self.replies.pop(0) if self.replies else {}
            if isinstance(reply, BaseException):
                raise reply
            return reply
        finally:
            self.in_flight -= 1


def _control_error(text: str) -> Exception:
    """How SDK 0.2.105 raises a control ``error`` reply: a bare Exception."""
    return Exception(text)


def _timeout_error() -> Exception:
    try:
        try:
            raise TimeoutError()
        except TimeoutError as t:
            raise Exception("Control request timeout: apply_flag_settings") from t
    except Exception as exc:  # noqa: BLE001
        return exc


# ---------------------------------------------------------------- model-list reads


def test_fast_mode_models_reads_supports_fast_mode_only() -> None:
    assert fs.fast_mode_models(INFO) == FAST_IDS
    assert fs.fast_mode_models(None) == []
    assert fs.fast_mode_models({"models": "nope"}) == []
    assert fs.fast_mode_models({"models": [{"value": "x", "supportsFastMode": "yes"}]}) == []


@pytest.mark.parametrize("model,ok", [
    (None, True), ("default", True), ("", True), ("opus", True), ("claude-opus-5-5", True),
    ("claude-opus-5-5[1m]", True), ("sonnet", False), ("claude-sonnet-5-5", False),
    ("fable", False), ("claude-opus-4-7", False),
])
def test_supports_fast_mode_is_the_cached_list(model, ok) -> None:
    assert fs.supports_fast_mode(FAST_IDS, model) is ok


def test_an_unknown_list_is_never_available() -> None:
    assert fs.supports_fast_mode(None, "opus") is False
    assert fs.supports_fast_mode([], None) is False


def test_effort_levels_come_from_the_model_row() -> None:
    assert fs.effort_levels(INFO, "sonnet") == _LEVELS
    assert fs.effort_levels(INFO, "claude-opus-4-7") is None
    assert fs.effort_levels(None, "opus") is None


def test_settings_error_carries_its_code_and_rejects_unknown_ones() -> None:
    err = fs.SettingsError(fs.FAST_UNAVAILABLE, "no")
    assert err.code == fs.FAST_UNAVAILABLE and err.error_data == {"code": fs.FAST_UNAVAILABLE}
    with pytest.raises(ValueError):
        fs.SettingsError("NOPE")


def test_flag_settings_is_a_claude_capability_only() -> None:
    from server.codex.driver import CodexAgentDriver
    from server.sessions.engine import capabilities_to_wire
    assert ClaudeAgentDriver.capabilities.flag_settings is True
    assert CodexAgentDriver.capabilities.flag_settings is False
    assert capabilities_to_wire(ClaudeAgentDriver.capabilities)["flagSettings"] is True


# ---------------------------------------------------------------- the driver


def _driver(control: FakeControl | None, *, info: dict | None = INFO,
            on_model_options=None, on_fast_mode_state=None) -> ClaudeAgentDriver:
    def make(cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *,
             on_pre_compact=None):
        client = FakeSDKClient([], on_can_use_tool=can_use_tool, startup_frames=[],
                               server_info=info)
        if control is not None:
            client._query = control  # type: ignore[attr-defined]
        return client
    return ClaudeAgentDriver("s1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                             sdk_factory=make, registry=PermissionRegistry(),
                             identity="u", on_model_options=on_model_options,
                             on_fast_mode_state=on_fast_mode_state)


@pytest.mark.asyncio
async def test_apply_flag_settings_sends_the_raw_control_request() -> None:
    control = FakeControl({"ok": 1})
    d = _driver(control)
    await d.start()
    try:
        assert await d.apply_flag_settings({"fastMode": True}) == {"ok": 1}
    finally:
        await d.aclose()
    assert control.sent == [{"subtype": "apply_flag_settings", "settings": {"fastMode": True}}]
    assert control.timeouts == [fs.SETTINGS_CONTROL_TIMEOUT_S]


@pytest.mark.asyncio
async def test_each_reply_is_awaited_before_the_next_write() -> None:
    control = FakeControl()
    control.gate = asyncio.Event()
    d = _driver(control)
    await d.start()
    try:
        first = asyncio.ensure_future(d.apply_flag_settings({"fastMode": True}))
        second = asyncio.ensure_future(d.apply_flag_settings({"effortLevel": "high"}))
        for _ in range(5):
            await asyncio.sleep(0)
        assert control.patches == [{"fastMode": True}]      # the second is held back
        control.gate.set()
        await asyncio.gather(first, second)
    finally:
        await d.aclose()
    assert control.patches == [{"fastMode": True}, {"effortLevel": "high"}]
    assert control.max_in_flight == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("raised,code", [
    (_control_error("apply_flag_settings requires `settings` to be an object"),
     fs.SETTINGS_REFUSED),
    (_timeout_error(), fs.SETTINGS_FAILED),
    (CLIConnectionError("closed"), fs.SETTINGS_UNAVAILABLE),
    (RuntimeError("reader died"), fs.SETTINGS_FAILED),
])
async def test_failures_map_to_one_code_at_info(raised, code, caplog) -> None:
    caplog.set_level(logging.INFO)
    d = _driver(FakeControl(raised))
    await d.start()
    try:
        with pytest.raises(fs.SettingsError) as ei:
            await d.apply_flag_settings({"fastMode": True})
    finally:
        await d.aclose()
    assert ei.value.code == code
    assert any("apply_flag_settings refused" in r.getMessage() and r.levelno == logging.INFO
               for r in caplog.records)
    assert _no_warnings(caplog) == []


@pytest.mark.asyncio
async def test_no_live_client_or_channel_is_unavailable() -> None:
    d = _driver(FakeControl())
    with pytest.raises(fs.SettingsError) as before:
        await d.apply_flag_settings({"fastMode": True})
    assert before.value.code == fs.SETTINGS_UNAVAILABLE
    no_channel = _driver(None)
    await no_channel.start()
    try:
        with pytest.raises(fs.SettingsError) as ei:
            await no_channel.apply_flag_settings({"fastMode": True})
    finally:
        await no_channel.aclose()
    assert ei.value.code == fs.SETTINGS_UNAVAILABLE


_ROW_KEYS = ("value", "resolvedModel", "supportsFastMode", "supportedEffortLevels")
ROWS = [{k: r[k] for k in _ROW_KEYS if k in r} for r in MODELS]


@pytest.mark.asyncio
async def test_driver_reports_the_model_options_once_per_cli_start() -> None:
    seen: list = []

    async def on_options(sid, rows):
        seen.append((sid, rows))
        return False

    control = FakeControl()
    d = _driver(control, on_model_options=on_options)
    await d.start()
    try:
        assert seen == [("s1", ROWS)]
        await d.reconnect()                       # /clear: a fresh CLI re-reports it
        assert seen == [("s1", ROWS), ("s1", ROWS)]
    finally:
        await d.aclose()
    assert control.patches == []


@pytest.mark.asyncio
async def test_driver_turns_fast_mode_off_when_the_record_drops_it_at_start() -> None:
    async def drop(sid, rows):
        return True

    control = FakeControl(_control_error("busy"))  # best-effort: a refusal is swallowed
    d = _driver(control, on_model_options=drop)
    await d.start()
    await d.aclose()
    assert control.patches == [{"fastMode": False}]


@pytest.mark.asyncio
async def test_set_model_with_effort_writes_effort_live() -> None:
    control = FakeControl()
    d = _driver(control)
    await d.start()
    try:
        await d.set_model("opus", effort="high")
        await d.set_model("opus", effort=None)           # back to the model default
        await d.set_model("opus")                        # unchanged: nothing written
        await d.set_model("opus", effort=EFFORT_UNCHANGED)
    finally:
        await d.aclose()
    assert control.patches == [{"effortLevel": "high"}, {"effortLevel": None}]


@pytest.mark.asyncio
async def test_set_model_refuses_an_effort_the_model_does_not_list(caplog) -> None:
    caplog.set_level(logging.INFO)
    control = FakeControl()
    d = _driver(control)
    await d.start()
    try:
        with pytest.raises(fs.SettingsError) as ei:
            await d.set_model("sonnet", effort="ludicrous")
    finally:
        await d.aclose()
    assert ei.value.code == fs.SETTINGS_INVALID and control.patches == []
    assert _no_warnings(caplog) == []


# ---------------------------------------------------------------- spawn settings


def test_spawn_settings_merge_the_session_flags_under_the_mcp_trust() -> None:
    assert json.loads(claude_sdk_factory.spawn_settings()) == {
        "enableAllProjectMcpServers": True}
    assert json.loads(claude_sdk_factory.spawn_settings(
        {"fastMode": True, "effortLevel": "high", "enableAllProjectMcpServers": False})) == {
        "fastMode": True, "effortLevel": "high", "enableAllProjectMcpServers": True}


@pytest.mark.asyncio
async def test_real_factory_puts_the_flags_in_the_settings_json(monkeypatch) -> None:
    built: list[dict] = []

    class _Opts:
        def __init__(self, **kw):
            built.append(kw)

    class _Client:
        def __init__(self, options):
            self.options = options

    monkeypatch.setattr(claude_sdk_factory, "_import_sdk", lambda: (_Client, _Opts))
    monkeypatch.setattr(claude_sdk_factory, "_cli_path_kwargs", lambda: {})
    await claude_sdk_factory.real_sdk_factory("/w", "/cfg", None, "interactive", None,
                                              flag_settings={"fastMode": True})
    await claude_sdk_factory.real_sdk_factory("/w", "/cfg", None, "interactive", None)
    assert json.loads(built[0]["settings"]) == {"fastMode": True,
                                                "enableAllProjectMcpServers": True}
    assert json.loads(built[1]["settings"]) == {"enableAllProjectMcpServers": True}


# ---------------------------------------------------------------- the orchestrator


def _fake_usage(account: str):
    from datetime import datetime, timedelta, timezone
    from viberpit.quota import anthropic_snapshot
    reset = datetime.now(timezone.utc) + timedelta(hours=5)
    return anthropic_snapshot(10.0, reset, 10.0, reset, False)


def _seed_account(name: str) -> None:
    from viberpit import paths
    from viberpit.creds import write_json_secure
    write_json_secure(paths.slot_path(name), {
        "accessToken": f"{name}-tok", "refreshToken": f"{name}-ref",
        "expiresAt": int(time.time() * 1000) + 9_000_000, "subscriptionType": "max",
    })
    if not paths.shared_mcp_path().exists():
        write_json_secure(paths.shared_mcp_path(), {})


class Spawns:
    """A fake SDK factory that declares ``flag_settings`` (like the real one), records
    each spawn's kwargs and wires one shared ``FakeControl`` into every client."""

    def __init__(self, control: FakeControl) -> None:
        self.control = control
        self.calls: list[dict] = []
        self.clients: list[FakeSDKClient] = []

    def __call__(self, cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *,
                 on_pre_compact=None, options_overlay=None, flag_settings=None, **kw):
        self.calls.append({"resume": resume, "flag_settings": flag_settings, **kw})
        client = FakeSDKClient([], on_can_use_tool=can_use_tool, startup_frames=[],
                               server_info=INFO)
        client._query = self.control  # type: ignore[attr-defined]
        self.clients.append(client)
        return client


async def _no_title(*a, **k):
    return None


@pytest_asyncio.fixture
async def env(tmp_path: Path):
    """A real orchestrator, Claude driver and SQLite DB on the fake CLI; one live
    session on Opus (fast-capable)."""
    store = tmp_path / "store"; store.mkdir()
    shared = tmp_path / "dot-claude"; (shared / "projects").mkdir(parents=True)
    (shared / "settings.json").write_text("{}")
    cfg_dir = tmp_path / "cfg" / "alpha"; cfg_dir.mkdir(parents=True)
    (cfg_dir / ".claude.json").write_text("{}")
    repo_root = tmp_path / "reporoot"; repo_root.mkdir()
    clone = repo_root / "myrepo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(clone)], check=True)
    (clone / "README.md").write_text("hi\n")
    git_env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    for args in (["add", "-A"], ["commit", "-q", "-m", "init"]):
        subprocess.run(["git", "-C", str(clone), *args], check=True, env=git_env)
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'fast.db'}"
    patches: list = [
        patch.dict(os.environ, {"VIBERPIT_HOME": str(store), "CLAUDE_CONFIG_DIR": str(shared),
                                "VIBERPIT_DB_URL": db_url}),
        patch("server.sessions.orchestrator.compose", return_value=cfg_dir),
        patch("server.sessions.orchestrator.configdir.trust_project"),
        patch("server.sessions.orchestrator.select_account", return_value=Picked("alpha")),
        patch("server.sessions.orchestrator.generate_title", _no_title),
    ]
    for p in patches:
        p.start()
    _seed_account("alpha")
    await init_db(db_url=db_url)
    control = FakeControl()
    spawns = Spawns(control)
    orch = SessionOrchestrator(sdk_factory=spawns, poll_interval=9999.0,
                               usage_fn=_fake_usage, repo_root=str(repo_root))
    rec = await orch.create_session("Fast", cwd=str(clone), model="opus",
                                    permission_mode="full-auto")
    try:
        yield SimpleNamespace(orch=orch, spawns=spawns, control=control, rec=rec,
                              repo_root=repo_root)
    finally:
        await orch.stop()
        await dispose_db()
        for p in reversed(patches):
            p.stop()


async def _row(sid: str) -> DbSession:
    async with db_session() as db:
        return (await db.execute(select(DbSession).where(DbSession.id == sid))).scalar_one()


def _payload(rec: SessionRecord) -> dict:
    from server.api.sessions import _record_to_dict
    return _record_to_dict(SessionRecord.from_dict(rec.to_dict()))


def test_live_fast_mode_reads_state_and_reason_from_any_frame() -> None:
    assert fs.live_fast_mode(INFO) == ("off", "sdk_opt_in_required")
    assert fs.live_fast_mode({"fast_mode_state": "cooldown"}) == ("cooldown", None)
    assert fs.live_fast_mode({"fast_mode_state": "on", "fast_mode_disabled_reason": ""}) == ("on", None)
    # Unknown state / absent / non-mapping: nothing to record (the last reading stands).
    assert fs.live_fast_mode({"fast_mode_state": "weird"}) == (None, None)
    assert fs.live_fast_mode({}) == (None, None)
    assert fs.live_fast_mode(None) == (None, None)


@pytest.mark.asyncio
async def test_driver_reports_the_live_fast_mode_state_at_start_and_on_each_init() -> None:
    from claude_agent_sdk import SystemMessage
    seen: list = []

    async def on_state(sid, state, reason):
        seen.append((sid, state, reason))

    d = _driver(FakeControl(), on_fast_mode_state=on_state)
    await d.start()
    try:
        assert seen == [("s1", "off", "sdk_opt_in_required")]
        await d._handle_msg(SystemMessage(subtype="init", data={
            "session_id": "s1", "fast_mode_state": "cooldown"}))
        await d._handle_msg(SystemMessage(subtype="init", data={"session_id": "s1"}))
        assert seen[1:] == [("s1", "cooldown", None)]     # no state on a frame → no report
    finally:
        await d.aclose()


@pytest.mark.asyncio
async def test_the_record_keeps_the_latest_live_state_and_serves_it(env) -> None:
    # The CLI's start-up reading (INFO) is already on the record and the payload.
    p = _payload(env.rec)
    assert (p["fastModeState"], p["fastModeDisabledReason"]) == ("off", "sdk_opt_in_required")
    await env.orch._record_fast_mode_state(env.rec.id, "cooldown", None)
    p = _payload(env.rec)
    assert (p["fastModeState"], p["fastModeDisabledReason"]) == ("cooldown", None)
    assert p["fastMode"] is False                # the stored choice is untouched


@pytest.mark.asyncio
async def test_a_started_session_caches_the_fast_models_and_reports_availability(env) -> None:
    assert env.rec.fast_mode_models == FAST_IDS
    assert env.orch.fast_mode_available(env.rec.id) is True
    p = _payload(env.rec)            # through the daemon→edge RPC copy, as served
    assert p["fastModeAvailable"] is True and p["fastMode"] is False


@pytest.mark.asyncio
async def test_set_fast_mode_writes_live_persists_and_returns_enabled(env, caplog) -> None:
    caplog.set_level(logging.INFO)
    assert await env.orch.set_fast_mode(env.rec.id, True) == {"enabled": True}
    assert env.control.patches == [{"fastMode": True}]
    assert env.rec.fast_mode is True and (await _row(env.rec.id)).fast_mode is True
    assert _payload(env.rec)["fastMode"] is True
    assert await env.orch.set_fast_mode(env.rec.id, False) == {"enabled": False}
    assert env.control.patches[-1] == {"fastMode": False}
    assert (await _row(env.rec.id)).fast_mode is False
    assert _no_warnings(caplog) == []


@pytest.mark.asyncio
async def test_an_unsupported_model_is_fast_unavailable_and_sends_nothing(env, caplog) -> None:
    caplog.set_level(logging.INFO)
    await env.orch.set_model_for_session(env.rec.id, "sonnet")
    assert env.orch.fast_mode_available(env.rec.id) is False
    assert _payload(env.rec)["fastModeAvailable"] is False
    with pytest.raises(fs.SettingsError) as ei:
        await env.orch.set_fast_mode(env.rec.id, True)
    assert ei.value.code == fs.FAST_UNAVAILABLE
    # Only the switch's re-sync of the (off) record went out; the refused enable sent nothing.
    assert env.control.patches == [{"fastMode": False}]
    assert not (await _row(env.rec.id)).fast_mode
    assert await env.orch.set_fast_mode(env.rec.id, False) == {"enabled": False}  # always ok
    assert _no_warnings(caplog) == []


@pytest.mark.asyncio
async def test_an_engine_without_flag_settings_is_fast_unavailable(env) -> None:
    with patch.object(SessionOrchestrator, "_capabilities_of",
                      lambda self, sid: AgentCapabilities(models=True)):
        assert env.orch.fast_mode_available(env.rec.id) is False
        with pytest.raises(fs.SettingsError) as ei:
            await env.orch.set_fast_mode(env.rec.id, True)
    assert ei.value.code == fs.FAST_UNAVAILABLE


@pytest.mark.asyncio
async def test_the_kill_switch_turns_flag_settings_off(env, monkeypatch) -> None:
    from server.sessions.engine import capabilities_to_wire, flag_settings_supported
    monkeypatch.setenv("VIBERPIT_FLAG_SETTINGS_ENABLED", "false")
    assert flag_settings_supported(ClaudeAgentDriver.capabilities) is False
    assert capabilities_to_wire(ClaudeAgentDriver.capabilities)["flagSettings"] is False
    assert env.orch.fast_mode_available(env.rec.id) is False
    assert _payload(env.rec)["fastModeAvailable"] is False    # the payload agrees
    with pytest.raises(fs.SettingsError) as ei:
        await env.orch.set_fast_mode(env.rec.id, True)
    assert ei.value.code == fs.FAST_UNAVAILABLE
    env.rec.fast_mode = True
    env.rec.reasoning_effort = "high"
    assert env.orch._spawn_flag_settings(env.rec.id) == {}
    names = {c["name"] for c in await env.orch.command_catalog(env.rec.id)}
    assert "fast" not in names and "effort" not in names
    async with db_session() as db:       # live effort falls back to the old refusal
        with pytest.raises(ActionError) as refused:
            await session_actions.set_model(env.orch, db, session_id=env.rec.id,
                                            model="opus", identity="u", effort="max")
    assert (refused.value.code, refused.value.http) == ("unsupported_reasoning_effort", 400)
    await env.orch.set_model_for_session(env.rec.id, "sonnet")
    assert env.control.patches == []                # no control request at all
    with pytest.raises(fs.SettingsError):
        await env.orch._drivers[env.rec.id].apply_flag_settings({"fastMode": False})
    assert env.control.patches == []


def test_codex_record_is_never_fast_available() -> None:
    rec = SessionRecord(id="c", name="n", account="a", engine=AgentEngine.CODEX,
                        engine_session_id="t", cols=80, rows=24, cwd="/w",
                        model="gpt-5", fast_mode_models=None)
    from server.api.sessions import _record_to_dict
    assert _record_to_dict(rec)["fastModeAvailable"] is False


@pytest.mark.asyncio
async def test_a_copilot_session_caches_no_fast_models(env) -> None:
    with patch("server.sessions.orchestrator._provider_of",
               return_value=__import__("viberpit.copilot", fromlist=["x"]).PROVIDER):
        await env.orch._record_model_options(env.rec.id, ROWS)
    assert env.rec.fast_mode_models == [] and env.orch.fast_mode_available(env.rec.id) is False


@pytest.mark.asyncio
async def test_a_cli_refusal_keeps_the_record_off(env) -> None:
    env.control.replies = [_control_error("nope")]
    with pytest.raises(fs.SettingsError) as ei:
        await env.orch.set_fast_mode(env.rec.id, True)
    assert ei.value.code == fs.SETTINGS_REFUSED
    assert env.control.patches == [{"fastMode": True}]      # definite refusal: no compensation
    assert env.rec.fast_mode is False and not (await _row(env.rec.id)).fast_mode


@pytest.mark.asyncio
async def test_a_driverless_session_persists_for_the_next_spawn(env) -> None:
    env.orch._drivers.pop(env.rec.id)
    assert await env.orch.set_fast_mode(env.rec.id, True) == {"enabled": True}
    assert env.control.patches == [] and (await _row(env.rec.id)).fast_mode is True


@pytest.mark.asyncio
async def test_unknown_session_is_a_keyerror(env) -> None:
    with pytest.raises(KeyError):
        await env.orch.set_fast_mode("nope", True)


# Review Focus 1
@pytest.mark.asyncio
async def test_switching_to_a_model_without_fast_mode_clears_it(env, caplog) -> None:
    caplog.set_level(logging.INFO)
    await env.orch.set_fast_mode(env.rec.id, True)
    await env.orch.set_model_for_session(env.rec.id, "sonnet")
    assert env.control.patches == [{"fastMode": True}, {"fastMode": False}]
    assert env.rec.fast_mode is False and (await _row(env.rec.id)).fast_mode is False
    p = _payload(env.rec)
    assert p["fastMode"] is False and p["fastModeAvailable"] is False
    assert _no_warnings(caplog) == []


@pytest.mark.asyncio
async def test_every_model_switch_resyncs_the_cli_to_the_record(env) -> None:
    """The record is authoritative: each live switch re-asserts its fast mode, so a
    CLI that drifted (a lost write, a refused clear) is put back on the next switch."""
    await env.orch.set_model_for_session(env.rec.id, "sonnet")
    await env.orch.set_model_for_session(env.rec.id, "opus")
    assert env.control.patches == [{"fastMode": False}, {"fastMode": False}]
    assert env.rec.fast_mode is False


@pytest.mark.asyncio
async def test_a_refused_clear_is_reasserted_on_the_next_switch(env, caplog) -> None:
    caplog.set_level(logging.INFO)
    await env.orch.set_fast_mode(env.rec.id, True)
    env.control.replies = [_control_error("busy")]           # the clear is refused
    await env.orch.set_model_for_session(env.rec.id, "haiku")
    assert env.rec.model == "haiku" and env.rec.fast_mode is False
    assert (await _row(env.rec.id)).fast_mode is False
    await env.orch.set_model_for_session(env.rec.id, "opus")  # back on a fast model
    # The refused clear, then the next switch re-asserts the record: the CLI ends off.
    assert env.control.patches == [{"fastMode": True}, {"fastMode": False},
                                   {"fastMode": False}]
    assert env.rec.fast_mode is False
    await env.orch._drivers[env.rec.id].reconnect()           # and a respawn starts off
    assert "fastMode" not in (env.spawns.calls[-1]["flag_settings"] or {})
    assert _no_warnings(caplog) == []


@pytest.mark.asyncio
async def test_a_switch_with_an_unknown_model_list_clears_fast_mode(env) -> None:
    """Fail closed: with no model list there is no proof the new model has it."""
    await env.orch.set_fast_mode(env.rec.id, True)
    env.rec.fast_mode_models = None
    await env.orch.set_model_for_session(env.rec.id, "claude-opus-5-5")
    assert env.rec.fast_mode is False and (await _row(env.rec.id)).fast_mode is False
    assert env.control.patches[-1] == {"fastMode": False}


@pytest.mark.asyncio
async def test_staying_on_a_fast_model_keeps_fast_mode(env) -> None:
    await env.orch.set_fast_mode(env.rec.id, True)
    await env.orch.set_model_for_session(env.rec.id, "claude-opus-5-5")
    assert env.rec.fast_mode is True
    assert env.control.patches == [{"fastMode": True}, {"fastMode": True}]


@pytest.mark.asyncio
async def test_an_enable_with_no_reply_is_compensated_and_kept_off(env, caplog) -> None:
    caplog.set_level(logging.INFO)
    env.control.replies = [_timeout_error()]                  # may or may not have landed
    with pytest.raises(fs.SettingsError) as ei:
        await env.orch.set_fast_mode(env.rec.id, True)
    assert ei.value.code == fs.SETTINGS_FAILED
    assert env.control.patches == [{"fastMode": True}, {"fastMode": False}]
    assert env.rec.fast_mode is False and (await _row(env.rec.id)).fast_mode is False
    assert _no_warnings(caplog) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [_control_error("busy"), _timeout_error()])
async def test_a_failed_disable_keeps_the_record_on(env, failure) -> None:
    """The CLI may still be on (billing), so the record must not say off: ⚡ stays lit
    and the user can retry."""
    await env.orch.set_fast_mode(env.rec.id, True)
    env.control.replies = [failure]
    with pytest.raises(fs.SettingsError):
        await env.orch.set_fast_mode(env.rec.id, False)
    assert env.rec.fast_mode is True and (await _row(env.rec.id)).fast_mode is True
    assert _payload(env.rec)["fastMode"] is True


@pytest.mark.asyncio
async def test_a_disable_with_no_live_client_records_off(env) -> None:
    await env.orch.set_fast_mode(env.rec.id, True)
    env.orch._drivers[env.rec.id]._stream_ended = True
    assert await env.orch.set_fast_mode(env.rec.id, False) == {"enabled": False}
    assert env.rec.fast_mode is False and (await _row(env.rec.id)).fast_mode is False


@pytest.mark.asyncio
async def test_a_failed_compensation_leaves_the_record_on(env) -> None:
    """Enable lost its reply AND the compensating off failed: the CLI may be on, so the
    record says on (unknown is billed until proven otherwise)."""
    env.control.replies = [_timeout_error(), _control_error("busy")]
    with pytest.raises(fs.SettingsError) as ei:
        await env.orch.set_fast_mode(env.rec.id, True)
    assert ei.value.code == fs.SETTINGS_FAILED
    assert env.control.patches == [{"fastMode": True}, {"fastMode": False}]
    assert env.rec.fast_mode is True and (await _row(env.rec.id)).fast_mode is True


@pytest.mark.asyncio
async def test_a_refused_enable_on_an_already_on_session_keeps_it_on(env) -> None:
    await env.orch.set_fast_mode(env.rec.id, True)
    env.control.replies = [_control_error("busy")]
    with pytest.raises(fs.SettingsError):
        await env.orch.set_fast_mode(env.rec.id, True)
    assert env.rec.fast_mode is True and (await _row(env.rec.id)).fast_mode is True


@pytest.mark.asyncio
async def test_a_failed_clear_with_an_unknown_list_keeps_the_record_on(env) -> None:
    """Unknown list + a refused clear: the model may have fast mode, so the CLI may be
    billing — the record stays on. (A KNOWN model without it is inert: cleared.)"""
    await env.orch.set_fast_mode(env.rec.id, True)
    env.rec.fast_mode_models = None
    env.control.replies = [_control_error("busy")]
    await env.orch.set_model_for_session(env.rec.id, "claude-opus-5-5")
    assert env.rec.fast_mode is True and (await _row(env.rec.id)).fast_mode is True


@pytest.mark.asyncio
async def test_a_spawn_on_a_model_without_fast_mode_clears_the_stored_flag(env) -> None:
    """After a restart the list is unknown, so the spawn passes the stored flag; once
    the CLI reports its list and the model has no fast mode, the record drops it."""
    await env.orch.set_model_for_session(env.rec.id, "sonnet")
    env.rec.fast_mode = True                       # as a pre-fix row could hold it
    env.rec.fast_mode_models = None
    before = len(env.control.patches)
    await env.orch._drivers[env.rec.id].reconnect()
    assert env.spawns.calls[-1]["flag_settings"] == {"fastMode": True}
    assert env.rec.fast_mode is False and (await _row(env.rec.id)).fast_mode is False
    assert env.control.patches[before:] == [{"fastMode": False}]   # and the CLI is told


# Review Focus 2
@pytest.mark.asyncio
async def test_a_daemon_restart_respawns_with_fast_mode_from_the_row(env) -> None:
    await env.orch.set_fast_mode(env.rec.id, True)
    sid = env.rec.id
    assert env.spawns.calls[0]["flag_settings"] is None      # created with nothing
    # A restart: a new orchestrator on the same DB knows the session only from its row.
    spawns = Spawns(FakeControl())
    orch2 = SessionOrchestrator(sdk_factory=spawns, poll_interval=9999.0,
                                usage_fn=_fake_usage, repo_root=str(env.repo_root))
    try:
        await orch2.rehydrate()
        assert orch2._records[sid].fast_mode is True
        assert await orch2.ensure_driver(sid) is not None
        assert spawns.calls[-1]["flag_settings"] == {"fastMode": True}
        assert orch2._records[sid].fast_mode_models == FAST_IDS
    finally:
        await orch2.stop()


@pytest.mark.asyncio
async def test_a_copilot_spawn_never_carries_fast_mode(env) -> None:
    from viberpit import copilot
    env.rec.fast_mode = True
    for models in (None, FAST_IDS):
        env.rec.fast_mode_models = models
        with patch("server.sessions.orchestrator._provider_of",
                   return_value=copilot.PROVIDER):
            assert "fastMode" not in env.orch._spawn_flag_settings(env.rec.id)


@pytest.mark.asyncio
async def test_a_spawn_on_a_known_unsupported_model_carries_no_fast_mode(env) -> None:
    env.rec.fast_mode = True
    env.rec.model = "sonnet"
    assert "fastMode" not in env.orch._spawn_flag_settings(env.rec.id)
    env.rec.model = "opus"
    assert env.orch._spawn_flag_settings(env.rec.id) == {"fastMode": True}


@pytest.mark.asyncio
async def test_clear_respawns_with_the_flags_too(env) -> None:
    await env.orch.set_fast_mode(env.rec.id, True)
    await env.orch._drivers[env.rec.id].reconnect()
    assert env.spawns.calls[-1]["flag_settings"] == {"fastMode": True}


# Live effort (D-1 endpoint) and Review Focus 5
@pytest.mark.asyncio
async def test_effort_on_an_idle_claude_session_applies_live_and_persists(env) -> None:
    restarted = await env.orch.set_model_for_session(env.rec.id, "opus",
                                                     reasoning_effort="high")
    assert restarted is False
    assert env.control.patches == [{"effortLevel": "high"}]
    assert env.rec.reasoning_effort == "high"
    assert (await _row(env.rec.id)).reasoning_effort == "high"


@pytest.mark.asyncio
async def test_effort_mid_turn_applies_now_without_interrupting(env) -> None:
    driver = env.orch._drivers[env.rec.id]
    interrupts: list = []

    async def _interrupt(**kw):
        interrupts.append(kw)

    driver.interrupt = _interrupt  # type: ignore[method-assign]
    driver._turn_active = True     # a turn is running
    env.orch._last_prompt[env.rec.id] = "keep going"
    for when in ("now", "after_turn"):
        restarted = await env.orch.set_model_for_session(
            env.rec.id, "opus", when=when, reasoning_effort="max")
        assert restarted is False
    assert interrupts == []
    assert env.control.patches == [{"effortLevel": "max"}, {"effortLevel": "max"}]
    assert env.rec.model_pending is False and driver.turn_active is True


@pytest.mark.asyncio
async def test_effort_on_a_driverless_session_applies_at_the_next_spawn(env) -> None:
    driver = env.orch._drivers.pop(env.rec.id)
    await env.orch.set_model_for_session(env.rec.id, "opus", reasoning_effort="low")
    assert env.control.patches == []
    env.orch._drivers[env.rec.id] = driver
    await driver.reconnect()
    assert env.spawns.calls[-1]["flag_settings"] == {"effortLevel": "low"}


@pytest.mark.asyncio
async def test_a_switch_with_an_invalid_effort_changes_nothing(env) -> None:
    """opus -> sonnet with an effort sonnet does not list: refused before ANY write —
    no interrupt, no model switch on the CLI, nothing persisted."""
    driver = env.orch._drivers[env.rec.id]
    client = env.spawns.clients[-1]
    interrupts: list = []

    async def _interrupt(**kw):
        interrupts.append(kw)

    driver.interrupt = _interrupt  # type: ignore[method-assign]
    driver._turn_active = True
    env.orch._last_prompt[env.rec.id] = "keep going"
    with pytest.raises(fs.SettingsError) as ei:
        await env.orch.set_model_for_session(env.rec.id, "sonnet", when="now",
                                             reasoning_effort="turbo")
    assert ei.value.code == fs.SETTINGS_INVALID
    assert interrupts == [] and client.model == "opus"
    assert env.rec.model == "opus" and (await _row(env.rec.id)).model == "opus"
    assert env.control.patches == []


@pytest.mark.asyncio
async def test_a_refused_effort_after_the_switch_keeps_the_model_and_the_turn(env) -> None:
    """The model switched before the effort write was refused: the record follows the
    CLI's model, the effort stays as it was, the interrupted turn is re-sent, and the
    refusal still reaches the caller."""
    driver = env.orch._drivers[env.rec.id]
    client = env.spawns.clients[-1]
    sent: list = []

    async def _interrupt(**kw):
        return None

    async def _send(text, *a, **k):
        sent.append(text)

    driver.interrupt = _interrupt  # type: ignore[method-assign]
    driver.send_prompt = _send  # type: ignore[method-assign]
    driver._turn_active = True
    env.orch._last_prompt[env.rec.id] = "keep going"
    env.control.replies = [_control_error("nope")]
    with pytest.raises(fs.SettingsError) as ei:
        await env.orch.set_model_for_session(env.rec.id, "sonnet", when="now",
                                             reasoning_effort="high")
    assert ei.value.code == fs.SETTINGS_REFUSED
    assert client.model == "sonnet" and env.rec.model == "sonnet"
    assert (await _row(env.rec.id)).model == "sonnet"
    assert env.rec.reasoning_effort is None
    assert (await _row(env.rec.id)).reasoning_effort is None
    assert sent == ["keep going"]


@pytest.mark.asyncio
async def test_effort_on_a_dead_cli_persists_for_the_next_spawn(env) -> None:
    driver = env.orch._drivers[env.rec.id]
    driver._stream_ended = True
    async with db_session() as db:
        out = await session_actions.set_model(env.orch, db, session_id=env.rec.id,
                                              model="opus", identity="u", effort="high")
    assert out["ok"] is True
    assert env.rec.reasoning_effort == "high"
    assert (await _row(env.rec.id)).reasoning_effort == "high"
    assert env.orch._spawn_flag_settings(env.rec.id) == {"effortLevel": "high"}


@pytest.mark.asyncio
async def test_an_invalid_effort_changes_nothing(env) -> None:
    with pytest.raises(fs.SettingsError) as ei:
        await env.orch.set_model_for_session(env.rec.id, "opus", reasoning_effort="turbo")
    assert ei.value.code == fs.SETTINGS_INVALID
    assert env.rec.reasoning_effort is None and env.control.patches == []


@pytest.mark.asyncio
async def test_session_actions_routes_claude_effort_and_maps_refusals(env) -> None:
    async with db_session() as db:
        out = await session_actions.set_model(env.orch, db, session_id=env.rec.id,
                                              model="opus", identity="u", effort="xhigh")
        assert out == {"ok": True, "restarted": False}
        with pytest.raises(ActionError) as bad:
            await session_actions.set_model(env.orch, db, session_id=env.rec.id,
                                            model="opus", identity="u", effort="turbo")
    assert env.control.patches == [{"effortLevel": "xhigh"}]
    assert (bad.value.code, bad.value.http) == (fs.SETTINGS_INVALID, 400)


# ---------------------------------------------------------------- /fast command


CLAUDE = ClaudeAgentDriver.capabilities


def test_fast_is_an_app_command_gated_on_availability() -> None:
    off = {c.name: c for c in build_catalog(INFO, CLAUDE, kind=None)}
    on = {c.name: c for c in build_catalog(INFO, CLAUDE, kind=None, fast_mode_available=True)}
    assert "fast" not in off                         # the CLI's own entry is dropped too
    assert on["fast"].source == APP and on["fast"].kind == "client"
    assert app_command("fast", CLAUDE) is None
    assert app_command("fast", CLAUDE, fast_mode_available=True) is not None
    codex = AgentCapabilities(models=True)
    assert "fast" not in {c.name for c in build_catalog(INFO, codex, kind=None,
                                                        fast_mode_available=True)}
    assert "compact" in off                          # other vendor entries untouched


@pytest.mark.asyncio
async def test_fast_reaching_the_dispatcher_is_a_notice() -> None:
    cmd = app_command("fast", CLAUDE, fast_mode_available=True)
    assert cmd is not None
    out = await dispatcher.dispatch(object(), cmd, "on")
    assert out is not None and out["render"] == "notice" and "/fast" in out["data"]["text"]


def test_effort_is_an_app_control_command_gated_on_flag_settings() -> None:
    by = {c.name: c for c in build_catalog(INFO | {"commands": [{"name": "effort"}]},
                                           CLAUDE, kind=None)}
    assert by["effort"].source == APP and by["effort"].kind == "control"
    assert app_command("effort", CLAUDE) is not None
    codex = AgentCapabilities(models=True, reasoning_effort=True)
    assert "effort" not in {c.name for c in build_catalog(
        {"commands": [{"name": "effort"}]}, codex, kind=None)}   # never the CLI's own
    assert app_command("effort", codex) is None


@pytest.mark.asyncio
async def test_slash_effort_applies_live_through_the_set_model_action(env) -> None:
    out = await env.orch.run_command(env.rec.id, "effort", "high")
    assert out is not None and out["render"] == "notice" and "high" in out["data"]["text"]
    assert env.control.patches == [{"effortLevel": "high"}]
    assert (await _row(env.rec.id)).reasoning_effort == "high"
    out = await env.orch.run_command(env.rec.id, "effort", "default")
    assert env.control.patches[-1] == {"effortLevel": None}
    assert env.rec.reasoning_effort is None
    bad = await env.orch.run_command(env.rec.id, "effort", "turbo")
    assert bad is not None and bad["render"] == "notice" and "turbo" in bad["data"]["text"]
    usage = await env.orch.run_command(env.rec.id, "effort", "")
    assert usage is not None and "Usage: /effort" in usage["data"]["text"]
    assert len(env.control.patches) == 2


@pytest.mark.asyncio
async def test_slash_effort_on_a_detached_session_saves_for_the_next_spawn(env) -> None:
    env.orch._drivers.pop(env.rec.id)
    out = await env.orch.run_command(env.rec.id, "effort", "low")
    assert out is not None and "saved" in out["data"]["text"]
    assert (await _row(env.rec.id)).reasoning_effort == "low"


@pytest.mark.asyncio
async def test_slash_effort_on_a_detached_session_checks_the_last_known_levels(env) -> None:
    env.orch._drivers.pop(env.rec.id)
    bad = await env.orch.run_command(env.rec.id, "effort", "turbo")
    assert bad is not None and "Effort unchanged" in bad["data"]["text"]
    assert (await _row(env.rec.id)).reasoning_effort is None
    env.rec.model_options = None                   # nothing known: accepted, as before
    ok = await env.orch.run_command(env.rec.id, "effort", "turbo")
    assert ok is not None and "saved" in ok["data"]["text"]
    assert (await _row(env.rec.id)).reasoning_effort == "turbo"


@pytest.mark.asyncio
async def test_the_session_catalog_follows_the_current_model(env) -> None:
    names = {c["name"] for c in await env.orch.command_catalog(env.rec.id)}
    assert "fast" in names
    await env.orch.set_model_for_session(env.rec.id, "sonnet")
    assert "fast" not in {c["name"] for c in await env.orch.command_catalog(env.rec.id)}


# ---------------------------------------------------------------- RPC / fleet / facade


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
async def test_remote_forwards_and_rebuilds_settings_errors() -> None:
    client = _RecordingClient(result={"enabled": True})
    assert await RemoteOrchestrator(client).set_fast_mode("s", True, identity="u") == {
        "enabled": True}
    assert client.calls == [("set_fast_mode", {"session_id": "s", "enabled": True,
                                               "identity": "u"})]
    for code in sorted(fs.SETTINGS_CODES):
        with pytest.raises(fs.SettingsError) as ei:
            await RemoteOrchestrator(_RecordingClient(error=DaemonError(
                "SettingsError", "why", {"code": code}))).set_fast_mode("s", True)
        assert ei.value.code == code
    with pytest.raises(KeyError):
        await RemoteOrchestrator(_RecordingClient(
            error=DaemonError("KeyError", "s"))).set_fast_mode("s", True)
    with pytest.raises(DaemonError) as old:
        await RemoteOrchestrator(_RecordingClient(
            error=DaemonError("UnknownMethod", "set_fast_mode"))).set_fast_mode("s", True)
    assert old.value.err_type == "UnknownMethod"
    with pytest.raises(DaemonError):          # an unknown code is never invented into a 409
        await RemoteOrchestrator(_RecordingClient(error=DaemonError(
            "SettingsError", "x", {"code": "WAT"}))).set_fast_mode("s", True)


@pytest.mark.asyncio
async def test_remote_set_model_rebuilds_a_settings_error() -> None:
    with pytest.raises(fs.SettingsError) as ei:
        await RemoteOrchestrator(_RecordingClient(error=DaemonError(
            "SettingsError", "bad", {"code": fs.SETTINGS_INVALID}))
        ).set_model_for_session("s", "opus", reasoning_effort="turbo")
    assert ei.value.code == fs.SETTINGS_INVALID


@pytest.mark.asyncio
async def test_uds_round_trip_on_the_owner_daemon(env, caplog) -> None:
    caplog.set_level(logging.INFO, logger="server.ipc.daemon")
    await env.orch.set_model_for_session(env.rec.id, "sonnet")
    app = create_daemon_app()
    register_orchestrator_rpc(app, env.orch)
    async with serve_daemon_uds(app) as sock:
        client = DaemonClient(sock)
        try:
            ro = RemoteOrchestrator(client)
            with pytest.raises(fs.SettingsError) as refused:
                await ro.set_fast_mode(env.rec.id, True)
            await ro.set_model_for_session(env.rec.id, "opus")
            assert await ro.set_fast_mode(env.rec.id, True) == {"enabled": True}
        finally:
            await client.aclose()
    assert refused.value.code == fs.FAST_UNAVAILABLE
    assert env.control.patches[-1] == {"fastMode": True}
    rpc_errors = [r for r in caplog.records
                  if r.name == "server.ipc.daemon" and "rpc.error" in r.getMessage()]
    assert rpc_errors and all(r.levelno == logging.INFO for r in rpc_errors)


@pytest.mark.asyncio
async def test_fleet_routes_to_the_owning_host_and_facade_forwards() -> None:
    assert "set_fast_mode" in _PER_SESSION

    class _Host:
        def __init__(self, name):
            self.name, self.calls = name, []

        async def set_fast_mode(self, session_id, enabled, **kw):
            self.calls.append((session_id, enabled, kw))
            return {"enabled": enabled, "host": self.name}

    class _Router:
        def __init__(self, orchs):
            self._orchs = orchs

        def get(self, name):
            return self._orchs[name]

        def names(self):
            return sorted(self._orchs)

    dobby, rocky = _Host("dobby"), _Host("rocky")
    fo = FleetOrchestrator(_Router({"dobby": dobby, "rocky": rocky}), primary="dobby")  # type: ignore[arg-type]
    fo._host_cache["s"] = "rocky"
    assert (await fo.set_fast_mode("s", True, identity="u"))["host"] == "rocky"
    assert dobby.calls == []
    adapter = InProcessOrchestratorAdapter(dobby)  # type: ignore[arg-type]
    assert (await adapter.set_fast_mode("s", False))["host"] == "dobby"


def test_the_orchestrator_method_signatures_agree() -> None:
    """The remote proxy speaks the orchestrator's own keyword names."""
    own = inspect.signature(SessionOrchestrator.set_fast_mode).parameters
    proxy = inspect.signature(RemoteOrchestrator.set_fast_mode).parameters
    assert list(own) == list(proxy)


# ---------------------------------------------------------------- HTTP

_ENV = {"VIBERPIT_DB_URL": "sqlite+aiosqlite:///:memory:", "VIBERPIT_DEV": "1",
        "VIBERPIT_TAILNET_ALLOWLIST": ""}
SID = "55555555-5555-4555-8555-555555555555"


def _client():
    with patch.dict(os.environ, _ENV):
        orch = SessionOrchestrator(sdk_factory=Spawns(FakeControl()), poll_interval=9999.0,
                                   usage_fn=lambda name: None)
        app = create_app(orchestrator=orch)
    orch._records[SID] = SessionRecord(id=SID, name="n", account="a",
                                       engine=AgentEngine.CLAUDE_CODE, engine_session_id="c",
                                       cols=80, rows=24, cwd=None)  # type: ignore[arg-type]
    return TestClient(app, raise_server_exceptions=False)


def _post(c, body, sid=SID):
    return c.post(f"/api/sessions/{sid}/fast", json=body, headers=_H)


def test_http_fast_is_200_with_enabled() -> None:
    async def fake(self, session_id, enabled, *, identity="system"):
        assert (session_id, enabled, identity) == (SID, True, "testuser@example.com")
        return {"enabled": True}

    with patch.dict(os.environ, _ENV), _client() as c, \
            patch.object(SessionOrchestrator, "set_fast_mode", fake):
        r = _post(c, {"enabled": True})
    assert r.status_code == 200 and r.json() == {"enabled": True}


def test_http_fast_unavailable_is_409() -> None:
    with patch.dict(os.environ, _ENV), _client() as c:
        r = _post(c, {"enabled": True})          # no model list cached: unavailable
    assert r.status_code == 409 and r.json()["detail"]["code"] == fs.FAST_UNAVAILABLE


@pytest.mark.parametrize("code", sorted(fs.SETTINGS_CODES))
def test_http_refusals_are_409_with_their_code(code: str) -> None:
    with patch.dict(os.environ, _ENV), _client() as c, \
            patch.object(SessionOrchestrator, "set_fast_mode",
                         side_effect=fs.SettingsError(code, "why")):
        r = _post(c, {"enabled": True})
    assert r.status_code == 409 and r.json()["detail"]["code"] == code


def test_http_unknown_session_old_daemon_and_bad_body() -> None:
    with patch.dict(os.environ, _ENV), _client() as c:
        assert _post(c, {"enabled": False}, sid="nope").status_code == 404
        assert _post(c, {"enabled": "maybe"}).status_code == 422
        with patch.object(SessionOrchestrator, "set_fast_mode",
                          side_effect=DaemonError("UnknownMethod", "set_fast_mode")):
            r = _post(c, {"enabled": True})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "daemon_outdated"


def test_http_requires_identity() -> None:
    with patch.dict(os.environ, _ENV), _client() as c:
        assert c.post(f"/api/sessions/{SID}/fast", json={"enabled": True}).status_code == 401


@pytest.mark.asyncio
async def test_payload_serves_the_current_models_supported_effort_levels(env) -> None:
    """#3260: the levels ride the daemon→edge copy, follow a model switch, and a model
    listing none serves [] (the picker hides); no list yet serves null."""
    assert _payload(env.rec)["supportedEffortLevels"] == _LEVELS
    env.rec.model_options = [{"value": "opus", "supportedEffortLevels": ["low"]},
                             {"value": "sonnet", "supportedEffortLevels": []}]
    assert _payload(env.rec)["supportedEffortLevels"] == ["low"]
    env.rec.model = "sonnet"
    assert _payload(env.rec)["supportedEffortLevels"] == []
    env.rec.model_options = None
    assert _payload(env.rec)["supportedEffortLevels"] is None
