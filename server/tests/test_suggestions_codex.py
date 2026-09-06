"""Native-Codex follow-up suggestions (#1281).

Two layers are covered:

1. ``SessionOrchestrator.generate_suggestions_codex`` — the one-shot throwaway
   Codex thread, using the same lightweight fake-driver/fake-registry pattern as
   ``test_refine_prompt_codex.py`` (this is an orchestration-level surface:
   attach/detach lifecycle, event draining, timeout, fail-open — not a
   wire-framing one).
2. The engine-routing in ``latest_suggestions``/``generate_suggestions_now``:
   a Codex session (live-idle or detached) must be dispatched to the Codex-native
   path instead of falling through into the Claude-only transcript/auth code
   that silently returned [] before this fix.
"""
from __future__ import annotations

import asyncio
import types
from pathlib import Path
from typing import AsyncIterator
from unittest.mock import AsyncMock

import pytest

import server.sessions.orchestrator as orch_mod
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
    """Minimal double for the surface `generate_suggestions_codex` actually calls."""

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


CONTEXT = "User: fix the bug\n\nAssistant: done, ran the tests."


# --- generate_suggestions_codex -------------------------------------------

@pytest.mark.asyncio
async def test_disabled_by_default_never_touches_the_manager(tmp_path, monkeypatch):
    monkeypatch.delenv("CCSWITCH_CODEX_SUGGEST_ENABLED", raising=False)
    codex = _FakeCodexManager()
    factory = _FakeCodexFactory(events=[ev.TextDelta(turn_id="t1", text='["Run tests"]'),
                                         ev.TurnEnd(turn_id="t1")])
    orch = _orch(tmp_path, codex=codex, factory=factory)

    result = await orch.generate_suggestions_codex(ACCOUNT, CONTEXT)

    assert result == []
    assert codex.ensured == []
    assert factory.created == []


@pytest.mark.asyncio
async def test_no_codex_manager_wired_is_empty_even_when_flag_is_on(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_CODEX_SUGGEST_ENABLED", "true")
    factory = _FakeCodexFactory(events=[ev.TurnEnd(turn_id="t1")])
    orch = _orch(tmp_path, codex=None, factory=factory)

    assert await orch.generate_suggestions_codex(ACCOUNT, CONTEXT) == []
    assert factory.created == []


@pytest.mark.asyncio
async def test_blank_context_never_spawns_a_thread(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_CODEX_SUGGEST_ENABLED", "true")
    codex = _FakeCodexManager()
    factory = _FakeCodexFactory(events=[ev.TurnEnd(turn_id="t1")])
    orch = _orch(tmp_path, codex=codex, factory=factory)

    assert await orch.generate_suggestions_codex(ACCOUNT, "   ") == []
    assert factory.created == []


@pytest.mark.asyncio
async def test_happy_path_parses_suggestions_and_tears_down_cleanly(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_CODEX_SUGGEST_ENABLED", "true")
    codex = _FakeCodexManager()
    factory = _FakeCodexFactory(events=[
        ev.TextDelta(turn_id="t1", text='["Run the tests", '),
        ev.TextDelta(turn_id="t1", text='"Commit it"]'),
        ev.TurnEnd(turn_id="t1", outcome="success"),
    ])
    orch = _orch(tmp_path, codex=codex, factory=factory)

    result = await orch.generate_suggestions_codex(ACCOUNT, CONTEXT)

    assert result == ["Run the tests", "Commit it"]
    assert codex.ensured == [ACCOUNT]
    driver = factory.created[0]
    assert driver.started == 1
    assert driver.archived == 1
    assert "fix the bug" in driver.sent[0]
    assert codex.attached == [(ACCOUNT, driver.session_id)]
    assert codex.detached == [driver.session_id]


@pytest.mark.asyncio
async def test_permission_requests_are_auto_denied(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_CODEX_SUGGEST_ENABLED", "true")
    codex = _FakeCodexManager()
    factory = _FakeCodexFactory(events=[
        ev.PermissionRequest(perm_id="p1", turn_id="t1", tool="shell",
                              kind=ev.ToolKind.BASH, input={}),
        ev.TextDelta(turn_id="t1", text='["ok"]'),
        ev.TurnEnd(turn_id="t1", outcome="success"),
    ])
    orch = _orch(tmp_path, codex=codex, factory=factory)

    result = await orch.generate_suggestions_codex(ACCOUNT, CONTEXT)

    assert result == ["ok"]
    driver = factory.created[0]
    assert driver.resolved == [("p1", "deny", SYSTEM_IDENTITY)]


@pytest.mark.asyncio
async def test_non_success_turn_outcome_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_CODEX_SUGGEST_ENABLED", "true")
    codex = _FakeCodexManager()
    factory = _FakeCodexFactory(events=[
        ev.TextDelta(turn_id="t1", text="partial"),
        ev.TurnEnd(turn_id="t1", outcome="interrupted"),
    ])
    orch = _orch(tmp_path, codex=codex, factory=factory)

    assert await orch.generate_suggestions_codex(ACCOUNT, CONTEXT) == []
    driver = factory.created[0]
    assert driver.archived == 1
    assert codex.detached == [driver.session_id]


@pytest.mark.asyncio
async def test_unparseable_reply_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_CODEX_SUGGEST_ENABLED", "true")
    codex = _FakeCodexManager()
    factory = _FakeCodexFactory(events=[
        ev.TextDelta(turn_id="t1", text="not json"),
        ev.TurnEnd(turn_id="t1", outcome="success"),
    ])
    orch = _orch(tmp_path, codex=codex, factory=factory)

    assert await orch.generate_suggestions_codex(ACCOUNT, CONTEXT) == []


@pytest.mark.asyncio
async def test_start_failure_is_fail_open_and_never_attaches(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_CODEX_SUGGEST_ENABLED", "true")
    codex = _FakeCodexManager()
    factory = _FakeCodexFactory(events=[], fail_start=True)
    orch = _orch(tmp_path, codex=codex, factory=factory)

    assert await orch.generate_suggestions_codex(ACCOUNT, CONTEXT) == []
    assert codex.attached == []
    assert codex.detached == []
    driver = factory.created[0]
    assert driver.archived == 1


@pytest.mark.asyncio
async def test_timeout_is_fail_open_and_still_tears_down(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_CODEX_SUGGEST_ENABLED", "true")
    monkeypatch.setenv("CCSWITCH_SUGGEST_CODEX_TIMEOUT_S", "0.05")
    codex = _FakeCodexManager()

    class _HungFactory(_FakeCodexFactory):
        def create(self, config):
            driver = _HungCodexHelperDriver(config, events=[])
            self.created.append(driver)
            return driver

    factory = _HungFactory(events=[])
    orch = _orch(tmp_path, codex=codex, factory=factory)

    result = await orch.generate_suggestions_codex(ACCOUNT, CONTEXT)

    assert result == []
    driver = factory.created[0]
    assert driver.archived == 1
    assert codex.detached == [driver.session_id]


# --- engine routing in latest_suggestions / generate_suggestions_now ------

def _codex_rec(status="idle"):
    return types.SimpleNamespace(
        status=status, cwd="/x", account=ACCOUNT, repo=None, engine=AgentEngine.CODEX,
    )


@pytest.fixture(autouse=True)
def _stub_dispatch_proposal(monkeypatch):
    monkeypatch.setattr(SessionOrchestrator, "_dispatch_proposal", lambda self, rec: None,
                         raising=False)


async def _await_until_cached(orch, sid, tries=50):
    # latest_suggestions schedules a background task; poll until it lands.
    for _ in range(tries):
        res = await orch.latest_suggestions(sid)
        if res:
            return res
        await asyncio.sleep(0.01)
    return await orch.latest_suggestions(sid)


def test_detached_codex_session_routes_to_codex_generator(monkeypatch):
    async def run():
        orch = SessionOrchestrator()
        orch._codex = object()  # only gates the background-spawn check; generation is mocked
        rec = _codex_rec("done")
        orch._records["s1"] = rec
        orch.transcript_events = AsyncMock(return_value=["evt"])
        monkeypatch.setattr(orch_mod, "last_turn_context", lambda events: ("hi", "done"),
                            raising=False)
        orch.generate_suggestions_codex = AsyncMock(return_value=["Run tests", "Commit"])

        first = await orch.latest_suggestions("s1")
        assert first == []  # nothing cached yet, generation kicked in background

        got = await _await_until_cached(orch, "s1")
        assert got == ["Run tests", "Commit"]
        orch.generate_suggestions_codex.assert_called_once_with(
            ACCOUNT, "User: hi\n\nAssistant: done")
    asyncio.run(run())


def test_live_idle_codex_session_with_no_last_suggestions_falls_through():
    async def run():
        orch = SessionOrchestrator()
        orch._codex = object()  # only gates the background-spawn check; generation is mocked
        rec = _codex_rec("idle")
        orch._records["s1"] = rec
        orch._drivers["s1"] = types.SimpleNamespace(
            engine=AgentEngine.CODEX, turn_active=False, _last_suggestions=[],
        )
        orch.transcript_events = AsyncMock(return_value=["evt"])
        orch.generate_suggestions_codex = AsyncMock(return_value=["Ship it"])

        got = await _await_until_cached(orch, "s1")
        assert got == ["Ship it"]
    asyncio.run(run())


def test_live_turn_active_codex_session_returns_empty_without_generating():
    async def run():
        orch = SessionOrchestrator()
        rec = _codex_rec("idle")
        orch._records["s1"] = rec
        orch._drivers["s1"] = types.SimpleNamespace(
            engine=AgentEngine.CODEX, turn_active=True, _last_suggestions=[],
        )
        orch.generate_suggestions_codex = AsyncMock(return_value=["should not run"])

        assert await orch.latest_suggestions("s1") == []
        orch.generate_suggestions_codex.assert_not_called()
    asyncio.run(run())


def test_claude_session_is_unaffected_by_codex_routing():
    async def run():
        orch = SessionOrchestrator()
        rec = types.SimpleNamespace(status="done", cwd="/x", account="acct", repo=None)
        orch._records["s1"] = rec
        orch._suggester = types.SimpleNamespace(generate=AsyncMock(return_value=["X"]))
        orch.generate_suggestions_codex = AsyncMock(return_value=["should not run"])
        orch.resolve_transcript_path = AsyncMock(return_value=None)

        assert await orch.latest_suggestions("s1") == []
        orch.generate_suggestions_codex.assert_not_called()
    asyncio.run(run())


def test_generate_suggestions_now_codex_regenerates_and_refreshes_cache():
    async def run():
        orch = SessionOrchestrator()
        rec = _codex_rec("done")
        orch._records["s1"] = rec
        orch.transcript_events = AsyncMock(return_value=["evt"])
        orch.generate_suggestions_codex = AsyncMock(return_value=["Fresh suggestion"])

        result = await orch.generate_suggestions_now("s1")

        assert result == ["Fresh suggestion"]
        cached = orch._detached_suggestions["s1"]
        assert cached[1] == ["Fresh suggestion"]
    asyncio.run(run())


def test_generate_suggestions_now_codex_skips_mid_turn():
    async def run():
        orch = SessionOrchestrator()
        rec = _codex_rec("idle")
        orch._records["s1"] = rec
        orch._drivers["s1"] = types.SimpleNamespace(engine=AgentEngine.CODEX, turn_active=True)
        orch.generate_suggestions_codex = AsyncMock(return_value=["should not run"])

        assert await orch.generate_suggestions_now("s1") == []
        orch.generate_suggestions_codex.assert_not_called()
    asyncio.run(run())
