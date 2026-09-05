from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from server.sessions.engine import AgentDriverRegistry, AgentEngine
from server.sessions.orchestrator import SessionOrchestrator


class _Driver:
    def __init__(self) -> None:
        self.started = False
        self.close_calls = 0

    async def start(self) -> None:
        self.started = True

    async def set_model(self, model: str | None) -> None:
        pass

    async def aclose(self) -> None:
        self.close_calls += 1


class _BlockingDriver(_Driver):
    def __init__(self) -> None:
        super().__init__()
        self.entered_start = asyncio.Event()
        self.release_start = asyncio.Event()

    async def start(self) -> None:
        self.started = True
        self.entered_start.set()
        await self.release_start.wait()


class _DoubleCancelDriver(_BlockingDriver):
    def __init__(self) -> None:
        super().__init__()
        self.entered_close = asyncio.Event()
        self.release_close = asyncio.Event()

    async def aclose(self) -> None:
        self.close_calls += 1
        self.entered_close.set()
        await self.release_close.wait()


class _Factory:
    def __init__(self, driver: _Driver) -> None:
        self.driver = driver
        self.configs = []

    def create(self, config):
        self.configs.append(config)
        return self.driver


class _AttachRaceManager:
    available = True

    def __init__(self) -> None:
        self.claimed: set[str] = set()
        self.detached: list[str] = []

    async def ensure(self, account: str) -> None:
        pass

    def attach(self, account: str, session_id: str) -> None:
        self.claimed.add(session_id)
        raise RuntimeError("runner exited during attach")

    def detach(self, session_id: str) -> None:
        self.detached.append(session_id)
        self.claimed.discard(session_id)


class _X11:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.stopped: list[str] = []

    async def start_display(self, session_id: str) -> None:
        self.started.append(session_id)

    async def stop_display(self, session_id: str) -> None:
        self.stopped.append(session_id)


@pytest.mark.asyncio
async def test_codex_attach_failure_rolls_back_started_spawn(tmp_path: Path) -> None:
    session_id = "codex-attach-race"
    account = "openai-main"
    driver = _Driver()
    manager = _AttachRaceManager()
    x11 = _X11()
    factory = _Factory(driver)
    registry = AgentDriverRegistry({AgentEngine.CODEX: factory})
    repo_root = tmp_path / "repos"
    repo_root.mkdir()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    orchestrator = SessionOrchestrator(
        codex=manager, x11=x11, driver_registry=registry, repo_root=str(repo_root),
    )
    with (
        patch("server.sessions.orchestrator.compose", return_value=config_dir) as compose,
        patch("server.sessions.orchestrator.configdir.trust_project") as trust_project,
        patch.object(orchestrator, "_isolated_dir_for", side_effect=lambda cfg, kind: cfg),
    ):
        with pytest.raises(RuntimeError, match="runner exited during attach"):
            await orchestrator._spawn_session(
                session_id,
                name="Codex",
                account=account,
                initial_prompt=None,
                cols=80,
                rows=24,
                cwd=None,
                branch=None,
                repo=None,
                identity="person@example.com",
                permission_mode="interactive",
                attachment_ids=None,
                name_is_auto=False,
                origin=None,
                origin_ref=None,
                model=None,
                engine=AgentEngine.CODEX,
            )

    compose.assert_not_called()
    trust_project.assert_not_called()
    assert factory.configs[0].config_dir == ""
    assert driver.started
    assert driver.close_calls == 1
    assert session_id not in orchestrator._drivers
    assert session_id not in orchestrator._records
    assert session_id not in orchestrator._spawning
    assert session_id not in orchestrator._worktrees
    assert manager.claimed == set()
    assert manager.detached == [session_id]
    assert x11.started == [session_id]
    assert x11.stopped == [session_id]
    assert not (repo_root / ".ccswitch" / "scratch" / session_id).exists()


@pytest.mark.asyncio
async def test_cancel_during_driver_start_closes_allocated_driver_once(
    tmp_path: Path,
) -> None:
    session_id = "codex-cancelled-start"
    driver = _BlockingDriver()
    manager = _AttachRaceManager()
    registry = AgentDriverRegistry({AgentEngine.CODEX: _Factory(driver)})
    repo_root = tmp_path / "repos"
    repo_root.mkdir()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    orchestrator = SessionOrchestrator(
        codex=manager, driver_registry=registry, repo_root=str(repo_root),
    )

    with (
        patch("server.sessions.orchestrator.compose", return_value=config_dir),
        patch("server.sessions.orchestrator.configdir.trust_project"),
        patch.object(orchestrator, "_isolated_dir_for", side_effect=lambda cfg, kind: cfg),
    ):
        spawn = asyncio.create_task(orchestrator._spawn_session(
            session_id,
            name="Codex",
            account="openai-main",
            initial_prompt=None,
            cols=80,
            rows=24,
            cwd=None,
            branch=None,
            repo=None,
            identity="person@example.com",
            permission_mode="interactive",
            attachment_ids=None,
            name_is_auto=False,
            origin=None,
            origin_ref=None,
            model=None,
            engine=AgentEngine.CODEX,
        ))
        await driver.entered_start.wait()
        spawn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await spawn

    assert driver.close_calls == 1
    assert session_id not in orchestrator._drivers
    assert session_id not in orchestrator._records
    assert session_id not in orchestrator._spawning
    assert manager.claimed == set()
    assert manager.detached == [session_id]
    assert not (repo_root / ".ccswitch" / "scratch" / session_id).exists()


@pytest.mark.asyncio
async def test_second_cancel_cannot_interrupt_spawn_rollback(tmp_path: Path) -> None:
    session_id = "codex-double-cancel"
    driver = _DoubleCancelDriver()
    manager = _AttachRaceManager()
    registry = AgentDriverRegistry({AgentEngine.CODEX: _Factory(driver)})
    repo_root = tmp_path / "repos"
    repo_root.mkdir()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    orchestrator = SessionOrchestrator(
        codex=manager, driver_registry=registry, repo_root=str(repo_root),
    )

    with (
        patch("server.sessions.orchestrator.compose", return_value=config_dir),
        patch("server.sessions.orchestrator.configdir.trust_project"),
        patch.object(orchestrator, "_isolated_dir_for", side_effect=lambda cfg, kind: cfg),
    ):
        spawn = asyncio.create_task(orchestrator._spawn_session(
            session_id,
            name="Codex",
            account="openai-main",
            initial_prompt=None,
            cols=80,
            rows=24,
            cwd=None,
            branch=None,
            repo=None,
            identity="person@example.com",
            permission_mode="interactive",
            attachment_ids=None,
            name_is_auto=False,
            origin=None,
            origin_ref=None,
            model=None,
            engine=AgentEngine.CODEX,
        ))
        await driver.entered_start.wait()
        spawn.cancel()
        await driver.entered_close.wait()
        spawn.cancel()
        await asyncio.sleep(0)
        assert not spawn.done()
        driver.release_close.set()
        with pytest.raises(asyncio.CancelledError):
            await spawn

    assert driver.close_calls == 1
    assert session_id not in orchestrator._drivers
    assert session_id not in orchestrator._records
    assert session_id not in orchestrator._spawning
    assert manager.claimed == set()
    assert manager.detached == [session_id]
    assert not (repo_root / ".ccswitch" / "scratch" / session_id).exists()
