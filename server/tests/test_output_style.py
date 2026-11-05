"""D-16a server side: per-session output styles and a Settings default.

- The CLI lists its styles in ``initialize.available_output_styles`` and reports the
  current one as ``initialize.output_style``. The driver hands both to the orchestrator
  once per CLI start; the record caches them (in memory, as D-14 caches the fast list).
- The session's choice is persisted (``sessions.output_style``); NULL means the app
  default (``app_settings.default_output_style``), NULL there means the CLI's
  ``default``. Every spawn passes the effective style in the ``--settings`` JSON unless
  it is ``default`` (Review Focus 1).
- A live change goes through ``apply_flag_settings {outputStyle}``. The CLI accepts any
  name silently, so every name is validated against the cached list first: an unknown
  one is ``OUTPUT_STYLE_UNKNOWN`` and nothing reaches the CLI (Review Focus 2).
- `/output-style` and `/plan` are app commands; the CLI's own `/output-style` (which
  writes ``.claude/settings.local.json`` into the worktree) never passes through
  (Review Focus 3). `/plan off` restores the pre-plan mode (Review Focus 4).

Only the CLI is faked; ``test_output_style_cli_contract`` checks the wire against the
installed binary.
"""
from __future__ import annotations

import inspect
import logging
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select

from viberpit.selector import Picked
from server.app import create_app
from server.db import db_session, dispose_db, init_db
from server.db.models import AppSettings
from server.db.models import Session as DbSession
from server.ipc.client import DaemonClient, DaemonError
from server.ipc.daemon import create_daemon_app
from server.ipc.orchestrator_rpc import register_orchestrator_rpc
from server.sessions import flag_settings as fs
from server.sessions.claude_driver import ClaudeAgentDriver
from server.sessions.commands.catalog import APP, APP_ONLY, app_command, build_catalog
from server.sessions.engine import AgentCapabilities, AgentEngine
from server.sessions.facade import InProcessOrchestratorAdapter
from server.sessions.fleet_orchestrator import _PER_SESSION, FleetOrchestrator
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.permissions import PermissionRegistry
from server.sessions.remote import RemoteOrchestrator
from server.sessions.types import SessionRecord
from server.tests.fakes.fake_sdk import FakeSDKClient
from server.tests.ipc_harness import serve_daemon_uds
from server.tests.test_fast_mode import (MODELS, FakeControl, _control_error, _fake_usage,
                                         _no_title, _no_warnings, _seed_account)

_H = {"Tailscale-User-Login": "testuser@example.com"}

BUILTINS = ["default", "Proactive", "Concise", "Explanatory", "Learning"]
# `initialize` as CLI 2.1.318 answers it (D-16 spike §3.1, probe 1), trimmed.
INFO = {"commands": [{"name": "output-style", "description": "Set the output style"},
                     {"name": "compact", "description": "Compact"}],
        "models": MODELS, "output_style": "default",
        "available_output_styles": BUILTINS + ["Terse"]}
STYLES = BUILTINS + ["Terse"]


# ---------------------------------------------------------------- initialize reads


def test_output_styles_reads_the_initialize_list() -> None:
    assert fs.output_styles(INFO) == STYLES
    assert fs.output_styles(None) is None
    assert fs.output_styles({}) is None
    assert fs.output_styles({"available_output_styles": "x"}) is None
    assert fs.output_styles({"available_output_styles": ["a", 3, "", "b"]}) == ["a", "b"]
    assert fs.live_output_style(INFO) == "default"
    assert fs.live_output_style({"output_style": ""}) is None
    assert fs.live_output_style(None) is None


def test_the_builtins_are_the_clis_five() -> None:
    assert list(fs.BUILTIN_OUTPUT_STYLES) == BUILTINS
    assert fs.DEFAULT_OUTPUT_STYLE == "default"


def test_the_output_style_codes_are_settings_codes() -> None:
    for code in (fs.OUTPUT_STYLE_UNKNOWN, fs.OUTPUT_STYLE_UNAVAILABLE):
        assert code in fs.SETTINGS_CODES
        assert fs.SettingsError(code).error_data == {"code": code}


@pytest.mark.asyncio
async def test_driver_reports_the_style_list_once_per_cli_start() -> None:
    seen: list = []

    async def on_styles(sid, styles, current):
        seen.append((sid, styles, current))

    def make(cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *,
             on_pre_compact=None):
        return FakeSDKClient([], on_can_use_tool=can_use_tool, startup_frames=[],
                             server_info=INFO)

    d = ClaudeAgentDriver("s1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=make, registry=PermissionRegistry(), identity="u",
                          on_output_styles=on_styles)
    await d.start()
    try:
        assert seen == [("s1", STYLES, "default")]
        await d.reconnect()
        assert len(seen) == 2
    finally:
        await d.aclose()


# ---------------------------------------------------------------- the orchestrator


class Spawns:
    """A fake SDK factory declaring ``flag_settings`` (like the real one)."""

    def __init__(self, control: FakeControl, info: dict = INFO) -> None:
        self.control, self.info = control, info
        self.calls: list[dict] = []
        self.clients: list[FakeSDKClient] = []

    def __call__(self, cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *,
                 on_pre_compact=None, options_overlay=None, flag_settings=None, **kw):
        self.calls.append({"resume": resume, "flag_settings": flag_settings, **kw})
        client = FakeSDKClient([], on_can_use_tool=can_use_tool, startup_frames=[],
                               server_info=self.info)
        client._query = self.control  # type: ignore[attr-defined]
        self.clients.append(client)
        return client


@pytest_asyncio.fixture
async def env(tmp_path: Path):
    """A real orchestrator, Claude driver and SQLite DB on the fake CLI."""
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
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'style.db'}"
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
    rec = await orch.create_session("Styled", cwd=str(clone), model="opus",
                                    permission_mode="full-auto")
    try:
        yield SimpleNamespace(orch=orch, spawns=spawns, control=control, rec=rec,
                              repo_root=repo_root, clone=clone)
    finally:
        await orch.stop()
        await dispose_db()
        for p in reversed(patches):
            p.stop()


async def _row(sid: str) -> DbSession:
    async with db_session() as db:
        return (await db.execute(select(DbSession).where(DbSession.id == sid))).scalar_one()


async def _set_app_default(style: str | None) -> None:
    async with db_session() as db:
        row = (await db.execute(select(AppSettings).where(
            AppSettings.id == "singleton"))).scalar_one_or_none()
        if row is None:
            row = AppSettings(id="singleton")
            db.add(row)
        row.default_output_style = style


def _payload(rec: SessionRecord) -> dict:
    from server.api.sessions import _record_to_dict
    return _record_to_dict(SessionRecord.from_dict(rec.to_dict()))


@pytest.mark.asyncio
async def test_a_started_session_caches_the_styles_and_serves_them(env) -> None:
    assert env.rec.output_styles == STYLES
    assert env.orch.available_output_styles(env.rec.id) == STYLES
    p = _payload(env.rec)            # through the daemon→edge RPC copy, as served
    assert p["availableOutputStyles"] == STYLES and p["outputStyle"] == "default"


@pytest.mark.asyncio
async def test_set_output_style_writes_live_persists_and_returns_the_style(env, caplog) -> None:
    caplog.set_level(logging.INFO)
    assert await env.orch.set_output_style(env.rec.id, "Explanatory") == {
        "style": "Explanatory"}
    assert env.control.patches == [{"outputStyle": "Explanatory"}]
    assert env.rec.output_style == "Explanatory"
    assert (await _row(env.rec.id)).output_style == "Explanatory"
    assert _payload(env.rec)["outputStyle"] == "Explanatory"
    assert await env.orch.set_output_style(env.rec.id, "Terse") == {"style": "Terse"}
    assert env.control.patches[-1] == {"outputStyle": "Terse"}
    assert _no_warnings(caplog) == []


# Review Focus 2
@pytest.mark.asyncio
async def test_an_unknown_style_is_refused_and_nothing_reaches_the_cli(env, caplog) -> None:
    caplog.set_level(logging.INFO)
    with pytest.raises(fs.SettingsError) as ei:
        await env.orch.set_output_style(env.rec.id, "NoSuchStyle")
    assert ei.value.code == fs.OUTPUT_STYLE_UNKNOWN
    assert env.control.sent == []
    assert env.rec.output_style is None and (await _row(env.rec.id)).output_style is None
    assert any("output_style refused" in r.getMessage() and r.levelno == logging.INFO
               for r in caplog.records)
    assert _no_warnings(caplog) == []


@pytest.mark.asyncio
async def test_null_resets_to_the_app_default_live(env) -> None:
    await env.orch.set_output_style(env.rec.id, "Learning")
    assert await env.orch.set_output_style(env.rec.id, None) == {"style": None}
    assert env.control.patches[-1] == {"outputStyle": "default"}
    assert (await _row(env.rec.id)).output_style is None
    assert _payload(env.rec)["outputStyle"] == "default"
    await _set_app_default("Concise")
    await env.orch.refresh_default_output_style()
    await env.orch.set_output_style(env.rec.id, None)
    assert env.control.patches[-1] == {"outputStyle": "Concise"}
    assert _payload(env.rec)["outputStyle"] == "Concise"


@pytest.mark.asyncio
async def test_an_engine_without_flag_settings_is_unavailable(env) -> None:
    with patch.object(SessionOrchestrator, "_capabilities_of",
                      lambda self, sid: AgentCapabilities(models=True)):
        assert env.orch.available_output_styles(env.rec.id) == []
        with pytest.raises(fs.SettingsError) as ei:
            await env.orch.set_output_style(env.rec.id, "Concise")
    assert ei.value.code == fs.OUTPUT_STYLE_UNAVAILABLE
    assert env.control.sent == []


@pytest.mark.asyncio
async def test_an_unknown_list_is_unavailable(env) -> None:
    env.rec.output_styles = None              # e.g. respawned on a restart, not yet started
    with pytest.raises(fs.SettingsError) as ei:
        await env.orch.set_output_style(env.rec.id, "Concise")
    assert ei.value.code == fs.OUTPUT_STYLE_UNAVAILABLE


@pytest.mark.asyncio
async def test_the_kill_switch_turns_output_styles_off(env, monkeypatch) -> None:
    monkeypatch.setenv("VIBERPIT_FLAG_SETTINGS_ENABLED", "false")
    assert env.orch.available_output_styles(env.rec.id) == []
    assert _payload(env.rec)["availableOutputStyles"] == []
    with pytest.raises(fs.SettingsError) as ei:
        await env.orch.set_output_style(env.rec.id, "Concise")
    assert ei.value.code == fs.OUTPUT_STYLE_UNAVAILABLE
    env.rec.output_style = "Concise"
    assert env.orch._spawn_flag_settings(env.rec.id) == {}


def test_a_codex_record_serves_no_styles() -> None:
    rec = SessionRecord(id="c", name="n", account="a", engine=AgentEngine.CODEX,
                        engine_session_id="t", cols=80, rows=24, cwd="/w",
                        output_styles=["default", "Concise"])
    from server.api.sessions import _record_to_dict
    assert _record_to_dict(rec)["availableOutputStyles"] == []


@pytest.mark.asyncio
async def test_an_assistant_session_never_takes_a_style(env) -> None:
    env.rec.kind = "assistant"
    await _set_app_default("Concise")
    await env.orch.refresh_default_output_style()
    assert env.orch.available_output_styles(env.rec.id) == []
    assert "outputStyle" not in env.orch._spawn_flag_settings(env.rec.id)
    with pytest.raises(fs.SettingsError) as ei:
        await env.orch.set_output_style(env.rec.id, "Concise")
    assert ei.value.code == fs.OUTPUT_STYLE_UNAVAILABLE


@pytest.mark.asyncio
async def test_a_cli_refusal_keeps_the_record(env) -> None:
    env.control.replies = [_control_error("nope")]
    with pytest.raises(fs.SettingsError) as ei:
        await env.orch.set_output_style(env.rec.id, "Concise")
    assert ei.value.code == fs.SETTINGS_REFUSED
    assert env.rec.output_style is None and (await _row(env.rec.id)).output_style is None


@pytest.mark.asyncio
async def test_a_driverless_session_persists_for_the_next_spawn(env) -> None:
    env.orch._drivers.pop(env.rec.id)
    assert await env.orch.set_output_style(env.rec.id, "Learning") == {"style": "Learning"}
    assert env.control.sent == [] and (await _row(env.rec.id)).output_style == "Learning"
    assert env.orch._spawn_flag_settings(env.rec.id) == {"outputStyle": "Learning"}


@pytest.mark.asyncio
async def test_unknown_session_is_a_keyerror(env) -> None:
    with pytest.raises(KeyError):
        await env.orch.set_output_style("nope", "Concise")


@pytest.mark.asyncio
async def test_spawn_flags_follow_the_session_then_the_app_default(env) -> None:
    sid = env.rec.id
    assert env.orch._spawn_flag_settings(sid) == {}           # nothing set: as before
    await _set_app_default("Concise")
    await env.orch.refresh_default_output_style()
    assert env.orch._spawn_flag_settings(sid) == {"outputStyle": "Concise"}
    env.rec.output_style = "Learning"
    assert env.orch._spawn_flag_settings(sid) == {"outputStyle": "Learning"}
    env.rec.output_style = "default"          # the session's own pick is always sent
    assert env.orch._spawn_flag_settings(sid) == {"outputStyle": "default"}
    env.rec.output_style = None
    await _set_app_default("default")
    await env.orch.refresh_default_output_style()
    assert env.orch._spawn_flag_settings(sid) == {}


@pytest.mark.asyncio
async def test_a_respawn_carries_the_style(env) -> None:
    await env.orch.set_output_style(env.rec.id, "Explanatory")
    await env.orch._drivers[env.rec.id].reconnect()
    assert env.spawns.calls[-1]["flag_settings"] == {"outputStyle": "Explanatory"}


# Review Focus 1
@pytest.mark.asyncio
async def test_a_daemon_restart_respawns_with_the_style_from_the_row(env) -> None:
    await env.orch.set_output_style(env.rec.id, "Learning")
    sid = env.rec.id
    assert env.spawns.calls[0]["flag_settings"] is None      # created with nothing
    spawns = Spawns(FakeControl())
    orch2 = SessionOrchestrator(sdk_factory=spawns, poll_interval=9999.0,
                                usage_fn=_fake_usage, repo_root=str(env.repo_root))
    try:
        await orch2.rehydrate()
        assert orch2._records[sid].output_style == "Learning"
        assert await orch2.ensure_driver(sid) is not None
        assert spawns.calls[-1]["flag_settings"] == {"outputStyle": "Learning"}
        assert orch2._records[sid].output_styles == STYLES
    finally:
        await orch2.stop()


@pytest.mark.asyncio
async def test_a_restarted_daemon_spawns_with_the_app_default(env) -> None:
    await _set_app_default("Concise")
    sid = env.rec.id
    spawns = Spawns(FakeControl())
    orch2 = SessionOrchestrator(sdk_factory=spawns, poll_interval=9999.0,
                                usage_fn=_fake_usage, repo_root=str(env.repo_root))
    try:
        await orch2.rehydrate()
        assert await orch2.ensure_driver(sid) is not None
        assert spawns.calls[-1]["flag_settings"] == {"outputStyle": "Concise"}
    finally:
        await orch2.stop()


# ---------------------------------------------------------------- commands

CLAUDE = ClaudeAgentDriver.capabilities
CODEX = AgentCapabilities(models=True)


def test_output_style_and_plan_are_app_only_commands() -> None:
    assert {"output-style", "plan"} <= APP_ONLY
    by = {c.name: c for c in build_catalog(INFO, CLAUDE, kind=None)}
    assert by["output-style"].source == APP and by["output-style"].kind == "control"
    assert by["output-style"].argument_hint == "[style]"
    assert by["plan"].source == APP and by["plan"].kind == "control"
    assert by["plan"].argument_hint == "[on|off]"
    assert "compact" in by
    codex = {c.name for c in build_catalog(INFO, CODEX, kind=None)}
    assert "output-style" not in codex and "plan" in codex   # never the CLI's own either
    assert app_command("output-style", CODEX) is None
    assert app_command("plan", CODEX) is not None


class _Spy:
    """Records every prompt the session's CLI driver is handed."""

    def __init__(self, driver) -> None:
        self.prompts: list = []
        real = driver.send_prompt

        async def spy(text, *a, **k):
            self.prompts.append(text)
            return await real(text, *a, **k)
        driver.send_prompt = spy


async def _audits(type_: str) -> list:
    from server.db.models import AuditEvent
    async with db_session() as db:
        return list((await db.execute(select(AuditEvent).where(
            AuditEvent.type == type_))).scalars().all())


# Review Focus 3
@pytest.mark.asyncio
async def test_slash_output_style_applies_through_the_same_action(env) -> None:
    out = await env.orch.run_command(env.rec.id, "output-style", "Explanatory",
                                     identity="u@example.com")
    assert out is not None and out["render"] == "notice"
    assert out["data"]["text"] == "Output style → Explanatory"
    assert env.control.patches == [{"outputStyle": "Explanatory"}]
    assert (await _row(env.rec.id)).output_style == "Explanatory"
    bad = await env.orch.run_command(env.rec.id, "output-style", "NoSuchStyle")
    assert bad is not None and "NoSuchStyle" in bad["data"]["text"]
    assert "unchanged" in bad["data"]["text"]
    assert len(env.control.patches) == 1
    reset = await env.orch.run_command(env.rec.id, "output-style", "default")
    assert reset is not None and env.control.patches[-1] == {"outputStyle": "default"}


@pytest.mark.asyncio
async def test_slash_output_style_is_audited(env) -> None:
    await env.orch.run_command(env.rec.id, "output-style", "Concise", identity="u@example.com")
    await env.orch.run_command(env.rec.id, "output-style", "NoSuchStyle")   # refused
    rows = await _audits("session.output_style")
    assert [(r.identity, r.detail) for r in rows] == [
        ("u@example.com", {"sessionId": env.rec.id, "style": "Concise"})]


@pytest.mark.asyncio
async def test_session_actions_set_output_style_audits_and_propagates_refusals(env) -> None:
    from server.sessions import session_actions
    async with db_session() as db:
        out = await session_actions.set_output_style(
            env.orch, db, session_id=env.rec.id, style="Learning", identity="u")
        assert out == {"style": "Learning"}
        with pytest.raises(fs.SettingsError):
            await session_actions.set_output_style(
                env.orch, db, session_id=env.rec.id, style="Nope", identity="u")
    rows = await _audits("session.output_style")
    assert [r.detail for r in rows] == [{"sessionId": env.rec.id, "style": "Learning"}]


# Every prompt path goes through orchestrator.send_prompt, which routes an APP_ONLY first
# line to the app command — only for a person (fix round 2, S1) — and never to the CLI.
HUMAN = {"command_origin": "composer", "identity": "u@example.com"}
APP_ONLY_PROMPTS = ["/output-style Concise", "/output-style", "/fast on", "/effort high",
                    "/plan on", "  /output-style Learning\n\n"]


def _emitted(driver) -> list:
    """Every CommandResult the orchestrator emits on *driver*, as wire dicts."""
    from server.sessions.events import CommandResult
    seen: list = []
    real = driver.emit

    def spy(e) -> None:
        if isinstance(e, CommandResult):
            seen.append(e.to_wire())
        real(e)
    driver.emit = spy                                           # type: ignore[method-assign]
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize("text", APP_ONLY_PROMPTS)
async def test_a_persons_app_only_prompt_runs_and_never_reaches_the_cli(env, text) -> None:
    from server.sessions.commands.catalog import APP_COMMAND
    driver = env.orch._drivers[env.rec.id]
    spy, results = _Spy(driver), _emitted(driver)
    assert await env.orch.send_prompt(env.rec.id, text, **HUMAN) == APP_COMMAND
    assert spy.prompts == []
    assert len(results) == 1 and results[0]["type"] == "commandResult"
    assert results[0]["render"] == "notice" and results[0]["ok"] is True


# S1: every non-human sender, and a call naming none, is refused — never run, never sent.
NON_HUMAN_SENDS: dict[str, dict] = {
    "default (no origin)": {},
    "autopilot": {"origin": {"kind": "autopilot"}},
    "completion nudge": {"origin": {"kind": "completion"}},
    "assistant": {"origin": {"kind": "assistant"}},
    "automation": {"origin": {"kind": "automation", "label": "PR Doctor"}},
    "unknown origin": {"command_origin": "agent", "identity": "u@example.com"},
    "human origin, no identity": {"command_origin": "composer"},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("sender", sorted(NON_HUMAN_SENDS))
@pytest.mark.parametrize("text", ["/plan off", "/fast on", "/output-style Concise"])
async def test_a_non_human_app_only_prompt_is_refused(env, sender, text, caplog) -> None:
    from server.sessions.commands.catalog import APP_COMMAND_REFUSED
    caplog.set_level(logging.INFO)
    await env.orch.run_command(env.rec.id, "plan", "on", identity="u@example.com")
    driver = env.orch._drivers[env.rec.id]
    spy, results = _Spy(driver), _emitted(driver)
    sent_before = list(env.control.sent)
    status = await env.orch.send_prompt(env.rec.id, text, **NON_HUMAN_SENDS[sender])
    assert status == APP_COMMAND_REFUSED
    assert spy.prompts == [] and env.control.sent == sent_before     # nothing ran
    assert driver.mode == "plan" and (await _row(env.rec.id)).output_style is None
    name = text.split()[0][1:]
    assert [r["data"]["text"] for r in results] == [
        f"Slash commands from automation are not run: /{name}"]
    assert any("app command refused" in r.getMessage() and r.levelno == logging.INFO
               for r in caplog.records)
    assert _no_warnings(caplog) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("text,dropped", [
    ("/plan\nnow write the migration", "the text after it"),
    ("/output-style Concise\nand also this", "the text after it"),
])
async def test_an_app_command_with_more_lines_is_refused_whole(env, text, dropped) -> None:
    """M1: `/plan\n<task>` must not toggle plan mode and silently drop the task."""
    from server.sessions.commands.catalog import APP_COMMAND_REFUSED, app_only_command
    driver = env.orch._drivers[env.rec.id]
    spy, results = _Spy(driver), _emitted(driver)
    assert app_only_command(text).extra_lines is True              # type: ignore[union-attr]
    assert await env.orch.send_prompt(env.rec.id, text, **HUMAN) == APP_COMMAND_REFUSED
    assert spy.prompts == [] and env.control.sent == [] and driver.mode == "full-auto"
    assert len(results) == 1 and dropped in results[0]["data"]["text"]
    assert "nothing was sent" in results[0]["data"]["text"]


@pytest.mark.asyncio
async def test_an_app_command_with_attachments_is_refused_whole(env) -> None:
    from server.sessions.commands.catalog import APP_COMMAND_REFUSED
    driver = env.orch._drivers[env.rec.id]
    spy, results = _Spy(driver), _emitted(driver)
    assert await env.orch.send_prompt(env.rec.id, "/output-style Concise", ["att-1"],
                                      **HUMAN) == APP_COMMAND_REFUSED
    assert spy.prompts == [] and env.control.patches == []
    assert "the attachments" in results[0]["data"]["text"]


def test_the_multi_line_rule_is_parse_command_lines() -> None:
    """One rule: a prompt is a single command exactly when parse_command_line says so."""
    from server.sessions.commands.catalog import app_only_command, parse_command_line
    for text in ("/fast on", "/fast\n", "\n/fast on\n\n", "/fast\nmore", "/fast\n\nmore"):
        cmd = app_only_command(text)
        assert cmd is not None
        assert cmd.extra_lines is (parse_command_line(text.strip()) is None)
    assert app_only_command("/compact") is None and app_only_command("hello /fast") is None


@pytest.mark.asyncio
async def test_an_ordinary_prompt_still_reaches_the_cli(env) -> None:
    spy = _Spy(env.orch._drivers[env.rec.id])
    assert await env.orch.send_prompt(env.rec.id, "please use /output-style later") == "sent"
    assert await env.orch.send_prompt(env.rec.id, "/compact") == "sent"
    assert spy.prompts == ["please use /output-style later", "/compact"]


# M2: a command that raises answers ok:false (INFO), never escapes send_prompt.
@pytest.mark.asyncio
async def test_a_failing_app_command_answers_ok_false(env, caplog) -> None:
    from server.sessions.commands.catalog import APP_COMMAND
    caplog.set_level(logging.INFO)
    driver = env.orch._drivers[env.rec.id]
    results = _emitted(driver)
    with patch.object(SessionOrchestrator, "set_permission_mode",
                      side_effect=RuntimeError("db down")):
        assert await env.orch.send_prompt(env.rec.id, "/plan on", **HUMAN) == APP_COMMAND
    assert results == [{"type": "commandResult", "ok": False, "name": "plan",
                        "error": "command failed"}]
    assert _no_warnings(caplog) == []


@pytest.mark.asyncio
async def test_a_cancelled_app_command_is_never_swallowed(env) -> None:
    import asyncio
    with patch.object(SessionOrchestrator, "set_permission_mode",
                      side_effect=asyncio.CancelledError()):
        with pytest.raises(asyncio.CancelledError):
            await env.orch.send_prompt(env.rec.id, "/plan on", **HUMAN)


# M4: no live driver — nothing runs, and the answer is not a delivery.
@pytest.mark.asyncio
async def test_an_app_command_on_a_session_without_a_driver_is_not_delivered(env) -> None:
    env.orch._drivers.pop(env.rec.id)
    assert await env.orch.send_prompt(env.rec.id, "/output-style Learning",
                                      **HUMAN) == "no_driver"
    assert (await _row(env.rec.id)).output_style is None


# The spawn's initial prompt: only a person's create request may run an app command.
class _RecordingSpawns(Spawns):
    def __init__(self, control, sent) -> None:
        super().__init__(control)
        self.sent = sent

    def __call__(self, *a, **k):
        client = super().__call__(*a, **k)
        real = client.query

        async def query(prompt, *qa, **qk):
            self.sent.append(prompt)
            return await real(prompt, *qa, **qk)
        client.query = query                                # type: ignore[method-assign]
        return client


@pytest.mark.asyncio
@pytest.mark.parametrize("human,attachments,parked", [
    (False, None, False),          # automation / child dispatch: refused, never parked
    (False, ["att-1"], False),
    (True, ["att-1"], False),      # a person, but with attachments: refused whole (M1)
    (True, None, True),            # a person's bare command: parked for the bridge
])
async def test_a_spawn_initial_app_command(env, human, attachments, parked) -> None:
    from server.sessions import attachments as _att
    sent: list = []
    env.orch._sdk_factory = _RecordingSpawns(env.control, sent)
    with patch.object(_att, "resolve_and_bind", side_effect=AssertionError("bound")), \
            patch.object(SessionOrchestrator, "run_command",
                         side_effect=RuntimeError("must not run at spawn")):
        rec = await env.orch.create_session(
            "Spawned", cwd=str(env.clone), model="opus", permission_mode="full-auto",
            identity="u@example.com", initial_prompt="/fast on",
            attachment_ids=attachments, initial_prompt_human=human)
    assert sent == []
    assert (rec.id in env.orch._pending_initial_prompt) is parked
    assert rec.id not in env.orch._human_initial_prompts


class _FakeDriver:
    """A live-looking driver that records what the CLI would be handed."""
    capabilities = ClaudeAgentDriver.capabilities
    identity = "first-attacher@example.com"      # pinned by _rethread_identity
    turn_active = False
    stream_ended = False

    def __init__(self) -> None:
        self.prompts: list = []
        self.emitted: list = []
        self.mode = "full-auto"

    async def send_prompt(self, text, *a, **k):
        self.prompts.append(text)

    async def set_permission_mode(self, mode):
        self.mode = mode

    def emit(self, e) -> None:
        self.emitted.append(e)


_BOB = {"Tailscale-User-Login": "bob@example.com"}


def _rest(text: str, *, human_gate=None, headers=_H, fail: bool = False):
    """POST /sessions/{id}/prompt against a fake live driver; returns (response, driver,
    audit rows for permission_mode.change)."""
    from server.db.models import AuditEvent
    driver = _FakeDriver()

    async def fake_ensure(self, session_id, identity=None):
        return driver

    async def audits():
        async with db_session() as db:
            return [(r.identity, r.detail) for r in (await db.execute(select(AuditEvent).where(
                AuditEvent.type == "permission_mode.change"))).scalars().all()]

    with patch.dict(os.environ, _ENV), _client(STYLES) as c, \
            patch.object(SessionOrchestrator, "ensure_driver", fake_ensure), \
            patch("server.api.sessions.human_gate_via", return_value=human_gate):
        orch = c.app.state.sessions
        getattr(orch, "_orch", orch)._drivers[SID] = driver
        if fail:
            with patch.object(SessionOrchestrator, "set_permission_mode",
                              side_effect=RuntimeError("db down")):
                r = c.post(f"/api/sessions/{SID}/prompt", json={"text": text}, headers=headers)
        else:
            r = c.post(f"/api/sessions/{SID}/prompt", json={"text": text}, headers=headers)
        rows = c.portal.call(audits)
    return r, driver, rows


def test_rest_runs_a_persons_command_as_that_person() -> None:
    """S2: the audit row names the sender, not the driver's pinned first attacher."""
    r, driver, rows = _rest("/plan on", headers=_BOB)
    assert r.status_code == 200, r.text
    assert driver.prompts == [] and driver.mode == "plan"
    assert rows == [("bob@example.com", {"sessionId": SID, "mode": "plan"})]


def test_rest_refuses_an_unattested_local_caller() -> None:
    r, driver, rows = _rest("/plan on", human_gate="local-process")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "app_command_refused"
    assert driver.prompts == [] and driver.mode == "full-auto" and rows == []


def test_rest_refuses_a_command_with_more_text() -> None:
    r, driver, _ = _rest("/plan on\nthen do the work")
    assert r.status_code == 409 and driver.prompts == [] and driver.mode == "full-auto"


def test_rest_a_failing_command_is_not_a_500() -> None:
    r, driver, _ = _rest("/plan on", fail=True)
    assert r.status_code == 200, r.text
    assert driver.emitted[0].to_wire()["ok"] is False


def test_the_assistant_token_path_is_refused() -> None:
    """The assistant tool's POST /prompt carries via="assistant": not a person."""
    from starlette.middleware.base import BaseHTTPMiddleware
    driver = _FakeDriver()

    async def fake_ensure(self, session_id, identity=None):
        return driver

    class _ViaAssistant(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.via = "assistant"
            return await call_next(request)

    with patch.dict(os.environ, _ENV):
        orch = SessionOrchestrator(sdk_factory=Spawns(FakeControl()), poll_interval=9999.0,
                                   usage_fn=lambda name: None)
        app = create_app(orchestrator=orch)
        app.add_middleware(_ViaAssistant)
    orch._records[SID] = SessionRecord(id=SID, name="n", account="a",
                                       engine=AgentEngine.CLAUDE_CODE, engine_session_id="c",
                                       cols=80, rows=24, cwd=None)  # type: ignore[arg-type]
    orch._drivers[SID] = driver  # type: ignore[assignment]
    with patch.dict(os.environ, _ENV), TestClient(app, raise_server_exceptions=False) as c, \
            patch.object(SessionOrchestrator, "ensure_driver", fake_ensure), \
            patch("server.api.sessions.human_gate_via", return_value=None):
        r = c.post(f"/api/sessions/{SID}/prompt", json={"text": "/plan off"}, headers=_H)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "app_command_refused"
    assert driver.prompts == [] and driver.mode == "full-auto"


def test_the_automations_attach_is_refused() -> None:
    """M3: the automations attach shares resume_and_send_prompt without human=True."""
    from server.api.sessions import resume_and_send_prompt
    from server.sessions.commands.catalog import APP_COMMAND_REFUSED
    driver = _FakeDriver()

    async def fake_ensure(self, session_id, identity=None):
        return driver

    with patch.dict(os.environ, _ENV), _client(STYLES) as c, \
            patch.object(SessionOrchestrator, "ensure_driver", fake_ensure):
        orch = c.app.state.sessions
        getattr(orch, "_orch", orch)._drivers[SID] = driver
        _, delivery = c.portal.call(resume_and_send_prompt, orch, SID, "/fast on", "u")
    assert delivery == APP_COMMAND_REFUSED and driver.prompts == []


@pytest.mark.asyncio
async def test_bare_slash_output_style_lists_the_styles(env) -> None:
    await env.orch.set_output_style(env.rec.id, "Concise")
    out = await env.orch.run_command(env.rec.id, "output-style", "")
    assert out is not None and out["render"] == "notice"
    text = out["data"]["text"]
    assert "Concise (current)" in text
    for name in STYLES:
        assert name in text
    assert len(env.control.patches) == 1


@pytest.mark.asyncio
async def test_slash_output_style_gated_off_is_a_notice_not_the_clis(env, monkeypatch) -> None:
    monkeypatch.setenv("VIBERPIT_FLAG_SETTINGS_ENABLED", "false")
    spy = _Spy(env.orch._drivers[env.rec.id])
    out = await env.orch.run_command(env.rec.id, "output-style", "Concise")
    assert out is not None and out["render"] == "notice"
    assert "isn't available" in out["data"]["text"]
    assert spy.prompts == [] and env.control.sent == []


@pytest.mark.asyncio
async def test_slash_output_style_on_a_detached_session_saves_for_the_next_spawn(env) -> None:
    env.orch._drivers.pop(env.rec.id)
    out = await env.orch.run_command(env.rec.id, "output-style", "Learning")
    assert out is not None and "saved" in out["data"]["text"]
    assert (await _row(env.rec.id)).output_style == "Learning"


# Item 4 of fix round 1: a stored or default name the CLI does not list falls back.
@pytest.mark.asyncio
async def test_a_stored_style_the_cli_no_longer_lists_spawns_as_default(env, caplog) -> None:
    caplog.set_level(logging.INFO)
    env.rec.output_style = "Gone"
    assert env.orch._spawn_flag_settings(env.rec.id) == {"outputStyle": "default"}
    assert any("output_style fallback" in r.getMessage() for r in caplog.records)
    env.rec.output_styles = None                  # unknown list: passed on trust
    assert env.orch._spawn_flag_settings(env.rec.id) == {"outputStyle": "Gone"}


@pytest.mark.asyncio
async def test_an_app_default_the_cli_does_not_list_spawns_nothing(env) -> None:
    await _set_app_default("Gone")
    await env.orch.refresh_default_output_style()
    assert env.orch._spawn_flag_settings(env.rec.id) == {}


@pytest.mark.asyncio
async def test_a_reset_to_an_unlisted_app_default_applies_default(env) -> None:
    await _set_app_default("Gone")
    assert await env.orch.set_output_style(env.rec.id, None) == {"style": None}
    assert env.control.patches[-1] == {"outputStyle": "default"}
    assert _payload(env.rec)["outputStyle"] == "default"


@pytest.mark.asyncio
async def test_the_payload_never_serves_an_unlisted_stored_style(env) -> None:
    env.rec.output_style, env.rec.output_style_live = "Gone", None
    assert _payload(env.rec)["outputStyle"] == "default"
    env.rec.output_styles = None
    assert _payload(env.rec)["outputStyle"] == "Gone"


@pytest.mark.asyncio
async def test_a_restart_spawn_on_an_unlisted_style_is_corrected_live(env) -> None:
    """After a restart the list is unknown, so the spawn passes the stored name on
    trust; once the CLI lists its styles without it, the driver applies `default`."""
    info = {**INFO, "output_style": "Gone"}
    env.rec.output_style, env.rec.output_styles = "Gone", None
    sid = env.rec.id
    spawns = Spawns(FakeControl(), info)
    await env.orch.stop()
    orch2 = SessionOrchestrator(sdk_factory=spawns, poll_interval=9999.0,
                                usage_fn=_fake_usage, repo_root=str(env.repo_root))
    try:
        async with db_session() as db:
            from sqlalchemy import update
            await db.execute(update(DbSession).where(DbSession.id == sid).values(
                output_style="Gone"))
        await orch2.rehydrate()
        assert await orch2.ensure_driver(sid) is not None
        assert spawns.calls[-1]["flag_settings"] == {"outputStyle": "Gone"}
        assert spawns.control.patches == [{"outputStyle": "default"}]
        assert orch2._records[sid].output_style_live == "default"
    finally:
        await orch2.stop()


# Item 7 of fix round 1: `/clear` respawns with a fresh app default.
@pytest.mark.asyncio
async def test_clear_respawns_with_the_current_app_default(env) -> None:
    assert env.orch._default_output_style is None
    await _set_app_default("Concise")              # changed in Settings after the spawn
    out = await env.orch.run_command(env.rec.id, "clear", "")
    assert out is not None and out["render"] == "clear"
    assert env.spawns.calls[-1]["flag_settings"] == {"outputStyle": "Concise"}


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
    client = _RecordingClient(result={"style": "Concise"})
    assert await RemoteOrchestrator(client).set_output_style(
        "s", "Concise", identity="u") == {"style": "Concise"}
    assert client.calls == [("set_output_style", {"session_id": "s", "style": "Concise",
                                                  "identity": "u"})]
    for code in (fs.OUTPUT_STYLE_UNKNOWN, fs.OUTPUT_STYLE_UNAVAILABLE):
        with pytest.raises(fs.SettingsError) as ei:
            await RemoteOrchestrator(_RecordingClient(error=DaemonError(
                "SettingsError", "why", {"code": code}))).set_output_style("s", "x")
        assert ei.value.code == code
    with pytest.raises(KeyError):
        await RemoteOrchestrator(_RecordingClient(
            error=DaemonError("KeyError", "s"))).set_output_style("s", None)
    with pytest.raises(DaemonError) as old:
        await RemoteOrchestrator(_RecordingClient(error=DaemonError(
            "UnknownMethod", "set_output_style"))).set_output_style("s", None)
    assert old.value.err_type == "UnknownMethod"


@pytest.mark.asyncio
async def test_uds_round_trip_on_the_owner_daemon(env, caplog) -> None:
    caplog.set_level(logging.INFO, logger="server.ipc.daemon")
    app = create_daemon_app()
    register_orchestrator_rpc(app, env.orch)
    async with serve_daemon_uds(app) as sock:
        client = DaemonClient(sock)
        try:
            ro = RemoteOrchestrator(client)
            with pytest.raises(fs.SettingsError) as refused:
                await ro.set_output_style(env.rec.id, "NoSuchStyle")
            assert await ro.set_output_style(env.rec.id, "Concise") == {"style": "Concise"}
        finally:
            await client.aclose()
    assert refused.value.code == fs.OUTPUT_STYLE_UNKNOWN
    assert env.control.patches == [{"outputStyle": "Concise"}]
    rpc_errors = [r for r in caplog.records
                  if r.name == "server.ipc.daemon" and "rpc.error" in r.getMessage()]
    assert rpc_errors and all(r.levelno == logging.INFO for r in rpc_errors)


@pytest.mark.asyncio
async def test_fleet_routes_to_the_owning_host_and_facade_forwards() -> None:
    assert "set_output_style" in _PER_SESSION

    class _Host:
        def __init__(self, name):
            self.name, self.calls = name, []

        async def set_output_style(self, session_id, style, **kw):
            self.calls.append((session_id, style, kw))
            return {"style": style, "host": self.name}

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
    assert (await fo.set_output_style("s", "Concise", identity="u"))["host"] == "rocky"
    assert dobby.calls == []
    adapter = InProcessOrchestratorAdapter(dobby)  # type: ignore[arg-type]
    assert (await adapter.set_output_style("s", None))["host"] == "dobby"


def test_the_orchestrator_method_signatures_agree() -> None:
    own = inspect.signature(SessionOrchestrator.set_output_style).parameters
    proxy = inspect.signature(RemoteOrchestrator.set_output_style).parameters
    assert list(own) == list(proxy)


# ---------------------------------------------------------------- HTTP

_ENV = {"VIBERPIT_DB_URL": "sqlite+aiosqlite:///:memory:", "VIBERPIT_DEV": "1",
        "VIBERPIT_TAILNET_ALLOWLIST": ""}
SID = "66666666-6666-4666-8666-666666666666"


def _client(styles: list[str] | None = None):
    with patch.dict(os.environ, _ENV):
        orch = SessionOrchestrator(sdk_factory=Spawns(FakeControl()), poll_interval=9999.0,
                                   usage_fn=lambda name: None)
        app = create_app(orchestrator=orch)
    orch._records[SID] = SessionRecord(id=SID, name="n", account="a",
                                       engine=AgentEngine.CLAUDE_CODE, engine_session_id="c",
                                       cols=80, rows=24, cwd=None,  # type: ignore[arg-type]
                                       output_styles=styles)
    return TestClient(app, raise_server_exceptions=False)


def _post(c, body, sid=SID):
    return c.post(f"/api/sessions/{sid}/output-style", json=body, headers=_H)


def test_http_output_style_is_200_with_the_style() -> None:
    async def fake(self, session_id, style, *, identity="system"):
        assert (session_id, style, identity) == (SID, "Concise", "testuser@example.com")
        return {"style": "Concise"}

    with patch.dict(os.environ, _ENV), _client() as c, \
            patch.object(SessionOrchestrator, "set_output_style", fake):
        r = _post(c, {"style": "Concise"})
    assert r.status_code == 200 and r.json() == {"style": "Concise"}


def test_http_null_resets() -> None:
    seen: list = []

    async def fake(self, session_id, style, *, identity="system"):
        seen.append(style)
        return {"style": None}

    with patch.dict(os.environ, _ENV), _client() as c, \
            patch.object(SessionOrchestrator, "set_output_style", fake):
        r = _post(c, {"style": None})
    assert r.status_code == 200 and r.json() == {"style": None} and seen == [None]


def test_http_unavailable_is_409() -> None:
    with patch.dict(os.environ, _ENV), _client() as c:
        r = _post(c, {"style": "Concise"})          # no style list cached
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == fs.OUTPUT_STYLE_UNAVAILABLE


def test_http_unknown_is_409() -> None:
    with patch.dict(os.environ, _ENV), _client(STYLES) as c:
        r = _post(c, {"style": "NoSuchStyle"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == fs.OUTPUT_STYLE_UNKNOWN


def test_http_unknown_session_old_daemon_and_bad_body() -> None:
    with patch.dict(os.environ, _ENV), _client() as c:
        assert _post(c, {"style": None}, sid="nope").status_code == 404
        assert _post(c, {"style": 5}).status_code == 422
        assert _post(c, {}).status_code == 422
        with patch.object(SessionOrchestrator, "set_output_style",
                          side_effect=DaemonError("UnknownMethod", "set_output_style")):
            r = _post(c, {"style": "Concise"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "daemon_outdated"


def test_http_requires_identity() -> None:
    with patch.dict(os.environ, _ENV), _client() as c:
        assert c.post(f"/api/sessions/{SID}/output-style",
                      json={"style": None}).status_code == 401


# ---------------------------------------------------------------- Settings default


@pytest.fixture
def settings_client(tmp_path: Path):
    shared = tmp_path / "dot-claude"
    styles = shared / "output-styles"
    styles.mkdir(parents=True)
    (styles / "terse.md").write_text(
        "---\nname: Terse\ndescription: Short answers\n---\nBe brief.\n")
    (styles / "plain.md").write_text("No frontmatter.\n")
    (styles / "notes.txt").write_text("ignored\n")
    env = {**_ENV, "CLAUDE_CONFIG_DIR": str(shared)}
    with patch.dict(os.environ, env):
        app = create_app(orchestrator=SessionOrchestrator(
            sdk_factory=Spawns(FakeControl()), poll_interval=9999.0,
            usage_fn=lambda name: None))
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c


def test_settings_lists_the_builtins_and_the_shared_user_styles(settings_client) -> None:
    r = settings_client.get("/api/settings/output-styles", headers=_H)
    assert r.status_code == 200
    rows = r.json()["styles"]
    assert [s["name"] for s in rows] == BUILTINS + ["plain", "Terse"]
    assert all(s["source"] == "builtin" and "path" not in s for s in rows[:5])
    terse = rows[-1]
    assert terse == {"name": "Terse", "source": "user", "description": "Short answers",
                     "path": "terse.md"}
    assert rows[-2]["description"] is None
    one = settings_client.get("/api/settings/output-styles/terse.md", headers=_H)
    assert one.status_code == 200
    assert one.json() == {"name": "Terse", "path": "terse.md",
                          "content": "---\nname: Terse\ndescription: Short answers\n---\nBe brief.\n"}
    assert settings_client.get("/api/settings/output-styles/nope.md",
                               headers=_H).status_code == 404
    assert settings_client.get("/api/settings/output-styles/..%2Fsettings.json",
                               headers=_H).status_code == 404


def test_settings_default_output_style_round_trips_and_validates(settings_client) -> None:
    c = settings_client
    assert c.get("/api/settings", headers=_H).json()["defaultOutputStyle"] is None
    r = c.put("/api/settings", json={"defaultOutputStyle": "Concise"}, headers=_H)
    assert r.status_code == 200 and r.json()["defaultOutputStyle"] == "Concise"
    r = c.put("/api/settings", json={"defaultOutputStyle": "Terse"}, headers=_H)
    assert r.status_code == 200 and r.json()["defaultOutputStyle"] == "Terse"
    bad = c.put("/api/settings", json={"defaultOutputStyle": "hello:Pirate"}, headers=_H)
    assert bad.status_code == 422
    assert c.get("/api/settings", headers=_H).json()["defaultOutputStyle"] == "Terse"
    r = c.put("/api/settings", json={"thinkingCollapsed": False}, headers=_H)
    assert r.json()["defaultOutputStyle"] == "Terse"          # omitted: unchanged
    r = c.put("/api/settings", json={"defaultOutputStyle": None}, headers=_H)
    assert r.status_code == 200 and r.json()["defaultOutputStyle"] is None


@pytest.mark.parametrize("text,gate,expected", [
    ("/fast on", None, True),                  # a person's app command
    ("/fast on", "local-process", False),      # an unattested local process
    ("hello", None, False),                    # not an app command: never sent
])
def test_post_sessions_marks_a_persons_initial_app_command(text, gate, expected) -> None:
    seen: list = []

    async def fake_create(self, *a, **k):
        seen.append(k.get("initial_prompt_human", False))
        raise KeyError("stop here")

    with patch.dict(os.environ, _ENV), _client() as c, \
            patch.object(SessionOrchestrator, "create_session", fake_create), \
            patch("server.api.sessions.human_gate_via", return_value=gate):
        c.post("/api/sessions", json={"name": "x", "initialPrompt": text}, headers=_H)
    assert seen == [expected]


@pytest.mark.asyncio
async def test_remote_create_strips_the_flag_for_an_old_daemon() -> None:
    calls: list = []

    class _Old:
        async def call(self, method, *, timeout=None, **kw):
            calls.append(kw)
            if "initial_prompt_human" in kw:
                raise DaemonError("TypeError", "create_session() got an unexpected keyword "
                                  "argument 'initial_prompt_human'")
            return {"session": SessionRecord(
                id="s", name="n", account="a", engine=AgentEngine.CLAUDE_CODE,
                engine_session_id=None, cols=80, rows=24, cwd=None).to_dict()}

    rec = await RemoteOrchestrator(_Old()).create_session(  # type: ignore[arg-type]
        name="n", initial_prompt="/fast on", initial_prompt_human=True)
    assert rec.id == "s" and "initial_prompt_human" not in calls[-1]
