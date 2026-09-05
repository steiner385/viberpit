"""Recovery for Codex sessions: restart, crash, authority loss, host loss, archive.

The through-line is that a Codex conversation lives in the app-server, not on this
host's disk — so recovery is "get the runner back and resume the thread", and every
failure to do that must land somewhere a human can see rather than quietly minting a
new conversation over the old one. An ambiguous in-flight turn is therefore always
resolved as interrupted, never as success (the #1100/#1112 shape: persist the
terminal fact first, and never let a recovery path upgrade an unknown to a good
outcome).
"""
from __future__ import annotations

import contextlib
from pathlib import Path
from unittest.mock import patch

import pytest

from server.db import db_session, init_db
from server.db.models import Session as DbSession
from server.sessions.engine import (
    AgentCapabilities,
    AgentDriverRegistry,
    AgentEngine,
    AgentEngineUnavailable,
)
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SYSTEM_IDENTITY

from server.tests.test_codex_orchestrator import (
    _FakeCodexManager,
    _FakeThreadClient,
    _RecFactory,
    _record,
    _turn,
)


def _registry(codex_factory: _RecFactory | None = None) -> AgentDriverRegistry:
    return AgentDriverRegistry({
        AgentEngine.CLAUDE_CODE: _RecFactory(
            AgentEngine.CLAUDE_CODE,
            capabilities=AgentCapabilities(adopt=True, account_rotation=True)),
        AgentEngine.CODEX: codex_factory or _RecFactory(AgentEngine.CODEX),
    })


def _orch(tmp_path: Path, *, codex=None, registry=None) -> SessionOrchestrator:
    repo_root = tmp_path / "repos"
    repo_root.mkdir(exist_ok=True)
    return SessionOrchestrator(sdk_factory=lambda *a, **k: None, codex=codex,
                               driver_registry=registry, repo_root=str(repo_root))


async def _seed_row(tmp_path: Path, session_id: str, *, status: str,
                    engine: str = AgentEngine.CODEX.value,
                    thread_id: str = "thread-1") -> str:
    cwd = tmp_path / session_id
    cwd.mkdir(parents=True, exist_ok=True)
    async with db_session() as db:
        db.add(DbSession(
            id=session_id, name="codex session", account_name="openai-main",
            engine=engine, engine_session_id=thread_id, status=status,
            cwd=str(cwd),
        ))
    return str(cwd)


async def _ensure_driver(orch: SessionOrchestrator, session_id: str, tmp_path: Path):
    cfg = tmp_path / "cfg"
    cfg.mkdir(exist_ok=True)

    async def _noop(_sid):
        return None

    orch._maybe_rearm_on_attach = _noop  # type: ignore[method-assign]
    with (
        patch("server.sessions.orchestrator.compose", return_value=cfg),
        patch("server.sessions.orchestrator.configdir.trust_project"),
        patch.object(orch, "_isolated_dir_for", side_effect=lambda c, kind: c),
    ):
        return await orch.ensure_driver(session_id, SYSTEM_IDENTITY)


# --------------------------------------------------------------------------- #
# daemon restart / drain                                                        #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_restart_leaves_an_in_flight_codex_turn_interrupted(
        tmp_path: Path, isolated_db) -> None:
    """A daemon restart cannot know how the turn ended, so it must not be recorded
    as finished. The row comes back idle AND flagged interrupted-by-restart — the
    banner a human acts on — with its thread id intact for the resume."""
    await _seed_row(tmp_path, "s1", status="in_progress")
    orch = _orch(tmp_path, codex=_FakeCodexManager(), registry=_registry())

    assert await orch.rehydrate() == 1

    rec = orch._records["s1"]
    assert rec.engine is AgentEngine.CODEX
    assert rec.status == "idle"
    assert rec.interrupted_by_restart is True
    assert rec.interrupted_from_status == "in_progress"
    assert rec.engine_session_id == "thread-1"
    assert rec.status not in {"done", "complete"}


@pytest.mark.asyncio
async def test_rehydrate_never_globs_the_claude_projects_tree_for_a_codex_row(
        tmp_path: Path, isolated_db) -> None:
    """`waiting_external` self-heal reads the transcript. A Codex row has none, so
    the engine gate must answer before the (sshfs-backed) glob runs."""
    await _seed_row(tmp_path, "s1", status="waiting_external")
    orch = _orch(tmp_path, codex=_FakeCodexManager(), registry=_registry())
    with patch.object(orch, "_find_transcript") as find:
        assert await orch.rehydrate() == 1
    find.assert_not_called()


@pytest.mark.asyncio
async def test_a_rehydrated_codex_session_resumes_its_thread_on_a_restarted_runner(
        tmp_path: Path, isolated_db) -> None:
    codex_factory = _RecFactory(AgentEngine.CODEX)
    manager = _FakeCodexManager(live=False)
    await _seed_row(tmp_path, "s1", status="in_progress")
    orch = _orch(tmp_path, codex=manager, registry=_registry(codex_factory))
    await orch.rehydrate()

    driver = await _ensure_driver(orch, "s1", tmp_path)

    assert manager.ensured == ["openai-main"], "the account's app-server was not restarted"
    assert driver.engine is AgentEngine.CODEX
    assert driver.resume == "thread-1"


@pytest.mark.asyncio
async def test_repeated_restarts_keep_resuming_the_same_thread(
        tmp_path: Path, isolated_db) -> None:
    """Each cycle must resume, never mint a new thread — the failure mode that
    silently strands the previous conversation."""
    codex_factory = _RecFactory(AgentEngine.CODEX)
    manager = _FakeCodexManager()
    await _seed_row(tmp_path, "s1", status="in_progress")

    for _ in range(3):
        orch = _orch(tmp_path, codex=manager, registry=_registry(codex_factory))
        await orch.rehydrate()
        await _ensure_driver(orch, "s1", tmp_path)
        await orch._teardown_driver_state("s1")

    assert [d.resume for d in codex_factory.created] == ["thread-1"] * 3
    assert manager.detached == ["s1", "s1", "s1"]
    assert len(manager.attached) == 3


# --------------------------------------------------------------------------- #
# crash / authority loss / host loss                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_lost_credential_authority_fails_the_attach_and_keeps_the_handle(
        tmp_path: Path, isolated_db) -> None:
    """No app-server means no session. Failing the attach is the fail-closed answer;
    the thread id must survive so a later retry resumes rather than starts over."""
    manager = _FakeCodexManager(live=False, ensure_raises=RuntimeError("chatgpt auth lost"))
    codex_factory = _RecFactory(AgentEngine.CODEX)
    await _seed_row(tmp_path, "s1", status="idle")
    orch = _orch(tmp_path, codex=manager, registry=_registry(codex_factory))
    await orch.rehydrate()

    with pytest.raises(RuntimeError, match="chatgpt auth lost"):
        await _ensure_driver(orch, "s1", tmp_path)

    assert codex_factory.created == [], "a driver was built without an app-server"
    assert "s1" not in orch._drivers
    assert orch._records["s1"].engine_session_id == "thread-1"


@pytest.mark.asyncio
async def test_a_runner_that_dies_between_ensure_and_attach_unwinds_the_build(
        tmp_path: Path, isolated_db) -> None:
    class _AttachRaces(_FakeCodexManager):
        def attach(self, account: str, session_id: str) -> None:
            raise RuntimeError("runner exited during attach")

    manager = _AttachRaces()
    codex_factory = _RecFactory(AgentEngine.CODEX)
    await _seed_row(tmp_path, "s1", status="idle")
    orch = _orch(tmp_path, codex=manager, registry=_registry(codex_factory))
    await orch.rehydrate()

    with pytest.raises(RuntimeError, match="runner exited during attach"):
        await _ensure_driver(orch, "s1", tmp_path)

    assert "s1" not in orch._drivers, "a driver bound to a dead app-server stayed registered"
    assert codex_factory.created[0].closed == 1
    assert manager.detached == ["s1"]


@pytest.mark.asyncio
async def test_a_host_without_codex_refuses_rather_than_rebuilding_as_claude(
        tmp_path: Path, isolated_db) -> None:
    """Host loss: the row moved to (or was left on) a host where Codex is not
    installed. `self._codex is None` keeps the engine out of the registry, and the
    row must NOT come back as a Claude driver over a Codex thread id."""
    await _seed_row(tmp_path, "s1", status="idle")
    orch = _orch(tmp_path)  # no codex manager, real registry
    await orch.rehydrate()

    with pytest.raises(AgentEngineUnavailable):
        await _ensure_driver(orch, "s1", tmp_path)
    assert "s1" not in orch._drivers


@pytest.mark.asyncio
async def test_replay_on_a_host_without_codex_reports_no_history_not_an_empty_one(
        tmp_path: Path, isolated_db) -> None:
    await _seed_row(tmp_path, "s1", status="idle")
    orch = _orch(tmp_path)
    await orch.rehydrate()
    assert await orch.transcript_window("s1") is None
    assert await orch.engine_replay_window("s1") is None


# --------------------------------------------------------------------------- #
# archive / delete                                                              #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_archive_releases_ccswitch_ownership_without_touching_the_thread(
        tmp_path: Path, isolated_db) -> None:
    """Archiving is reversible in ccswitch (unarchive exists), so it must not
    archive or delete the native thread — that would destroy the resumability
    unarchive depends on. It releases the runner claim and closes the driver."""
    class _StrictClient(_FakeThreadClient):
        async def archive_thread(self, params):  # pragma: no cover - must not run
            raise AssertionError("archive_session issued a native thread/archive")

    manager = _FakeCodexManager(client=_StrictClient())
    codex_factory = _RecFactory(AgentEngine.CODEX)
    await _seed_row(tmp_path, "s1", status="idle")
    orch = _orch(tmp_path, codex=manager, registry=_registry(codex_factory))
    await orch.rehydrate()
    await _ensure_driver(orch, "s1", tmp_path)

    driver: object = orch._drivers["s1"]
    await orch.archive_session("s1")
    # archive_session defers the driver close to a background task; drain them so a
    # native call made during teardown would still fail this test.
    for task in list(getattr(orch, "_bg_tasks", ()) or ()):
        with contextlib.suppress(Exception):
            await task

    assert getattr(driver, "closed") == 1
    assert manager.detached == ["s1"]
    assert "s1" not in orch._drivers and "s1" not in orch._records
    async with db_session() as db:
        row = await db.get(DbSession, "s1")
        assert row is not None and row.status == "archived"
        assert row.engine_session_id == "thread-1", (
            "the thread handle must survive an archive so unarchive can resume it")


@pytest.mark.asyncio
async def test_history_of_an_archived_codex_session_is_still_readable(
        tmp_path: Path, isolated_db) -> None:
    """An archived row is not rehydrated, so `_records` cannot say which engine owns
    it — but `GET /api/sessions/{id}/transcript` serves archived sessions. Without the
    row lookup, every archived Codex session's review page renders blank."""
    client = _FakeThreadClient([_turn("t1"), _turn("t2")])
    manager = _FakeCodexManager(client=client)
    await init_db()
    await _seed_row(tmp_path, "s1", status="archived")
    orch = _orch(tmp_path, codex=manager, registry=_registry())
    assert "s1" not in orch._records

    # The Claude transcript glob must never be consulted for a Codex row…
    with patch.object(orch, "_find_transcript") as find:
        assert await orch.resolve_transcript_path("s1") is None
        window = await orch.transcript_window("s1")
    find.assert_not_called()
    # …and the history comes back over thread/read instead.
    assert window is not None and window.total_turns == 2
    assert client.calls == 1


# --------------------------------------------------------------------------- #
# teardown paths                                                                #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_every_teardown_path_releases_the_runner_claim(
        tmp_path: Path, isolated_db) -> None:
    """`_teardown_driver_state` is the single "this session no longer has a driver"
    primitive, and releasing the runner claim lives inside it — so close, archive and
    the assistant reap all inherit it. A path that released the driver without going
    through it would strand a ref-count and pin the app-server alive forever.

    (The one deliberate second call site is ensure_driver's attach-failure unwind,
    which is undoing a build whose driver was never fully registered; it cannot use
    the shared primitive without also dropping this session's deferred initial prompt
    and its X11 display. It is covered by
    test_a_runner_that_dies_between_ensure_and_attach_unwinds_the_build.)"""
    manager = _FakeCodexManager()
    orch = _orch(tmp_path, codex=manager, registry=_registry())
    orch._records["s1"] = _record("s1", tmp_path)
    await _ensure_driver(orch, "s1", tmp_path)

    await orch.close_session("s1")

    assert manager.detached == ["s1"]
    assert "s1" not in orch._drivers
