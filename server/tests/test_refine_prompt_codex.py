"""`SessionOrchestrator.refine_prompt_codex` — native Codex prompt refinement (#1288).

Uses the lightweight fake-driver/fake-registry pattern from test_codex_orchestrator.py
rather than the full JSONL FakeCodexAppServer: this is an orchestration-level surface
(attach/detach lifecycle, event draining, timeout, fail-open), not a wire-framing one.
"""
from __future__ import annotations

from pathlib import Path
from typing import AsyncIterator

import pytest

from server.sessions import events as ev
from server.sessions.engine import AgentDriverRegistry, AgentEngine
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SYSTEM_IDENTITY

ACCOUNT = "openai-main"


class _FakeCodexManager:
    def __init__(self) -> None:
        self.ensured: list[str] = []
        self.attached: list[tuple[str, str]] = []
        self.detached: list[str] = []

    async def ensure(self, account: str) -> None:
        self.ensured.append(account)

    def attach(self, account: str, session_id: str) -> None:
        self.attached.append((account, session_id))

    def detach(self, session_id: str) -> None:
        self.detached.append(session_id)


class _FakeCodexHelperDriver:
    """Minimal double for the surface `refine_prompt_codex` actually calls."""

    def __init__(self, config, *, events: list[object], fail_start: bool = False) -> None:
        self.config = config
        self.session_id = config.session_id
        self.account = config.account
        self._events = events
        self._fail_start = fail_start
        self.started = 0
        self.archived = 0
        self.sent: list[str] = []
        self.resolved: list[tuple[str, str, str]] = []

    async def start(self) -> None:
        self.started += 1
        if self._fail_start:
            raise RuntimeError("app-server refused the thread")

    async def send_prompt(self, text: str, *_a, **_k) -> None:
        self.sent.append(text)

    async def events(self) -> AsyncIterator[object]:
        for event in self._events:
            yield event

    def resolve_permission(self, perm_id: str, decision: str, identity: str) -> bool:
        self.resolved.append((perm_id, decision, identity))
        return True

    async def archive(self) -> None:
        self.archived += 1


class _FakeCodexFactory:
    def __init__(self, *, events: list[object], fail_start: bool = False) -> None:
        self.capabilities = None
        self.created: list[_FakeCodexHelperDriver] = []
        self._events = events
        self._fail_start = fail_start

    def create(self, config):
        driver = _FakeCodexHelperDriver(config, events=self._events, fail_start=self._fail_start)
        self.created.append(driver)
        return driver


class _HungCodexHelperDriver(_FakeCodexHelperDriver):
    """Never yields a TurnEnd — models a wedged/slow turn for the timeout test."""

    async def events(self) -> AsyncIterator[object]:
        import asyncio
        await asyncio.sleep(3600)
        yield ev.TurnEnd(turn_id="t1")  # pragma: no cover — unreachable


def _orch(tmp_path: Path, *, codex, factory: _FakeCodexFactory) -> SessionOrchestrator:
    repo_root = tmp_path / "repos"
    repo_root.mkdir(exist_ok=True)
    registry = AgentDriverRegistry({AgentEngine.CODEX: factory})
    return SessionOrchestrator(
        sdk_factory=lambda *a, **k: None, codex=codex,
        driver_registry=registry, repo_root=str(repo_root),
    )


@pytest.mark.asyncio
async def test_disabled_by_default_never_touches_the_manager(tmp_path, monkeypatch):
    monkeypatch.delenv("CCSWITCH_CODEX_REFINE_ENABLED", raising=False)
    codex = _FakeCodexManager()
    factory = _FakeCodexFactory(events=[ev.TextDelta(turn_id="t1", text="hi"),
                                         ev.TurnEnd(turn_id="t1")])
    orch = _orch(tmp_path, codex=codex, factory=factory)

    result = await orch.refine_prompt_codex(ACCOUNT, "fix the bug")

    assert result is None
    assert codex.ensured == []
    assert factory.created == []


@pytest.mark.asyncio
async def test_no_codex_manager_wired_is_none_even_when_flag_is_on(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_CODEX_REFINE_ENABLED", "true")
    factory = _FakeCodexFactory(events=[ev.TurnEnd(turn_id="t1")])
    orch = _orch(tmp_path, codex=None, factory=factory)

    assert await orch.refine_prompt_codex(ACCOUNT, "fix the bug") is None
    assert factory.created == []


@pytest.mark.asyncio
async def test_happy_path_returns_joined_text_and_tears_down_cleanly(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_CODEX_REFINE_ENABLED", "true")
    codex = _FakeCodexManager()
    factory = _FakeCodexFactory(events=[
        ev.TextDelta(turn_id="t1", text="Rewrite the "),
        ev.TextDelta(turn_id="t1", text="draft prompt."),
        ev.TurnEnd(turn_id="t1", outcome="success"),
    ])
    orch = _orch(tmp_path, codex=codex, factory=factory)

    result = await orch.refine_prompt_codex(ACCOUNT, "fix the bug")

    assert result == "Rewrite the draft prompt."
    assert codex.ensured == [ACCOUNT]
    driver = factory.created[0]
    assert driver.started == 1
    assert driver.archived == 1
    assert "fix the bug" in driver.sent[0]
    assert codex.attached == [(ACCOUNT, driver.session_id)]
    assert codex.detached == [driver.session_id]


@pytest.mark.asyncio
async def test_permission_requests_are_auto_denied(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_CODEX_REFINE_ENABLED", "true")
    codex = _FakeCodexManager()
    factory = _FakeCodexFactory(events=[
        ev.PermissionRequest(perm_id="p1", turn_id="t1", tool="shell",
                              kind=ev.ToolKind.BASH, input={}),
        ev.TextDelta(turn_id="t1", text="ok"),
        ev.TurnEnd(turn_id="t1", outcome="success"),
    ])
    orch = _orch(tmp_path, codex=codex, factory=factory)

    result = await orch.refine_prompt_codex(ACCOUNT, "fix the bug")

    assert result == "ok"
    driver = factory.created[0]
    assert driver.resolved == [("p1", "deny", SYSTEM_IDENTITY)]


@pytest.mark.asyncio
async def test_non_success_turn_outcome_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_CODEX_REFINE_ENABLED", "true")
    codex = _FakeCodexManager()
    factory = _FakeCodexFactory(events=[
        ev.TextDelta(turn_id="t1", text="partial"),
        ev.TurnEnd(turn_id="t1", outcome="interrupted"),
    ])
    orch = _orch(tmp_path, codex=codex, factory=factory)

    assert await orch.refine_prompt_codex(ACCOUNT, "fix the bug") is None
    # Cleanup still ran despite the non-success outcome.
    driver = factory.created[0]
    assert driver.archived == 1
    assert codex.detached == [driver.session_id]


@pytest.mark.asyncio
async def test_start_failure_is_fail_open_and_never_attaches(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_CODEX_REFINE_ENABLED", "true")
    codex = _FakeCodexManager()
    factory = _FakeCodexFactory(events=[], fail_start=True)
    orch = _orch(tmp_path, codex=codex, factory=factory)

    assert await orch.refine_prompt_codex(ACCOUNT, "fix the bug") is None
    assert codex.attached == []
    assert codex.detached == []
    driver = factory.created[0]
    assert driver.archived == 1


@pytest.mark.asyncio
async def test_timeout_is_fail_open_and_still_tears_down(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_CODEX_REFINE_ENABLED", "true")
    codex = _FakeCodexManager()

    class _HungFactory(_FakeCodexFactory):
        def create(self, config):
            driver = _HungCodexHelperDriver(config, events=[])
            self.created.append(driver)
            return driver

    factory = _HungFactory(events=[])
    orch = _orch(tmp_path, codex=codex, factory=factory)

    result = await orch.refine_prompt_codex(ACCOUNT, "fix the bug", timeout_s=0.05)

    assert result is None
    driver = factory.created[0]
    assert driver.archived == 1
    assert codex.detached == [driver.session_id]
