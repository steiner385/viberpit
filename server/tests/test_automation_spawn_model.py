"""Spawn-site model wiring: backlog-runner and pr-doctor resolve default_model
config into the ``model=`` create kwarg (and stamp model/model_source into the
spawned-event detail). The assessor itself is covered in test_model_pick.py."""
from __future__ import annotations

import os
import types
from unittest.mock import patch

import pytest

from server.automations.backlog_selection import Batch
from server.github.pr_status import PullRequestInfo


def _capture_spawn(captured):
    async def fake_spawn(**kw):
        captured.update(kw)
        return types.SimpleNamespace(id="new-sess")
    return fake_spawn


# --------------------------------------------------------------------------- #
# backlog-runner (_backlog_spawn)                                              #
# --------------------------------------------------------------------------- #

def _build_backlog_orch(config):
    import server.sessions.orchestrator as orch_mod
    from server.automations.context import AutomationContext
    orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    orch._backlog_ctx = AutomationContext("backlog-runner")

    async def read_config():
        return "auto", config
    orch._backlog_read_config = read_config  # type: ignore[method-assign]
    return orch


def _patch_git(monkeypatch, tmp_path):
    import server.sessions.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod.gitrepos, "ensure_clone",
                        lambda root, repo, url: str(tmp_path))
    monkeypatch.setattr(orch_mod.gitrepos, "list_remote_branches",
                        lambda repo: {"default": "master"})


_BATCH = Batch(primary="o/r#7", issues=["o/r#7"], repo="o/r",
               titles={"o/r#7": "fix the frobnicator"})


@pytest.mark.asyncio
async def test_backlog_spawn_passes_pinned_model(tmp_path, monkeypatch):
    orch = _build_backlog_orch({"default_model": "sonnet"})
    _patch_git(monkeypatch, tmp_path)
    captured: dict = {}
    orch._backlog_ctx.spawn_session = _capture_spawn(captured)  # type: ignore[method-assign]
    await orch._backlog_spawn(_BATCH, "acct")
    assert captured["model"] == "sonnet"
    # `engine` joins the detail in Phase 7: a spawn record has to say which agent
    # ran it, or a mixed-engine backlog is unreadable after the fact.
    assert captured["detail"] == {"issues": ["o/r#7"], "model": "sonnet",
                                  "model_source": "pinned", "engine": "claude_code"}


@pytest.mark.asyncio
async def test_backlog_spawn_auto_assesses_issue_titles(tmp_path, monkeypatch):
    import server.automations.model_pick as model_pick
    seen: dict = {}

    async def pick_haiku(task_summary, **kw):
        seen["summary"] = task_summary
        return "haiku"
    monkeypatch.setattr(model_pick, "assess_model", pick_haiku)

    orch = _build_backlog_orch({"default_model": "auto"})
    _patch_git(monkeypatch, tmp_path)
    captured: dict = {}
    orch._backlog_ctx.spawn_session = _capture_spawn(captured)  # type: ignore[method-assign]
    await orch._backlog_spawn(_BATCH, "acct")
    assert "fix the frobnicator" in seen["summary"]  # titles reach the assessor
    assert captured["model"] == "haiku"
    assert captured["detail"]["model_source"] == "auto"


@pytest.mark.asyncio
async def test_backlog_spawn_default_keeps_account_default(tmp_path, monkeypatch):
    orch = _build_backlog_orch({"default_model": "default"})
    _patch_git(monkeypatch, tmp_path)
    captured: dict = {}
    orch._backlog_ctx.spawn_session = _capture_spawn(captured)  # type: ignore[method-assign]
    await orch._backlog_spawn(_BATCH, "acct")
    assert captured["model"] is None
    # No model keys — but the engine is always recorded (Phase 7).
    assert captured["detail"] == {"issues": ["o/r#7"], "engine": "claude_code"}


# --------------------------------------------------------------------------- #
# backlog-runner Codex path (issue #1289)                                     #
# --------------------------------------------------------------------------- #

def _fake_launch_plan_codex(config):
    from server.sessions.engine import AgentEngine
    async def plan(origin_ref):
        return AgentEngine.CODEX, config
    return plan


@pytest.mark.asyncio
async def test_backlog_spawn_codex_auto_uses_codex_assessor(tmp_path, monkeypatch):
    orch = _build_backlog_orch({"default_model": "auto"})
    _patch_git(monkeypatch, tmp_path)
    orch._backlog_ctx.launch_plan_or_refuse = _fake_launch_plan_codex(
        {"default_model": "auto"})

    seen: dict = {}
    async def fake_codex_effort(account, config, task_summary):
        seen["account"] = account
        seen["summary"] = task_summary
        return "high", "auto"
    orch._resolve_codex_auto_effort = fake_codex_effort  # type: ignore[method-assign]

    captured: dict = {}
    orch._backlog_ctx.spawn_session = _capture_spawn(captured)  # type: ignore[method-assign]
    await orch._backlog_spawn(_BATCH, "acct")

    assert seen["account"] == "acct"
    assert "fix the frobnicator" in seen["summary"]
    # A Codex spawn must never carry a Claude alias in `model=` — only the
    # live-catalog reasoning effort.
    assert captured["model"] is None
    assert captured["reasoning_effort"] == "high"
    assert captured["detail"] == {
        "issues": ["o/r#7"], "engine": "codex",
        "reasoning_effort": "high", "model_source": "auto",
    }


@pytest.mark.asyncio
async def test_backlog_spawn_codex_assessment_failure_falls_back(tmp_path, monkeypatch):
    orch = _build_backlog_orch({"default_model": "auto"})
    _patch_git(monkeypatch, tmp_path)
    orch._backlog_ctx.launch_plan_or_refuse = _fake_launch_plan_codex(
        {"default_model": "auto"})

    async def fake_codex_effort(account, config, task_summary):
        return None, None
    orch._resolve_codex_auto_effort = fake_codex_effort  # type: ignore[method-assign]

    captured: dict = {}
    orch._backlog_ctx.spawn_session = _capture_spawn(captured)  # type: ignore[method-assign]
    await orch._backlog_spawn(_BATCH, "acct")

    assert captured["model"] is None
    assert captured["reasoning_effort"] is None
    assert captured["detail"] == {"issues": ["o/r#7"], "engine": "codex"}


# --------------------------------------------------------------------------- #
# SessionOrchestrator._resolve_codex_auto_effort (issue #1289)                #
# --------------------------------------------------------------------------- #

class _FakeCodexOption:
    def __init__(self, id_, default=False, reasoning_efforts=()):
        self.id = id_
        self.default = default
        self.reasoning_efforts = reasoning_efforts


class _FakeCodexAvailability:
    cli_version = "1.0.0"


class _FakeCodexManager:
    available = True
    availability = _FakeCodexAvailability()

    async def ensure(self, account):
        pass

    def client_for(self, account):
        return object()


class _FakeCodexCatalog:
    def __init__(self, options):
        self._options = options

    async def models(self, account, *, client, cli_version, refresh=False):
        return self._options


def _bare_orchestrator():
    import server.sessions.orchestrator as orch_mod
    return orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)


@pytest.mark.asyncio
async def test_resolve_codex_auto_effort_uses_pinned_model_catalog(monkeypatch):
    import server.automations.model_pick as model_pick
    orch = _bare_orchestrator()
    orch._codex = _FakeCodexManager()
    orch._codex_model_catalog = _FakeCodexCatalog((
        _FakeCodexOption("gpt-5.1-codex-mini", default=True,
                         reasoning_efforts=("minimal", "low")),
        _FakeCodexOption("gpt-5.1-codex", reasoning_efforts=("low", "medium", "high")),
    ))
    seen: dict = {}
    async def fake_resolve(config, task_summary, efforts):
        seen["efforts"] = efforts
        return "high", "auto"
    monkeypatch.setattr(model_pick, "resolve_codex_model", fake_resolve)

    got = await orch._resolve_codex_auto_effort(
        "acct", {"default_model": "auto", "codex_model": "gpt-5.1-codex"}, "hard job")
    assert seen["efforts"] == ("low", "medium", "high")
    assert got == ("high", "auto")


@pytest.mark.asyncio
async def test_resolve_codex_auto_effort_falls_back_to_catalog_default(monkeypatch):
    import server.automations.model_pick as model_pick
    orch = _bare_orchestrator()
    orch._codex = _FakeCodexManager()
    orch._codex_model_catalog = _FakeCodexCatalog((
        _FakeCodexOption("gpt-5.1-codex-mini", default=True,
                         reasoning_efforts=("minimal", "low")),
    ))
    seen: dict = {}
    async def fake_resolve(config, task_summary, efforts):
        seen["efforts"] = efforts
        return "minimal", "auto"
    monkeypatch.setattr(model_pick, "resolve_codex_model", fake_resolve)

    # No codex_model pin → falls back to the catalog's own default option.
    got = await orch._resolve_codex_auto_effort(
        "acct", {"default_model": "auto"}, "trivial job")
    assert seen["efforts"] == ("minimal", "low")
    assert got == ("minimal", "auto")


@pytest.mark.asyncio
async def test_resolve_codex_auto_effort_skips_when_not_auto():
    orch = _bare_orchestrator()
    orch._codex = _FakeCodexManager()  # never touched — short-circuits first
    got = await orch._resolve_codex_auto_effort(
        "acct", {"default_model": "default"}, "job")
    assert got == (None, None)


@pytest.mark.asyncio
async def test_resolve_codex_auto_effort_fails_open_without_codex_manager():
    orch = _bare_orchestrator()
    orch._codex = None
    got = await orch._resolve_codex_auto_effort(
        "acct", {"default_model": "auto"}, "job")
    assert got == (None, None)


@pytest.mark.asyncio
async def test_resolve_codex_auto_effort_fails_open_on_catalog_error():
    from server.codex.catalog import ModelCatalogError

    class _BoomCatalog:
        async def models(self, *a, **kw):
            raise ModelCatalogError("app-server unreachable")

    orch = _bare_orchestrator()
    orch._codex = _FakeCodexManager()
    orch._codex_model_catalog = _BoomCatalog()
    got = await orch._resolve_codex_auto_effort(
        "acct", {"default_model": "auto"}, "job")
    assert got == (None, None)


# --------------------------------------------------------------------------- #
# pr-doctor (_pr_doctor_session_fix fresh-spawn branch)                        #
# --------------------------------------------------------------------------- #

def _full_orch():
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory
    with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
                                 "CCSWITCH_DEV": "1"}):
        return SessionOrchestrator(sdk_factory=fake_sdk_factory())


_INFO = PullRequestInfo(number=11, url="https://x/11", state="open",
                        checks="failing", title="red build", repo="o/r",
                        head_ref="fix-lint")


@pytest.mark.asyncio
async def test_pr_fix_spawn_passes_pinned_model():
    orch = _full_orch()

    async def config():
        return {"default_model": "opus"}
    captured: dict = {}
    with patch("server.sessions.orchestrator.gitrepos.ensure_clone",
               return_value="/tmp/x"), \
         patch.object(orch, "_pr_doctor_config", config), \
         patch.object(orch._pr_doctor_ctx, "spawn_session", _capture_spawn(captured)):
        status, sid = await orch._pr_doctor_session_fix(
            "o/r", "fix-lint", _INFO, "failing_ci", None)
    assert (status, sid) == ("spawned", "new-sess")
    assert captured["model"] == "opus"
    assert captured["detail"] == {"model": "opus", "model_source": "pinned"}


@pytest.mark.asyncio
async def test_pr_fix_spawn_auto_fails_open_without_model_access():
    # Default config resolves to "auto"; with no reachable assessor (the
    # conftest no-real-model guard = production's SDK-unavailable degraded
    # mode) the spawn proceeds on the account default instead of failing.
    orch = _full_orch()
    captured: dict = {}
    with patch("server.sessions.orchestrator.gitrepos.ensure_clone",
               return_value="/tmp/x"), \
         patch.object(orch._pr_doctor_ctx, "spawn_session", _capture_spawn(captured)):
        status, _ = await orch._pr_doctor_session_fix(
            "o/r", "fix-lint", _INFO, "failing_ci", None)
    assert status == "spawned"
    assert captured["model"] is None
    assert captured["detail"] is None
