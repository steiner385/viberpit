"""Orchestrator lifecycle for Codex sessions (Task 4.3).

Three invariants are worth naming, because each of them was a silent, unrecoverable
failure before this slice:

* a session is rebuilt on ITS OWN engine — a detached Codex session used to come back
  as a Claude driver pointed at a Codex thread id,
* ``engine_session_id`` is the only durable handle on a conversation, so a write from
  the wrong session/engine/account is refused rather than merged,
* replay dispatches on the engine, so a Codex session's history comes back over
  ``thread/read`` instead of a fruitless glob of the Claude projects tree.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from server.codex import schema
from server.sessions.engine import (
    AgentCapabilities,
    AgentDriverRegistry,
    AgentEngine,
    AgentEngineUnavailable,
)
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SYSTEM_IDENTITY, SessionRecord

# --------------------------------------------------------------------------- #
# doubles                                                                      #
# --------------------------------------------------------------------------- #


class _RecDriver:
    def __init__(self, engine: AgentEngine, config, *, fail_start: bool = False) -> None:
        self.engine = engine
        self.config = config
        self.session_id = config.session_id
        self.account = config.account
        self.cwd = config.cwd
        self.config_dir = config.config_dir
        self.mode = config.mode
        self.identity = config.identity
        self.resume = config.resume
        self.engine_session_id = config.resume
        self.models: list[str | None] = []
        self.started = 0
        self.closed = 0
        self._fail_start = fail_start

    async def start(self) -> None:
        self.started += 1
        if self._fail_start:
            raise RuntimeError("app-server refused the resume")

    async def aclose(self) -> None:
        self.closed += 1

    async def set_model(self, model: str | None) -> None:
        self.models.append(model)

    def seed_status(self, persisted: str) -> None:
        pass


class _RecFactory:
    def __init__(self, engine: AgentEngine, *, capabilities=None, fail_start: bool = False):
        self.engine = engine
        self.capabilities = capabilities
        self.created: list[_RecDriver] = []
        self._fail_start = fail_start

    def create(self, config):
        driver = _RecDriver(self.engine, config, fail_start=self._fail_start)
        self.created.append(driver)
        return driver


class _FakeThreadClient:
    """Answers `thread/read` from a scripted turn list."""

    def __init__(self, turns: list[dict] | None = None) -> None:
        self.turns = turns or []
        self.calls = 0

    async def read_thread(self, params):
        self.calls += 1
        return schema.parse_client_response(
            schema.METHOD_THREAD_READ,
            {"thread": {"id": params.thread_id, "cwd": "/w",
                        "status": {"type": "idle"}, "modelProvider": "openai",
                        "createdAt": 0, "updatedAt": 0, "preview": "",
                        "model": "gpt-5-codex", "turns": self.turns}},
        )


class _FakeCodexManager:
    """The slice of `CodexRunnerManager` orchestration actually touches."""

    available = True

    def __init__(self, *, client: _FakeThreadClient | None = None,
                 live: bool = True, ensure_raises: BaseException | None = None) -> None:
        self._client = client or _FakeThreadClient()
        self._live = live
        self._ensure_raises = ensure_raises
        self.ensured: list[str] = []
        self.attached: list[tuple[str, str]] = []
        self.detached: list[str] = []

    async def ensure(self, account: str) -> None:
        self.ensured.append(account)
        if self._ensure_raises is not None:
            raise self._ensure_raises
        self._live = True

    def client_for(self, account: str):
        return self._client if self._live else None

    def attach(self, account: str, session_id: str) -> None:
        self.attached.append((account, session_id))

    def detach(self, session_id: str) -> None:
        self.detached.append(session_id)


def _turn(turn_id: str, status: str = "completed") -> dict:
    return {"id": turn_id, "status": status, "items": [
        {"type": "userMessage", "id": f"{turn_id}-u",
         "content": [{"type": "text", "text": "hi"}]},
        {"type": "agentMessage", "id": f"{turn_id}-a", "text": "hello"},
    ]}


def _record(session_id: str, tmp_path: Path, *, engine=AgentEngine.CODEX,
            thread_id: str = "thread-1", **kw) -> SessionRecord:
    cwd = tmp_path / session_id
    cwd.mkdir(parents=True, exist_ok=True)
    fields: dict = dict(
        id=session_id, name="s", account="openai-main", engine=engine,
        engine_session_id=thread_id, cols=80, rows=24, cwd=str(cwd), status="idle",
    )
    fields.update(kw)
    return SessionRecord(**fields)


def _orch(tmp_path: Path, *, codex=None, registry=None) -> SessionOrchestrator:
    repo_root = tmp_path / "repos"
    repo_root.mkdir(exist_ok=True)
    return SessionOrchestrator(sdk_factory=lambda *a, **k: None, codex=codex,
                               driver_registry=registry, repo_root=str(repo_root))


def _registry(*, claude_rotates: bool = True, codex_factory: _RecFactory | None = None,
              claude_factory: _RecFactory | None = None) -> AgentDriverRegistry:
    claude = claude_factory or _RecFactory(
        AgentEngine.CLAUDE_CODE,
        capabilities=AgentCapabilities(adopt=True, account_rotation=claude_rotates))
    codex = codex_factory or _RecFactory(
        AgentEngine.CODEX, capabilities=AgentCapabilities(models=True))
    return AgentDriverRegistry({AgentEngine.CLAUDE_CODE: claude, AgentEngine.CODEX: codex})


# --------------------------------------------------------------------------- #
# 1. engine registration                                                       #
# --------------------------------------------------------------------------- #


def test_codex_is_unavailable_when_no_manager_is_wired(tmp_path: Path) -> None:
    """`self._codex is None` is the fail-closed signal: a host without Codex must
    keep raising AgentEngineUnavailable rather than build a driver with no
    app-server under it."""
    orch = _orch(tmp_path)
    assert orch._engine_registry.require(AgentEngine.CLAUDE_CODE) is AgentEngine.CLAUDE_CODE
    with pytest.raises(AgentEngineUnavailable):
        orch._engine_registry.require(AgentEngine.CODEX)


def test_codex_is_registered_when_the_manager_is_wired(tmp_path: Path) -> None:
    orch = _orch(tmp_path, codex=_FakeCodexManager())
    assert orch._engine_registry.require(AgentEngine.CODEX) is AgentEngine.CODEX
    caps = orch._engine_registry.capabilities_for(AgentEngine.CODEX)
    assert caps is not None and caps.steer and caps.approvals
    # …and the real driver's own declaration, not a restatement of it.
    from server.codex.driver import CodexAgentDriver
    assert caps is CodexAgentDriver.capabilities


def test_the_launch_model_comes_from_the_record(tmp_path: Path) -> None:
    """AgentDriverConfig carries no model, so the Codex factory resolves it through
    the orchestrator — a resume must land on the persisted alias, not the account
    default."""
    orch = _orch(tmp_path, codex=_FakeCodexManager())
    assert orch._launch_model_for_session("nope") is None
    orch._records["s1"] = _record("s1", tmp_path, model="gpt-5-codex")
    assert orch._launch_model_for_session("s1") == "gpt-5-codex"


# --------------------------------------------------------------------------- #
# 2. ensure_driver rebuilds the session's OWN engine                            #
# --------------------------------------------------------------------------- #


async def _ensure_driver(orch: SessionOrchestrator, session_id: str, tmp_path: Path,
                         identity: str = SYSTEM_IDENTITY):
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
        return await orch.ensure_driver(session_id, identity)


@pytest.mark.asyncio
async def test_ensure_driver_rebuilds_a_codex_session_as_codex(tmp_path: Path,
                                                               isolated_db) -> None:
    codex_factory = _RecFactory(AgentEngine.CODEX)
    manager = _FakeCodexManager()
    orch = _orch(tmp_path, codex=manager,
                 registry=_registry(codex_factory=codex_factory))
    orch._records["s1"] = _record("s1", tmp_path, model="gpt-5-codex")

    driver = await _ensure_driver(orch, "s1", tmp_path)

    assert driver is codex_factory.created[0]
    assert driver.engine is AgentEngine.CODEX
    # The stored thread id is the resume handle, not a fresh conversation.
    assert driver.resume == "thread-1"
    # OpenAI accounts have no Claude credential slot. Resuming Codex must not
    # manufacture a CLAUDE_CONFIG_DIR for the shared driver contract.
    assert driver.config.config_dir == ""
    assert orch._drivers["s1"] is driver


@pytest.mark.asyncio
async def test_ensure_driver_starts_and_claims_the_accounts_runner(tmp_path: Path,
                                                                   isolated_db) -> None:
    """The runner must be up before the driver is built (no app-server, no thread)
    and claimed after it registers — the mirror of _teardown_driver_state's detach,
    without which the idle sweep tears the app-server down under a live thread."""
    manager = _FakeCodexManager()
    orch = _orch(tmp_path, codex=manager, registry=_registry())
    orch._records["s1"] = _record("s1", tmp_path)

    await _ensure_driver(orch, "s1", tmp_path)

    assert manager.ensured == ["openai-main"]
    assert manager.attached == [("openai-main", "s1")]


@pytest.mark.asyncio
async def test_ensure_driver_does_not_touch_the_codex_manager_for_claude(
        tmp_path: Path, isolated_db) -> None:
    manager = _FakeCodexManager()
    orch = _orch(tmp_path, codex=manager, registry=_registry())
    orch._records["s1"] = _record("s1", tmp_path, engine=AgentEngine.CLAUDE_CODE)

    driver = await _ensure_driver(orch, "s1", tmp_path)

    assert driver.engine is AgentEngine.CLAUDE_CODE
    assert manager.ensured == [] and manager.attached == []


@pytest.mark.asyncio
async def test_a_failed_codex_resume_never_starts_a_fresh_conversation(
        tmp_path: Path, isolated_db) -> None:
    """The Claude fallback discriminates on the transcript FILE, which Codex has
    none of — so left ungated it would fire on EVERY failed Codex resume and
    abandon a thread that is still sitting in the app-server."""
    codex_factory = _RecFactory(AgentEngine.CODEX, fail_start=True)
    orch = _orch(tmp_path, codex=_FakeCodexManager(),
                 registry=_registry(codex_factory=codex_factory))
    orch._records["s1"] = _record("s1", tmp_path)

    with pytest.raises(RuntimeError, match="refused the resume"):
        await _ensure_driver(orch, "s1", tmp_path)

    assert len(codex_factory.created) == 1, "a second, resume-less driver was built"
    assert codex_factory.created[0].closed == 1
    assert "s1" not in orch._drivers


@pytest.mark.asyncio
async def test_teardown_releases_the_codex_runner_claim(tmp_path: Path,
                                                        isolated_db) -> None:
    manager = _FakeCodexManager()
    orch = _orch(tmp_path, codex=manager, registry=_registry())
    orch._records["s1"] = _record("s1", tmp_path)
    await _ensure_driver(orch, "s1", tmp_path)

    assert await orch._teardown_driver_state("s1") is not None
    assert manager.detached == ["s1"]
    assert "s1" not in orch._drivers


# --------------------------------------------------------------------------- #
# 3. capability gates on the Claude-only paths                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_rotation_refuses_a_session_whose_engine_cannot_rotate(
        tmp_path: Path) -> None:
    orch = _orch(tmp_path, codex=_FakeCodexManager(), registry=_registry())
    record = _record("s1", tmp_path)
    orch._records["s1"] = record
    paused: list[tuple[str, str]] = []

    async def _pause(rec, *, reason, earliest_reset=None):
        paused.append((rec.id, reason))

    async def _locked(*a, **k):  # pragma: no cover - the refusal must return first
        raise AssertionError("rotation ran for an engine that cannot rotate")

    orch._pause_session = _pause  # type: ignore[method-assign]
    orch._reassign_locked = _locked  # type: ignore[method-assign]

    await orch._reassign(record, "openai-other", reason="usage_limit")

    assert paused == [("s1", "engine_rotation_unsupported")]


@pytest.mark.asyncio
async def test_rotation_still_runs_for_claude(tmp_path: Path) -> None:
    """The gate is a capability check, not a blanket refusal — the Claude path must
    reach _reassign_locked exactly as before."""
    orch = _orch(tmp_path, registry=_registry())
    record = _record("s1", tmp_path, engine=AgentEngine.CLAUDE_CODE)
    orch._records["s1"] = record
    seen: list[str] = []

    async def _locked(rec, new_account, *, reason, replay_last_prompt=True,
                      new_thread=False):
        seen.append(new_account)
        return True

    orch._reassign_locked = _locked  # type: ignore[method-assign]
    await orch._reassign(record, "acct-b", reason="usage_limit")
    assert seen == ["acct-b"]


@pytest.mark.asyncio
async def test_rotation_falls_back_to_a_new_thread_when_only_that_capability_is_declared(
        tmp_path: Path) -> None:
    """account_switch_new_thread (#1275) is the honest middle ground: an engine
    that can't preserve the live thread (account_rotation=False) but CAN start a
    fresh one on the target account still moves — _reassign_locked must be told
    so it drops the resume handle, not run the thread-preserving path."""
    registry = _registry(codex_factory=_RecFactory(
        AgentEngine.CODEX, capabilities=AgentCapabilities(account_switch_new_thread=True)))
    orch = _orch(tmp_path, codex=_FakeCodexManager(), registry=registry)
    record = _record("s1", tmp_path, engine=AgentEngine.CODEX)
    orch._records["s1"] = record
    seen: list[tuple] = []

    async def _locked(rec, new_account, *, reason, replay_last_prompt=True,
                      new_thread=False):
        seen.append((new_account, new_thread))
        return True

    orch._reassign_locked = _locked  # type: ignore[method-assign]
    result = await orch._reassign(record, "openai-other", reason="usage_limit")
    assert seen == [("openai-other", True)]
    assert result == "rotated_new_thread"


@pytest.mark.asyncio
async def test_new_thread_switch_drops_the_old_thread_id_and_reports_the_loss(
        tmp_path: Path, isolated_db) -> None:
    """End-to-end through the real _reassign_locked: the new driver must NOT
    receive the old thread id as its resume handle (there is nothing to resume it
    on under a different account), and the rotated event must say so via
    newThread — the whole point of #1275 being "expose it honestly" rather than
    silently claiming a continued conversation."""
    codex_factory = _RecFactory(
        AgentEngine.CODEX, capabilities=AgentCapabilities(account_switch_new_thread=True))
    orch = _orch(tmp_path, codex=_FakeCodexManager(),
                 registry=_registry(codex_factory=codex_factory))
    record = _record("s1", tmp_path, engine=AgentEngine.CODEX, thread_id="thread-old")
    orch._records["s1"] = record
    events: list[dict] = []
    orch._publish_event = events.append  # type: ignore[assignment]

    result = await orch._reassign(record, "openai-other", reason="usage_limit")

    assert result == "rotated_new_thread"
    new_driver = codex_factory.created[-1]
    assert new_driver.engine is AgentEngine.CODEX
    assert new_driver.resume is None, "a fresh thread must not resume the old id"
    assert record.account == "openai-other"
    rotated = [e for e in events if e["type"] == "rotated"]
    assert rotated and rotated[0]["newThread"] is True
    assert rotated[0]["toAccount"] == "openai-other"


@pytest.mark.asyncio
async def test_adopt_refuses_when_the_claude_engine_declares_no_adopt(
        tmp_path: Path) -> None:
    registry = AgentDriverRegistry({AgentEngine.CODEX: _RecFactory(AgentEngine.CODEX)})
    orch = _orch(tmp_path, codex=_FakeCodexManager(), registry=registry)
    with pytest.raises(AgentEngineUnavailable):
        await orch.adopt_session("claude-sid", "person@example.com")


# --------------------------------------------------------------------------- #
# 4. the engine_session_id write fence                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_engine_session_id_is_persisted_for_the_owning_driver(
        tmp_path: Path, isolated_db) -> None:
    orch = _orch(tmp_path, codex=_FakeCodexManager())
    orch._records["s1"] = _record("s1", tmp_path, thread_id="")
    await orch._persist_engine_session_id(
        "s1", "thread-9", expect_session_id="s1",
        expect_engine=AgentEngine.CODEX, expect_account="openai-main")
    assert orch._records["s1"].engine_session_id == "thread-9"


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [
    {"expect_session_id": "other"},
    {"expect_engine": AgentEngine.CLAUDE_CODE},
    {"expect_account": "some-other-account"},
])
async def test_engine_session_id_write_is_fenced(tmp_path: Path, isolated_db,
                                                 kwargs: dict) -> None:
    """Session, engine and account each fence the write. Overwriting the handle with
    a foreign id orphans the real conversation with nothing left pointing at it."""
    orch = _orch(tmp_path, codex=_FakeCodexManager())
    orch._records["s1"] = _record("s1", tmp_path, thread_id="thread-1")
    fence: dict[str, object] = {"expect_session_id": "s1",
                                "expect_engine": AgentEngine.CODEX,
                                "expect_account": "openai-main"}
    fence.update(kwargs)
    await orch._persist_engine_session_id("s1", "claude-uuid", **fence)  # type: ignore[arg-type]
    assert orch._records["s1"].engine_session_id == "thread-1"


@pytest.mark.asyncio
async def test_the_fence_is_bound_to_the_driver_that_reports(tmp_path: Path,
                                                             isolated_db) -> None:
    """The wiring, not just the guard: _create_driver binds the triple, so a driver
    can only ever report under its own identity."""
    codex_factory = _RecFactory(AgentEngine.CODEX)
    orch = _orch(tmp_path, codex=_FakeCodexManager(),
                 registry=_registry(codex_factory=codex_factory))
    orch._records["s1"] = _record("s1", tmp_path, thread_id="thread-1")
    await _ensure_driver(orch, "s1", tmp_path)

    report = codex_factory.created[0].config.on_engine_session_id
    assert report.keywords == {"expect_session_id": "s1",
                               "expect_engine": AgentEngine.CODEX,
                               "expect_account": "openai-main"}
    # A late report from a driver that has since been superseded by a rotation is
    # dropped rather than clobbering the live handle.
    orch._records["s1"].account = "openai-rotated"
    await report("s1", "thread-STALE")
    assert orch._records["s1"].engine_session_id == "thread-1"


# --------------------------------------------------------------------------- #
# 5. replay dispatches on the engine                                            #
# --------------------------------------------------------------------------- #


def test_transcript_path_is_none_for_a_codex_session(tmp_path: Path) -> None:
    """A `.jsonl` is a Claude artefact. None here is also the signal the WS attach
    keys its engine-replay fallback off."""
    orch = _orch(tmp_path, codex=_FakeCodexManager())
    orch._records["s1"] = _record("s1", tmp_path)
    with patch.object(orch, "_find_transcript") as find:
        assert orch.transcript_path("s1") is None
    find.assert_not_called()


@pytest.mark.asyncio
async def test_engine_replay_reads_a_codex_thread(tmp_path: Path) -> None:
    client = _FakeThreadClient([_turn("t1"), _turn("t2")])
    orch = _orch(tmp_path, codex=_FakeCodexManager(client=client))
    orch._records["s1"] = _record("s1", tmp_path)

    from server.codex.replay import CodexThreadReplay
    replay = await orch._engine_replay("s1")
    assert isinstance(replay, CodexThreadReplay)

    window = await orch.transcript_window("s1")
    assert window is not None and window.total_turns == 2
    assert client.calls == 1


@pytest.mark.asyncio
async def test_engine_replay_reads_the_sidecar_the_driver_factory_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One derivation for both ends: the path the replay reads item times from is
    the path the driver factory hands its writer, under the account's config dir."""
    import server.sessions.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "account_cfg_dir", lambda name: tmp_path / "cfg" / name)
    orch = _orch(tmp_path, codex=_FakeCodexManager(client=_FakeThreadClient([_turn("t1")])))
    orch._records["s1"] = _record("s1", tmp_path)

    from server.codex.replay import CodexThreadReplay
    replay = await orch._engine_replay("s1")
    assert isinstance(replay, CodexThreadReplay)
    expected = tmp_path / "cfg" / "openai-main" / "codex-item-times" / "thread-1.jsonl"
    assert replay.item_times_path == expected

    from server.codex.driver import CodexAgentDriverFactory
    factory = orch._default_driver_factories()[AgentEngine.CODEX]
    assert isinstance(factory, CodexAgentDriverFactory)
    assert factory._item_times_root_for is not None
    assert factory._item_times_root_for("openai-main") == expected.parent


@pytest.mark.asyncio
async def test_permanent_delete_removes_a_codex_sessions_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.sessions.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "account_cfg_dir", lambda name: tmp_path / "cfg" / name)
    orch = _orch(tmp_path, codex=_FakeCodexManager())
    sidecar = tmp_path / "cfg" / "openai-main" / "codex-item-times" / "thread-1.jsonl"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text('{"i":"a","s":1}\n')
    history = sidecar.with_suffix(".turns.jsonl")
    history.write_text("saved conversation")

    class _Row:
        id = "s1"; engine = "codex"; engine_session_id = "thread-1"; account_name = "openai-main"

    await orch._delete_codex_item_times(_Row())
    assert not sidecar.exists()
    assert not history.exists()
    # Idempotent, and never a reason to fail the delete.
    await orch._delete_codex_item_times(_Row())

    class _ClaudeRow(_Row):
        engine = "claude_code"
    sidecar.write_text("")
    await orch._delete_codex_item_times(_ClaudeRow())
    assert sidecar.exists()


@pytest.mark.asyncio
async def test_engine_replay_starts_a_runner_that_is_not_live(tmp_path: Path) -> None:
    manager = _FakeCodexManager(live=False)
    orch = _orch(tmp_path, codex=manager)
    orch._records["s1"] = _record("s1", tmp_path)
    assert await orch._engine_replay("s1") is not None
    assert manager.ensured == ["openai-main"]


@pytest.mark.asyncio
async def test_engine_replay_does_not_restart_a_live_runner(tmp_path: Path) -> None:
    """Replay runs on background sweeps too; re-entering the manager's start path on
    each of them would churn a healthy process."""
    manager = _FakeCodexManager()
    orch = _orch(tmp_path, codex=manager)
    orch._records["s1"] = _record("s1", tmp_path)
    await orch._engine_replay("s1")
    assert manager.ensured == []


@pytest.mark.asyncio
async def test_engine_replay_is_none_when_the_runner_will_not_start(
        tmp_path: Path) -> None:
    manager = _FakeCodexManager(live=False, ensure_raises=RuntimeError("no auth"))
    orch = _orch(tmp_path, codex=manager)
    orch._records["s1"] = _record("s1", tmp_path)
    assert await orch._engine_replay("s1") is None
    # None means "could not read", NOT "the session has no history".
    assert await orch.transcript_window("s1") is None


@pytest.mark.asyncio
async def test_engine_replay_is_none_before_turn_one(tmp_path: Path) -> None:
    orch = _orch(tmp_path, codex=_FakeCodexManager())
    orch._records["s1"] = _record("s1", tmp_path, thread_id="")
    assert await orch._engine_replay("s1") is None
    assert await orch.transcript_events("s1") == []


class _ThreadDriver:
    """A live Codex driver reduced to what `_codex_replay` reads off it."""

    def __init__(self, *, thread_may_have_history: bool) -> None:
        self.engine_session_id = "thread-1"
        self.account = "openai-main"
        self.thread_may_have_history = thread_may_have_history


class _RefusingThreadClient(_FakeThreadClient):
    """The pinned app-server: `thread/read includeTurns` on a thread that never ran
    a turn is refused (-32603 against a real account's state DB, #2692)."""

    async def read_thread(self, params):
        from server.codex.protocol import CodexRequestFailed
        self.calls += 1
        raise CodexRequestFailed(-32603, method=schema.METHOD_THREAD_READ)


@pytest.mark.asyncio
async def test_a_never_turned_thread_is_not_read_back(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Create persists `idle`, which sweeps sub-agents, which replays — so every
    fresh Codex session asked the app-server for turns it could not have, and filed
    the refusal as an auto-error (#2692). The driver knows; nothing is sent."""
    import logging

    import server.sessions.orchestrator as orch_mod
    orch_mod._REPLAY_FAILURE_LOG.clear()
    client = _RefusingThreadClient()
    manager = _FakeCodexManager(client=client, live=False)
    orch = _orch(tmp_path, codex=manager)
    orch._records["s1"] = _record("s1", tmp_path)
    orch._drivers["s1"] = _ThreadDriver(thread_may_have_history=False)  # type: ignore[assignment]

    with caplog.at_level(logging.WARNING, logger="server.sessions.orchestrator"):
        assert await orch.transcript_events("s1", max_turns=None) == []
        assert await orch.transcript_window("s1") is None
        assert await orch.engine_replay_window("s1") is None
        assert await orch.has_transcript("s1") is False

    assert client.calls == 0
    # Nor is a runner started just to be told there is nothing to read.
    assert manager.ensured == []
    assert caplog.records == []


@pytest.mark.asyncio
async def test_a_thread_that_has_run_a_turn_is_read_back(tmp_path: Path) -> None:
    """The other side of the guard above: a first `turn/start`, or a driver built
    with `resume=`, reads exactly as before."""
    client = _FakeThreadClient([_turn("t1")])
    orch = _orch(tmp_path, codex=_FakeCodexManager(client=client))
    orch._records["s1"] = _record("s1", tmp_path)
    orch._drivers["s1"] = _ThreadDriver(thread_may_have_history=True)  # type: ignore[assignment]

    assert await orch.transcript_events("s1", max_turns=None) != []
    assert client.calls == 1


@pytest.mark.asyncio
async def test_a_driver_that_cannot_say_is_still_read_back(tmp_path: Path) -> None:
    """Only an explicit "never had a turn" skips the read. A driver without the
    property (another engine's, a test double) keeps today's path — skipping on
    absence would blank a transcript that exists."""
    client = _FakeThreadClient([_turn("t1")])
    orch = _orch(tmp_path, codex=_FakeCodexManager(client=client))
    orch._records["s1"] = _record("s1", tmp_path)
    driver = _ThreadDriver(thread_may_have_history=True)
    del driver.thread_may_have_history
    orch._drivers["s1"] = driver  # type: ignore[assignment]

    assert await orch.transcript_events("s1", max_turns=None) != []
    assert client.calls == 1


@pytest.mark.asyncio
async def test_engine_replay_window_hands_the_live_turn_to_the_stream(
        tmp_path: Path) -> None:
    """The attach-only arguments transcript_window does not carry: an inProgress
    turn is dropped so the live stream re-supplies it instead of duplicating it."""
    client = _FakeThreadClient([_turn("t1"), _turn("t2", status="inProgress")])
    orch = _orch(tmp_path, codex=_FakeCodexManager(client=client))
    orch._records["s1"] = _record("s1", tmp_path)

    kept = await orch.engine_replay_window("s1")
    dropped = await orch.engine_replay_window("s1", drop_incomplete_turn=True)

    assert kept is not None and kept.total_turns == 2
    assert dropped is not None and dropped.total_turns == 1


@pytest.mark.asyncio
async def test_engine_replay_window_falls_back_to_the_claude_reader(
        tmp_path: Path) -> None:
    """The method is engine-neutral: a Claude session with no transcript yet still
    answers None, and one with a transcript replays off disk."""
    orch = _orch(tmp_path)
    orch._records["s1"] = _record("s1", tmp_path, engine=AgentEngine.CLAUDE_CODE)
    assert await orch.engine_replay_window("s1") is None

    jsonl = tmp_path / "t.jsonl"
    jsonl.write_text(
        '{"type":"user","uuid":"u1","message":{"content":"hi"}}\n'
        '{"type":"assistant","uuid":"a1","message":{"model":"opus",'
        '"content":[{"type":"text","text":"yo"}]}}\n')
    with patch.object(orch, "_find_transcript", return_value=str(jsonl)):
        window = await orch.engine_replay_window("s1")
    assert window is not None and window.total_turns == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("preexisting_row", [False, True], ids=["fresh", "queued"])
@pytest.mark.parametrize("prompt_state", ["command", "pending", "started"])
async def test_codex_native_id_survives_restart_at_first_turn_boundary(
        tmp_path: Path, isolated_db, monkeypatch: pytest.MonkeyPatch,
        preexisting_row: bool, prompt_state: str) -> None:
    """Use the real driver/JSONL stack; only a native turn creates a resume handle."""
    import asyncio

    from server.db import db_session
    from server.db.models import Session as DbSession
    from contextlib import nullcontext

    from server.tests.test_codex_driver import Harness, THREAD

    # The real factory persists completed turns. Keep this fake account/thread
    # private so later replay tests cannot inherit its durable history.
    import server.sessions.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "account_cfg_dir", lambda name: tmp_path / "cfg" / name)

    first_turn = prompt_state == "started"
    harness = Harness(tmp_path)
    await harness.boot(start=False)
    orch = _orch(tmp_path, codex=harness.manager)
    restarted = _orch(tmp_path, codex=harness.manager)
    session_id = "restart-session"
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    prompt = "/help" if prompt_state == "command" else "Continue the calendar implementation"
    if preexisting_row:
        async with db_session() as db:
            db.add(DbSession(
                id=session_id, name="Restart", account_name="openai-main",
                engine=AgentEngine.CODEX.value, engine_session_id=None, status="queued",
                cwd=str(cwd), permission_mode="interactive", initial_prompt=prompt,
            ))
    try:
        # Simulate the crash window after row creation but before a normal
        # prompt is sent; all other cases run the actual send path.
        with (patch.object(orch, "send_prompt", new_callable=AsyncMock)
              if prompt_state == "pending" else nullcontext()):
            rec = await orch._spawn_session(
                session_id, name="Restart", account="openai-main",
                initial_prompt=prompt, cols=80, rows=24, cwd=str(cwd), branch=None,
                repo=None, identity=SYSTEM_IDENTITY, permission_mode="interactive",
                attachment_ids=None, name_is_auto=False, origin=None, origin_ref=None,
                model=None, engine=AgentEngine.CODEX, preexisting_row=preexisting_row,
            )
        if first_turn:
            harness.notify_turn_started()
            harness.notify_turn_completed()
            await harness.settle()
        native_id = THREAD if first_turn else None
        assert (rec.engine_session_id or None) == native_id
        async with db_session() as db:
            row = await db.get(DbSession, session_id)
            assert row is not None
            assert row.engine_session_id == native_id
            assert row.engine == AgentEngine.CODEX.value
            assert row.account_name == "openai-main"

        await orch.stop()
        with patch.object(restarted, "_deliver_initial_prompts", new_callable=AsyncMock) as replay:
            assert await restarted.rehydrate() == 1
            if restarted._bg_tasks:
                await asyncio.gather(*list(restarted._bg_tasks))
            if prompt_state == "pending":
                replay.assert_awaited_once_with([(session_id, prompt)])
            else:
                replay.assert_not_awaited()
        restored = restarted._records[session_id]
        assert (restored.engine_session_id or None) == native_id
        if prompt_state == "command":
            # The bridge-deferred command survives without a bogus empty-thread
            # resume handle, and will be dispatched on the next attach.
            assert restarted._pending_initial_prompt[session_id] == prompt
        else:
            assert session_id not in restarted._pending_initial_prompt
        driver = await _ensure_driver(restarted, session_id, tmp_path)
        assert driver.resume == native_id
        assert driver.engine is AgentEngine.CODEX
        assert driver.account == "openai-main"
        assert driver.cwd == str(cwd)
        if first_turn:
            assert len(harness.frames(schema.METHOD_TURN_START)) == 1
            assert harness.params(schema.METHOD_THREAD_RESUME)["threadId"] == THREAD
        else:
            assert not harness.frames(schema.METHOD_TURN_START)
            assert not harness.frames(schema.METHOD_THREAD_RESUME)
            assert len(harness.frames(schema.METHOD_THREAD_START)) == 2
    finally:
        await orch.stop()
        await restarted.stop()
        await harness.aclose()


@pytest.mark.asyncio
async def test_claude_native_id_remains_deferred_until_first_turn(
        tmp_path: Path, isolated_db) -> None:
    from server.db import db_session
    from server.db.models import Session as DbSession

    factory = _RecFactory(AgentEngine.CLAUDE_CODE)
    orch = _orch(tmp_path, registry=_registry(claude_factory=factory))
    try:
        with patch.object(orch, "_driver_config_dir", return_value=""):
            rec = await orch._spawn_session(
                "late-claude", name="Restart", account="claude-main",
                initial_prompt=None, cols=80, rows=24, cwd=str(tmp_path), branch=None,
                repo=None, identity=SYSTEM_IDENTITY, permission_mode="interactive",
                attachment_ids=None, name_is_auto=False, origin=None, origin_ref=None,
                model=None, engine=AgentEngine.CLAUDE_CODE,
            )
        async with db_session() as db:
            row = await db.get(DbSession, rec.id)
            assert row is not None and row.engine_session_id is None
        await factory.created[0].config.on_engine_session_id(rec.id, "claude-native-id")
        async with db_session() as db:
            row = await db.get(DbSession, rec.id)
            assert row is not None and row.engine_session_id == "claude-native-id"
        assert rec.engine_session_id == "claude-native-id"
    finally:
        await orch.stop()
