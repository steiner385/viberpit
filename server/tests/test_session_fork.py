"""Fork from here (D-8): ``fork_session`` on the orchestrator and every layer above it
(daemon RPC, RemoteOrchestrator, fleet router, facade, ``POST /api/sessions/{id}/fork``).

Real temp git repos and the REAL orchestrator spawn path; the SDK factory is a fake
that records the options each spawn was given, so no ``claude`` ever runs."""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path
from unittest import mock
from unittest.mock import patch

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select

from viberpit import worktrees
from viberpit.selector import Picked
from server.app import create_app
from server.codex.capabilities import CODEX_CAPABILITIES
from server.db import db_session, dispose_db, init_db
from server.db.models import Session as DbSession
from server.ipc.client import DaemonClient, DaemonError
from server.ipc.daemon import create_daemon_app
from server.ipc.orchestrator_rpc import register_orchestrator_rpc
from server.sessions import fork as fk
from server.sessions.claude_driver import ClaudeAgentDriver
from server.sessions.engine import AgentEngine, capabilities_to_wire, fork_supported
from server.sessions.fleet_orchestrator import _PER_SESSION, FleetOrchestrator
from server.sessions.orchestrator import ModelBlockedError, SessionOrchestrator
from server.sessions.permissions import PermissionRegistry
from server.sessions.remote import FORK_RPC_TIMEOUT, RemoteOrchestrator
from server.sessions.translator import Translator
from server.sessions.types import SessionRecord
from server.tests.fakes.fake_sdk import FakeSDKClient
from server.tests.ipc_harness import serve_daemon_uds

_H = {"Tailscale-User-Login": "testuser@example.com"}
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
}


def _git(cwd: str | Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True, env={**os.environ, **_GIT_ENV}).stdout


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
    """A fake SDK factory recording every client it builds. ``fail_fork`` makes a
    fork spawn's client blow up on start, after the fork worktree exists."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.fail_fork = False

    def __call__(self, cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *,
                 on_pre_compact=None, options_overlay=None, **fork_kw):
        self.calls.append({"cwd": cwd, "resume": resume, **fork_kw})
        client = FakeSDKClient([], on_can_use_tool=can_use_tool)
        if self.fail_fork and "fork_session" in fork_kw:
            async def boom():
                raise RuntimeError("cli exited")
            client.__aenter__ = boom  # type: ignore[method-assign]
        return client


@pytest_asyncio.fixture
async def env(tmp_path: Path):
    """An orchestrator on a fake SDK, a clone under a repo root, and one source session
    (account alpha, model sonnet, full-auto, effort high) running in its own worktree,
    with uncommitted work, a known SDK session id and one settled reply anchor."""
    store = tmp_path / "store"; store.mkdir()
    shared = tmp_path / "dot-claude"; (shared / "projects").mkdir(parents=True)
    (shared / "settings.json").write_text("{}")
    cfg_dir = tmp_path / "cfg" / "alpha"; cfg_dir.mkdir(parents=True)
    (cfg_dir / ".claude.json").write_text("{}")
    repo_root = tmp_path / "reporoot"; repo_root.mkdir()
    clone = repo_root / "myrepo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(clone)], check=True)
    (clone / "README.md").write_text("hi\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "init")
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'fork.db'}"
    patches: list = [
        patch.dict(os.environ, {"VIBERPIT_HOME": str(store), "CLAUDE_CONFIG_DIR": str(shared),
                                "VIBERPIT_DB_URL": db_url}),
        patch("server.sessions.orchestrator.compose", return_value=cfg_dir),
        patch("server.sessions.orchestrator.configdir.trust_project"),
        patch("server.sessions.orchestrator.select_account", return_value=Picked("alpha")),
    ]
    for p in patches:
        p.start()
    _seed_account("alpha")
    await init_db(db_url=db_url)
    spawns = Spawns()
    orch = SessionOrchestrator(sdk_factory=spawns, poll_interval=9999.0,
                               usage_fn=_fake_usage, repo_root=str(repo_root))
    src = await orch.create_session("Source", cwd=str(clone), model="sonnet",
                                    permission_mode="full-auto", reasoning_effort="high")
    src.engine_session_id = "src-sdk"
    src.record_turn_anchor("t1", "uuid-1")
    Path(src.cwd, "README.md").write_text("edited\n")
    Path(src.cwd, "new.txt").write_text("untracked\n")
    try:
        yield orch, spawns, src, repo_root, clone
    finally:
        await orch.stop()
        await dispose_db()
        for p in reversed(patches):
            p.stop()


async def _fork(orch, source: str, **kw):
    """fork_session with a fresh request id (one per click) unless one is given."""
    kw.setdefault("request_id", str(uuid.uuid4()))
    return await orch.fork_session(source, **kw)


def _fork_branches(clone: Path) -> list[str]:
    return _git(clone, "branch", "--list", "--format=%(refname:short)", "*fork-*").split()


async def _rows_with_parent(parent: str) -> list[DbSession]:
    async with db_session() as db:
        return list((await db.execute(
            select(DbSession).where(DbSession.parent_session_id == parent))).scalars())


# --- orchestrator -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_fork_from_reply_resumes_the_source_as_a_fork(env) -> None:
    orch, spawns, src, repo_root, clone = env
    out = await _fork(orch, src.id, at_turn_id="t1", mode="reply", prefill="ignored")
    assert out["prefill"] is None
    child = orch.record(out["sessionId"])
    assert spawns.calls[-1] == {"cwd": child.cwd, "resume": "src-sdk", "fork_session": True,
                                "extra_args": {"resume-session-at": "uuid-1"}}
    # The child record: linked, titled, and on the source's account/engine/model/mode/host.
    assert child.parent_session_id == src.id
    assert child.name == "Fork of Source" and child.name_is_auto is False
    assert (child.account, child.engine, child.model, child.permission_mode,
            child.reasoning_effort, child.host) == (
        src.account, src.engine, src.model, src.permission_mode, src.reasoning_effort,
        src.host)
    # Its worktree: the standard session layout (janitor-swept), the source's current
    # files including uncommitted work, on a fork branch.
    assert Path(child.cwd) == worktrees.session_worktree_dir(
        str(repo_root), str(clone), child.id)
    assert Path(child.cwd, "README.md").read_text() == "edited\n"
    assert Path(child.cwd, "new.txt").read_text() == "untracked\n"
    assert child.branch == worktrees.fork_branch_name(src.id, 1)
    # Never the source's id: the child learns its own from the forked stream.
    assert child.engine_session_id == "" and child.fork_from == ("src-sdk", "uuid-1")
    (row,) = await _rows_with_parent(src.id)
    assert row.id == child.id and row.name == "Fork of Source" and row.cwd == child.cwd


@pytest.mark.asyncio
async def test_fork_from_message_cuts_at_the_previous_reply_and_echoes_prefill(env) -> None:
    orch, spawns, src, *_ = env
    out = await _fork(orch, src.id, at_turn_id="t1", mode="message",
                                  prefill="try it the other way")
    assert out["prefill"] == "try it the other way"
    assert spawns.calls[-1]["resume"] == "src-sdk"
    assert spawns.calls[-1]["extra_args"] == {"resume-session-at": "uuid-1"}


@pytest.mark.asyncio
async def test_fork_from_the_first_message_is_a_fresh_session(env) -> None:
    orch, spawns, src, *_ = env
    out = await _fork(orch, src.id, at_turn_id=None, mode="message", prefill="hello")
    assert out["prefill"] == "hello"
    child = orch.record(out["sessionId"])
    assert spawns.calls[-1] == {"cwd": child.cwd, "resume": None}
    assert child.parent_session_id == src.id and child.fork_from is None
    assert Path(child.cwd, "new.txt").exists()


@pytest.mark.asyncio
async def test_prefill_over_the_cap_is_refused(env) -> None:
    orch, _, src, *_ = env
    with pytest.raises(fk.ForkError) as ei:
        await _fork(orch, src.id, at_turn_id=None, mode="message",
                                prefill="x" * (fk.PREFILL_MAX_CHARS + 1))
    assert ei.value.code == fk.PREFILL_TOO_LONG
    assert fk.PREFILL_MAX_CHARS == 10_000


@pytest.mark.parametrize(("turn", "mode"), [("nope", "reply"), (None, "reply")])
@pytest.mark.asyncio
async def test_a_turn_without_an_anchor_is_not_forkable(env, turn, mode) -> None:
    orch, spawns, src, repo_root, clone = env
    before = len(spawns.calls)
    with pytest.raises(fk.ForkError) as ei:
        await _fork(orch, src.id, at_turn_id=turn, mode=mode)
    assert ei.value.code == fk.TURN_NOT_FORKABLE
    assert ei.value.error_data == {"code": fk.TURN_NOT_FORKABLE}
    assert len(spawns.calls) == before and _fork_branches(clone) == []


@pytest.mark.asyncio
async def test_a_turn_still_in_flight_is_not_forkable(env) -> None:
    """The live turn id, and a replay id naming the running turn's latest entry
    (which resolve_turn_anchor CAN anchor), are both refused while it runs."""
    orch, _, src, _, clone = env
    tr = orch._drivers[src.id]._tr
    tr._turn_id = "live-turn"
    tr._turn_entry_uuids = {"uuid-2"}
    src.record_turn_anchor("live-turn", "uuid-2")   # e.g. a steered turn's first segment
    src.record_turn_anchor("uuid-2", "uuid-2")      # the replay id of the running turn
    src.record_turn_anchor("other-id", "uuid-2")    # any id whose anchor is a running entry
    for turn in ("live-turn", "uuid-2", "other-id"):
        with pytest.raises(fk.ForkError) as ei:
            await _fork(orch, src.id, at_turn_id=turn, mode="reply")
        assert ei.value.code == fk.TURN_NOT_FORKABLE
    assert _fork_branches(clone) == []
    # A settled earlier turn still forks while the source is busy.
    out = await _fork(orch, src.id, at_turn_id="t1", mode="reply")
    assert orch.record(out["sessionId"]).fork_from == ("src-sdk", "uuid-1")


@pytest.mark.asyncio
async def test_kill_switch_off_refuses_and_hides_the_capability(env) -> None:
    orch, _, src, *_ = env
    assert ClaudeAgentDriver.capabilities.fork is True
    assert capabilities_to_wire(ClaudeAgentDriver.capabilities)["fork"] is True
    with patch.dict(os.environ, {"VIBERPIT_FORK_ENABLED": "false"}):
        assert capabilities_to_wire(ClaudeAgentDriver.capabilities)["fork"] is False
        assert fork_supported(ClaudeAgentDriver.capabilities) is False
        with pytest.raises(fk.ForkError) as ei:
            await _fork(orch, src.id, at_turn_id="t1", mode="reply")
    assert ei.value.code == fk.FORK_UNAVAILABLE


@pytest.mark.asyncio
async def test_codex_is_not_forkable(env) -> None:
    orch, _, src, *_ = env
    assert CODEX_CAPABILITIES.fork is False
    assert capabilities_to_wire(CODEX_CAPABILITIES)["fork"] is False
    src.engine = AgentEngine.CODEX
    with pytest.raises(fk.ForkError) as ei:
        await _fork(orch, src.id, at_turn_id="t1", mode="reply")
    assert ei.value.code == fk.FORK_UNAVAILABLE


@pytest.mark.asyncio
async def test_unknown_session_is_a_keyerror(env) -> None:
    orch, *_ = env
    with pytest.raises(KeyError):
        await _fork(orch, "nope", at_turn_id="t1", mode="reply")


@pytest.mark.asyncio
async def test_unsafe_repo_config_is_refused_with_its_code(env) -> None:
    orch, _, src, _, clone = env
    _git(clone, "config", "includeIf.onbranch:main.path", "/dev/null")
    with pytest.raises(worktrees.ForkWorktreeError) as ei:
        await _fork(orch, src.id, at_turn_id="t1", mode="reply")
    assert ei.value.code == worktrees.FORK_UNSAFE_CONFIG
    assert ei.value.error_data == {"code": worktrees.FORK_UNSAFE_CONFIG}


@pytest.mark.parametrize("stage", ["driver_start", "before_driver"])
@pytest.mark.asyncio
async def test_a_failure_after_the_worktree_exists_leaves_no_half_fork(env, stage) -> None:
    orch, spawns, src, repo_root, clone = env
    made: list[str] = []
    real = worktrees.create_fork_worktree

    def spy(*a, **k):
        info = real(*a, **k)
        made.append(info.path)
        return info

    records_before = set(orch._records)
    with patch.object(worktrees, "create_fork_worktree", spy):
        if stage == "driver_start":
            spawns.fail_fork = True
            ctx = pytest.raises(RuntimeError, match="cli exited")
        else:
            ctx = pytest.raises(RuntimeError, match="no config")
            orch._driver_config_dir = mock.Mock(side_effect=RuntimeError("no config"))  # type: ignore[method-assign]
        with ctx:
            await _fork(orch, src.id, at_turn_id="t1", mode="reply")
    assert len(made) == 1 and not os.path.lexists(made[0])
    assert _fork_branches(clone) == []
    assert made[0] not in _git(clone, "worktree", "list", "--porcelain")
    assert set(orch._records) == records_before
    assert await _rows_with_parent(src.id) == []
    assert all(wt.path != made[0] for wt in orch._worktrees.values())


@pytest.mark.asyncio
async def test_child_rebuilds_keep_forking_until_its_own_id_arrives(
        env, caplog: pytest.LogCaptureFixture) -> None:
    """A driver rebuilt before the child's first turn resumes the source AS A FORK
    (never unforked into the source's transcript); the child's own id ends that."""
    orch, spawns, src, *_ = env
    child = orch.record((await _fork(orch, src.id, at_turn_id="t1",
                                                 mode="reply"))["sessionId"])
    cfg = orch._create_driver(engine=AgentEngine.CLAUDE_CODE, session_id=child.id,
                              account=child.account, cwd=child.cwd, config_dir="/x",
                              mode="interactive", identity="u", resume=None,
                              on_sync_back=lambda: None)
    assert (cfg.resume, cfg._fork_resume_at) == ("src-sdk", "uuid-1")
    with caplog.at_level(logging.ERROR, logger="server.sessions.orchestrator"):
        await orch._persist_engine_session_id(child.id, "src-sdk")  # never adopt it
    assert child.engine_session_id == "" and child.fork_from is not None
    (err,) = [r for r in caplog.records if "engine_session_id_rejected" in r.getMessage()]
    assert err.levelno == logging.ERROR and "reason=fork_source" in err.getMessage()
    await orch._persist_engine_session_id(child.id, "child-sdk")
    assert child.engine_session_id == "child-sdk" and child.fork_from is None
    cfg2 = orch._create_driver(engine=AgentEngine.CLAUDE_CODE, session_id=child.id,
                               account=child.account, cwd=child.cwd, config_dir="/x",
                               mode="interactive", identity="u", resume="child-sdk",
                               on_sync_back=lambda: None)
    assert (cfg2.resume, cfg2._fork_resume_at) == ("child-sdk", None)


# --- idempotency -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_same_request_id_returns_the_same_child_once(env) -> None:
    orch, spawns, src, _, clone = env
    rid = str(uuid.uuid4())
    first = await orch.fork_session(src.id, at_turn_id="t1", mode="reply", request_id=rid)
    n_spawns = len(spawns.calls)
    again = await orch.fork_session(src.id, at_turn_id="t1", mode="reply", request_id=rid)
    assert again == first and len(spawns.calls) == n_spawns
    assert first["sessionId"] == fk.fork_child_id(src.id, rid)
    assert _fork_branches(clone) == [worktrees.fork_branch_name(src.id, 1)]
    # Still the same child once it is gone from memory (closed, archived, restarted).
    orch._records.pop(first["sessionId"])
    assert (await orch.fork_session(src.id, at_turn_id="t1", mode="reply",
                                    request_id=rid)) == first
    other = await orch.fork_session(src.id, at_turn_id="t1", mode="reply",
                                    request_id=str(uuid.uuid4()))
    assert other["sessionId"] != first["sessionId"]
    assert len(_fork_branches(clone)) == 2


@pytest.mark.asyncio
async def test_concurrent_duplicates_make_one_child(env) -> None:
    orch, spawns, src, _, clone = env
    rid = str(uuid.uuid4())
    before = len(spawns.calls)
    a, b = await asyncio.gather(
        orch.fork_session(src.id, at_turn_id="t1", mode="message", prefill="p",
                          request_id=rid),
        orch.fork_session(src.id, at_turn_id="t1", mode="message", prefill="p",
                          request_id=rid))
    assert a == b == {"sessionId": fk.fork_child_id(src.id, rid), "prefill": "p"}
    assert len(spawns.calls) == before + 1 and len(_fork_branches(clone)) == 1
    assert len(await _rows_with_parent(src.id)) == 1


# --- admission --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_global_cap_refuses_before_any_worktree(env) -> None:
    orch, spawns, src, _, clone = env
    before = len(spawns.calls)
    with patch.object(orch, "_max_active_sessions", mock.AsyncMock(return_value=1)):
        with pytest.raises(fk.ForkError) as ei:
            await _fork(orch, src.id, at_turn_id="t1", mode="reply")
    assert ei.value.code == fk.FORK_AT_CAPACITY
    assert len(spawns.calls) == before and _fork_branches(clone) == []


@pytest.mark.asyncio
async def test_product_cap_refuses(env) -> None:
    orch, _, src, _, clone = env
    src.repo = "o/r"
    seen: list = []

    async def full(repo, *, context=None):
        seen.append(repo)
        return ("prod", 2, 2)

    with patch("server.automations.product_admission.repo_capacity_status", full):
        with pytest.raises(fk.ForkError) as ei:
            await _fork(orch, src.id, at_turn_id="t1", mode="reply")
    assert ei.value.code == fk.FORK_AT_CAPACITY and seen == ["o/r"]
    assert _fork_branches(clone) == []


@pytest.mark.asyncio
async def test_model_needing_approval_is_refused(env) -> None:
    orch, _, src, _, clone = env
    with patch("server.sessions.model_policy.resolve_model_policy",
               mock.AsyncMock(return_value="require_approval")):
        with pytest.raises(fk.ForkError) as ei:
            await _fork(orch, src.id, at_turn_id="t1", mode="reply")
    assert ei.value.code == fk.FORK_NEEDS_APPROVAL and _fork_branches(clone) == []


@pytest.mark.asyncio
async def test_blocked_model_is_model_blocked_error(env) -> None:
    orch, _, src, _, clone = env
    with patch("server.sessions.model_policy.resolve_model_policy",
               mock.AsyncMock(return_value="block")):
        with pytest.raises(ModelBlockedError) as ei:
            await _fork(orch, src.id, at_turn_id="t1", mode="reply")
    assert ei.value.error_data == {"model": ei.value.model}   # an INFO refusal over RPC
    assert _fork_branches(clone) == []


async def _own_source(src_id: str, *, released: bool = False,
                      transferred: bool = False) -> str:
    """A real ownership operation (the automation store) whose root session is the
    fork source. *released* finishes it; *transferred* hands it to a successor that
    is still ``claimed`` (the transfer window). Returns the source operation's token."""
    from server.automations.ownership import ReleaseProof, owned
    from server.db.models import AutomationOperation
    from server.tests.test_automation_ownership import claim, target
    token = await claim([target()])
    if released:
        async with owned(token) as tx:
            tx.release(ReleaseProof.NOT_STARTED, "done")
    if transferred:
        async with owned(token) as tx:
            await tx.transfer(owner="host-b:pr-doctor", source="pr-doctor",
                              proof=ReleaseProof.NOT_STARTED, reason="handoff")
    async with db_session() as db:
        op = await db.get(AutomationOperation, token)
        assert op is not None
        op.session_id = src_id
        await db.commit()
    return token


async def _operation(token: str):
    from server.db.models import AutomationOperation
    async with db_session() as db:
        op = await db.get(AutomationOperation, token)
        assert op is not None
        return op


@pytest.mark.asyncio
async def test_a_live_owner_refuses_the_fork_before_any_git_work(env) -> None:
    from server.automations.ownership import (
        ReleaseProof, attest_session_stopped, begin_session_execution, execution_snapshot,
        owned)
    orch, spawns, src, _, clone = env
    token = await _own_source(src.id)
    before = len(spawns.calls)
    with patch.object(worktrees, "create_fork_worktree") as made:
        with pytest.raises(fk.ForkError) as ei:
            await _fork(orch, src.id, at_turn_id="t1", mode="reply")
    assert ei.value.code == fk.FORK_OWNED_BY_AUTOMATION
    assert ei.value.error_data == {"code": fk.FORK_OWNED_BY_AUTOMATION}
    assert "fork it after the run finishes" in str(ei.value)
    made.assert_not_called()
    assert len(spawns.calls) == before and _fork_branches(clone) == []
    op = await _operation(token)
    assert set(op.execution_sessions or {}) <= {src.id}       # no pending fork entry
    # Orphan check: the refused fork left nothing that blocks the stopped-work proof.
    await begin_session_execution(src.id)
    snap = await execution_snapshot(src.id)
    assert snap is not None and snap.generation is not None
    assert await attest_session_stopped(token, snap.generation, session_id=src.id)
    async with owned(token) as tx:
        tx.release(ReleaseProof.WORK_STOPPED, "run finished")
    assert (await _operation(token)).phase == "released"


@pytest.mark.asyncio
async def test_a_finished_owner_does_not_block_the_fork(env) -> None:
    """A finished (released) Backlog Runner / PR Doctor source: the fork child is
    unowned, so it registers nothing and its driver's ownership fence returns early."""
    from server.automations.ownership import begin_session_execution, lineage_unowned
    orch, spawns, src, *_ = env
    token = await _own_source(src.id, released=True)
    gen_before = (await _operation(token)).execution_generation
    out = await _fork(orch, src.id, at_turn_id="t1", mode="reply")
    child = orch.record(out["sessionId"])
    assert child.parent_session_id == src.id and out["sessionId"] in orch._drivers
    assert spawns.calls[-1]["fork_session"] is True
    assert await lineage_unowned(child.id)
    await begin_session_execution(child.id)                      # early return, no raise
    op = await _operation(token)
    assert child.id not in (op.execution_sessions or {})
    assert op.execution_generation == gen_before


@pytest.mark.asyncio
async def test_the_transfer_window_still_owns_the_source(env) -> None:
    """Root transferred, successor still claimed: the lineage is owned."""
    from server.automations.ownership import lineage_unowned
    orch, _, src, _, clone = env
    await _own_source(src.id, transferred=True)
    assert not await lineage_unowned("new-child", parent_session_id=src.id)
    with pytest.raises(fk.ForkError) as ei:
        await _fork(orch, src.id, at_turn_id="t1", mode="reply")
    assert ei.value.code == fk.FORK_OWNED_BY_AUTOMATION and _fork_branches(clone) == []


@pytest.mark.asyncio
async def test_a_live_ancestor_above_a_finished_level_still_owns_the_lineage(env) -> None:
    """Every ancestor level is walked: grandchild G's parent B has only a finished
    (transferred → released) operation, but B's parent A is held by a live one. G is
    not unowned, and begin_session_execution fences it."""
    from server.automations.ownership import (
        ReleaseProof, StaleOwnership, begin_session_execution, lineage_unowned, owned)
    from server.db.models import AutomationOperation
    from server.tests.test_automation_ownership import claim, target
    async with db_session() as db:
        db.add(DbSession(id="A", name="A", account_name="alpha", cwd="/tmp", status="idle"))
        db.add(DbSession(id="B", name="B", account_name="alpha", cwd="/tmp", status="idle",
                         parent_session_id="A"))
        await db.commit()
    live = await claim([target("I_live", "o/r#1")])
    b_op = await claim([target("I_b", "o/r#2")])
    async with owned(b_op) as tx:
        successor = await tx.transfer(owner="host-b:x", source="x",
                                      proof=ReleaseProof.NOT_STARTED, reason="handoff")
    async with owned(successor) as tx:
        tx.release(ReleaseProof.NOT_STARTED, "done")
    async with db_session() as db:
        (await db.get(AutomationOperation, live)).session_id = "A"     # type: ignore[union-attr]
        (await db.get(AutomationOperation, b_op)).session_id = "B"     # type: ignore[union-attr]
        await db.commit()
    assert (await _operation(b_op)).phase == "transferred"
    assert not await lineage_unowned("G", parent_session_id="B")
    with pytest.raises(StaleOwnership):
        await begin_session_execution("G", parent_session_id="B")
    # With A's operation finished too, the same grandchild is unowned.
    async with owned(live) as tx:
        tx.release(ReleaseProof.NOT_STARTED, "done")
    assert await lineage_unowned("G", parent_session_id="B")


@pytest.mark.asyncio
async def test_retry_after_a_failure_returns_the_same_child(env) -> None:
    orch, spawns, src, _, clone = env
    await _own_source(src.id, released=True)
    rid = str(uuid.uuid4())
    spawns.fail_fork = True
    with pytest.raises(RuntimeError, match="cli exited"):
        await orch.fork_session(src.id, at_turn_id="t1", mode="reply", request_id=rid)
    assert _fork_branches(clone) == []
    spawns.fail_fork = False
    out = await orch.fork_session(src.id, at_turn_id="t1", mode="reply", request_id=rid)
    assert out["sessionId"] == fk.fork_child_id(src.id, rid)
    assert orch.record(out["sessionId"]).parent_session_id == src.id


@pytest.mark.asyncio
async def test_an_owner_going_live_mid_fork_is_a_coded_refusal(env) -> None:
    """An operation that goes live between admission and the child driver's
    ownership fence: FORK_OWNERSHIP_STALE, and the fork worktree is gone."""
    from server.automations.ownership import StaleOwnership
    orch, _, src, _, clone = env
    with patch("server.automations.ownership.begin_session_execution",
               mock.AsyncMock(side_effect=StaleOwnership("claimed meanwhile"))):
        with pytest.raises(fk.ForkError) as ei:
            await _fork(orch, src.id, at_turn_id="t1", mode="reply")
    assert ei.value.code == fk.FORK_OWNERSHIP_STALE
    assert _fork_branches(clone) == [] and await _rows_with_parent(src.id) == []


# --- restart durability and cancellation --------------------------------------------

@pytest.mark.asyncio
async def test_a_record_rebuilt_from_its_row_still_forks(env) -> None:
    orch, spawns, src, *_ = env
    child_id = (await _fork(orch, src.id, at_turn_id="t1", mode="reply"))["sessionId"]
    async with db_session() as db:
        row = await db.get(DbSession, child_id)
    assert row is not None
    assert (row.fork_source_engine_session_id, row.fork_resume_at) == ("src-sdk", "uuid-1")
    # A daemon restart before the child's first turn: the record comes back from the row.
    driver = orch._drivers.pop(child_id)
    await driver.aclose()
    orch._records[child_id] = SessionOrchestrator._record_from_row(row)
    assert orch._records[child_id].fork_from == ("src-sdk", "uuid-1")
    rebuilt = orch._create_driver(engine=AgentEngine.CLAUDE_CODE, session_id=child_id,
                                  account="alpha", cwd=row.cwd, config_dir="/x",
                                  mode="interactive", identity="u",
                                  resume=orch._records[child_id].engine_session_id or None,
                                  on_sync_back=lambda: None)
    await rebuilt.start()
    assert spawns.calls[-1]["resume"] == "src-sdk"
    assert spawns.calls[-1]["fork_session"] is True
    assert spawns.calls[-1]["extra_args"] == {"resume-session-at": "uuid-1"}
    await rebuilt.aclose()
    # The child's own id clears the pending fork in the same write.
    await orch._persist_engine_session_id(child_id, "child-sdk")
    async with db_session() as db:
        row = await db.get(DbSession, child_id)
    assert row is not None
    assert (row.engine_session_id, row.fork_source_engine_session_id,
            row.fork_resume_at) == ("child-sdk", None, None)


@pytest.mark.asyncio
async def test_cancelled_while_making_the_worktree_discards_it(env) -> None:
    orch, spawns, src, _, clone = env
    entered, release = threading.Event(), threading.Event()
    made: list[str] = []
    real = worktrees.create_fork_worktree

    def slow(*a, **k):
        entered.set()
        release.wait(30)
        info = real(*a, **k)
        made.append(info.path)
        return info

    before = len(spawns.calls)
    with patch.object(worktrees, "create_fork_worktree", slow):
        task = asyncio.ensure_future(_fork(orch, src.id, at_turn_id="t1", mode="reply"))
        while not entered.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.05)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(made) == 1 and not os.path.lexists(made[0])
    assert _fork_branches(clone) == [] and len(spawns.calls) == before
    assert await _rows_with_parent(src.id) == []


# --- driver + translator ----------------------------------------------------------

@pytest.mark.asyncio
async def test_driver_fork_start_passes_fork_options_and_never_repairs_the_source() -> None:
    calls: list[tuple] = []

    def factory(cwd, config_dir, can_use_tool, mode, resume, **kw):
        calls.append((resume, kw))
        return FakeSDKClient([])

    with patch("server.sessions.claude_driver.repair_resumed_transcript") as repair:
        d = ClaudeAgentDriver("s", "a", "/tmp", "/cfg", mode="interactive",
                              sdk_factory=factory, registry=PermissionRegistry(),
                              identity="u", resume="src-sdk", fork_resume_at="uuid-1")
        await d.start()
        await d.aclose()
        plain = ClaudeAgentDriver("s2", "a", "/tmp", "/cfg", mode="interactive",
                                  sdk_factory=factory, registry=PermissionRegistry(),
                                  identity="u", resume="src-sdk")
        await plain.start()
        await plain.aclose()
    assert calls[0] == ("src-sdk", {"on_pre_compact": d._on_pre_compact, "fork_session": True,
                                    "extra_args": {"resume-session-at": "uuid-1"}})
    assert calls[1] == ("src-sdk", {"on_pre_compact": plain._on_pre_compact})
    assert [c.args[1] for c in repair.call_args_list] == ["src-sdk"]   # the plain resume only


def test_translator_in_flight_ids_cover_the_open_turn_only() -> None:
    from server.tests.fakes.fake_sdk import assistant, result, stream_start, tool_use
    from claude_agent_sdk import TextBlock
    tr = Translator()
    assert tr.in_flight_ids() == frozenset()
    tr.feed(stream_start("s1"))
    tr.feed(tool_use("tu", "Bash", {"command": "ls"}, uuid="a1"))
    tr.feed(assistant(TextBlock("ok"), uuid="a2"))
    assert tr.in_flight_ids() == {"s1", "a1", "a2"}
    tr.feed(result())
    assert tr.in_flight_ids() == frozenset()


@pytest.mark.asyncio
async def test_real_sdk_factory_builds_fork_options(tmp_path) -> None:
    from server.sessions import claude_sdk_factory as f
    captured: dict = {}

    class Opts:
        def __init__(self, **kw):
            captured.update(kw)

    class Client:
        def __init__(self, options):
            self.options = options

    with patch.object(f, "_import_sdk", return_value=(Client, Opts)), \
            patch.object(f, "_cli_path_kwargs", return_value={}):
        await f.real_sdk_factory(str(tmp_path), str(tmp_path), None, "interactive", "src-sdk",
                                 fork_session=True,
                                 extra_args={"resume-session-at": "uuid-1"})
        assert (captured["resume"], captured["fork_session"], captured["extra_args"]) == (
            "src-sdk", True, {"resume-session-at": "uuid-1"})
        await f.real_sdk_factory(str(tmp_path), str(tmp_path), None, "interactive", None)
        assert (captured["fork_session"], captured["extra_args"]) == (False, {})


# --- RPC / remote / fleet / facade ------------------------------------------------

@pytest.mark.asyncio
async def test_rpc_round_trip_creates_the_child_on_the_owner_daemon(
        env, caplog: pytest.LogCaptureFixture) -> None:
    orch, spawns, src, *_ = env
    caplog.set_level(logging.INFO)
    app = create_daemon_app()
    register_orchestrator_rpc(app, orch)
    async with serve_daemon_uds(app) as sock:
        client = DaemonClient(sock)
        try:
            ro = RemoteOrchestrator(client)
            out = await _fork(ro, src.id, at_turn_id="t1", mode="message",
                                        prefill="again", identity="u@x")
            with pytest.raises(fk.ForkError) as not_forkable:
                await _fork(ro, src.id, at_turn_id="nope", mode="reply")
            with pytest.raises(KeyError):
                await _fork(ro, "nope", at_turn_id="t1", mode="reply")
            with patch.dict(os.environ, {"VIBERPIT_FORK_ENABLED": "false"}):
                with pytest.raises(fk.ForkError) as off:
                    await _fork(ro, src.id, at_turn_id="t1", mode="reply")
        finally:
            await client.aclose()
    assert out["prefill"] == "again"
    child = orch.record(out["sessionId"])            # created on the daemon's orchestrator
    assert child.parent_session_id == src.id
    assert spawns.calls[-1]["extra_args"] == {"resume-session-at": "uuid-1"}
    assert not_forkable.value.code == fk.TURN_NOT_FORKABLE
    assert off.value.code == fk.FORK_UNAVAILABLE
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == [], [(r.name, r.getMessage()) for r in warnings]
    rpc_errors = [r for r in caplog.records if "rpc.error" in r.getMessage()]
    assert len(rpc_errors) == 3


class _RecordingClient:
    def __init__(self, result=None, error: Exception | None = None):
        self.result, self.error = result, error
        self.calls: list[tuple[str, dict, float | None]] = []

    async def call(self, method, *, timeout=None, **kwargs):
        self.calls.append((method, kwargs, timeout))
        if self.error is not None:
            raise self.error
        return self.result


@pytest.mark.asyncio
async def test_remote_forwards_kwargs_with_the_spawn_budget() -> None:
    client = _RecordingClient(result={"sessionId": "c", "prefill": None})
    await RemoteOrchestrator(client).fork_session("s", at_turn_id="t", mode="reply",
                                                  request_id="r")
    assert client.calls == [("fork_session", {"session_id": "s", "at_turn_id": "t",
                                              "mode": "reply", "request_id": "r",
                                              "prefill": None, "identity": "system"},
                             FORK_RPC_TIMEOUT)]
    from viberpit.worktrees import FORK_GIT_TIMEOUT_S
    from viberpit.worktree_diff import GIT_TIMEOUT_S
    from server.sessions.remote import SPAWN_RPC_TIMEOUT
    assert FORK_RPC_TIMEOUT > 4 * FORK_GIT_TIMEOUT_S + 12 * GIT_TIMEOUT_S + SPAWN_RPC_TIMEOUT


@pytest.mark.parametrize(("err_type", "code", "exc"), [
    ("ForkError", fk.TURN_NOT_FORKABLE, fk.ForkError),
    ("ForkError", fk.FORK_UNAVAILABLE, fk.ForkError),
    ("ForkWorktreeError", worktrees.FORK_UNSAFE_CONFIG, worktrees.ForkWorktreeError),
])
@pytest.mark.asyncio
async def test_remote_rebuilds_known_codes(err_type, code, exc) -> None:
    ro = RemoteOrchestrator(_RecordingClient(error=DaemonError(err_type, "m", {"code": code})))
    with pytest.raises(exc) as ei:
        await _fork(ro, "s", at_turn_id="t", mode="reply")
    assert ei.value.code == code


@pytest.mark.asyncio
async def test_remote_rebuilds_model_blocked_and_account_refusals() -> None:
    from server.sessions.selection import AllAccountsBusy
    ro = RemoteOrchestrator(_RecordingClient(error=DaemonError(
        "ModelBlockedError", "model 'opus' is blocked by policy", {"model": "opus"})))
    with pytest.raises(ModelBlockedError) as mb:
        await _fork(ro, "s", at_turn_id="t", mode="reply")
    assert mb.value.model == "opus"
    ro = RemoteOrchestrator(_RecordingClient(error=DaemonError(
        "AllAccountsBusy", "busy", {"kind": "no_usable_accounts"})))
    with pytest.raises(AllAccountsBusy):
        await _fork(ro, "s", at_turn_id="t", mode="reply")


@pytest.mark.parametrize("err_type", ["ForkError", "ForkWorktreeError"])
@pytest.mark.asyncio
async def test_remote_does_not_invent_unknown_codes(err_type) -> None:
    ro = RemoteOrchestrator(_RecordingClient(error=DaemonError(err_type, "m", {"code": "WAT"})))
    with pytest.raises(DaemonError):
        await _fork(ro, "s", at_turn_id="t", mode="reply")


@pytest.mark.asyncio
async def test_fleet_routes_the_fork_to_the_owning_host() -> None:
    assert "fork_session" in _PER_SESSION

    class _Host:
        def __init__(self):
            self.calls: list = []

        async def fork_session(self, session_id, **kw):
            self.calls.append((session_id, kw))
            return {"sessionId": "c", "prefill": None}

    class _Router:
        def __init__(self, orchs):
            self._orchs = orchs

        def get(self, name):
            return self._orchs[name]

        def names(self):
            return sorted(self._orchs)

    dobby, rocky = _Host(), _Host()
    fo = FleetOrchestrator(_Router({"dobby": dobby, "rocky": rocky}), primary="dobby")  # type: ignore[arg-type]
    fo._host_cache["s"] = "rocky"
    await fo.fork_session("s", at_turn_id="t", mode="reply", request_id="r")
    assert rocky.calls == [("s", {"at_turn_id": "t", "mode": "reply", "request_id": "r"})]
    assert dobby.calls == []


# --- HTTP -------------------------------------------------------------------------

_ENV = {"VIBERPIT_DB_URL": "sqlite+aiosqlite:///:memory:", "VIBERPIT_DEV": "1",
        "VIBERPIT_TAILNET_ALLOWLIST": ""}
SID = "33333333-3333-4333-8333-333333333333"


def _client():
    with patch.dict(os.environ, _ENV):
        orch = SessionOrchestrator(sdk_factory=Spawns(), poll_interval=9999.0,
                                   usage_fn=lambda name: None)
        app = create_app(orchestrator=orch)
    orch._records[SID] = SessionRecord(id=SID, name="n", account="a",
                                       engine=AgentEngine.CLAUDE_CODE, engine_session_id="c",
                                       cols=80, rows=24, cwd=None)  # type: ignore[arg-type]
    return TestClient(app, raise_server_exceptions=False)


def _post(c, body, sid=SID):
    body = {"requestId": str(uuid.uuid4()), **body}
    return c.post(f"/api/sessions/{sid}/fork", json=body, headers=_H)


def test_http_fork_is_201_with_session_id_and_prefill() -> None:
    rid = str(uuid.uuid4())

    async def fake(self, session_id, *, at_turn_id, mode, request_id, prefill=None,
                   identity="system"):
        assert (session_id, at_turn_id, mode, request_id, identity) == (
            SID, "t1", "message", rid, "testuser@example.com")
        return {"sessionId": "child", "prefill": prefill}

    with patch.dict(os.environ, _ENV), _client() as c, \
            patch.object(SessionOrchestrator, "fork_session", fake):
        r = _post(c, {"atTurnId": "t1", "mode": "message", "prefill": "hi",
                      "requestId": rid})
    assert r.status_code == 201, r.text
    assert r.json() == {"sessionId": "child", "prefill": "hi"}


def test_http_unknown_session_404() -> None:
    with patch.dict(os.environ, _ENV), _client() as c:
        assert _post(c, {"atTurnId": "t1", "mode": "reply"}, sid="nope").status_code == 404


def test_http_bad_mode_422() -> None:
    with patch.dict(os.environ, _ENV), _client() as c:
        assert _post(c, {"atTurnId": "t1", "mode": "sideways"}).status_code == 422


def test_http_prefill_too_long_400() -> None:
    with patch.dict(os.environ, _ENV), _client() as c:
        r = _post(c, {"atTurnId": None, "mode": "message", "prefill": "x" * 10_001})
    assert r.status_code == 400 and r.json()["detail"]["code"] == fk.PREFILL_TOO_LONG


def test_http_turn_not_forkable_409() -> None:
    with patch.dict(os.environ, _ENV), _client() as c:
        r = _post(c, {"atTurnId": "nope", "mode": "reply"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == fk.TURN_NOT_FORKABLE


def test_http_kill_switch_409() -> None:
    with patch.dict(os.environ, {**_ENV, "VIBERPIT_FORK_ENABLED": "false"}), _client() as c:
        r = _post(c, {"atTurnId": "t1", "mode": "reply"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == fk.FORK_UNAVAILABLE


@pytest.mark.parametrize("code", [worktrees.FORK_UNSAFE_CONFIG, worktrees.FORK_NOT_A_REPO])
def test_http_fork_worktree_error_409_with_its_code(code) -> None:
    with patch.dict(os.environ, _ENV), _client() as c, \
            patch.object(SessionOrchestrator, "fork_session",
                         side_effect=worktrees.ForkWorktreeError("m", code)):
        r = _post(c, {"atTurnId": "t1", "mode": "reply"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == code


def test_http_blocked_model_and_busy_accounts_map_like_create() -> None:
    from server.sessions.selection import AllAccountsBusy
    with patch.dict(os.environ, _ENV), _client() as c:
        with patch.object(SessionOrchestrator, "fork_session",
                          side_effect=ModelBlockedError("opus")):
            r = _post(c, {"atTurnId": "t1", "mode": "reply"})
        assert r.status_code == 409 and r.json() == {"detail": "model blocked by policy"}
        with patch.object(SessionOrchestrator, "fork_session",
                          side_effect=AllAccountsBusy(None, "no_usable_accounts")):
            r = _post(c, {"atTurnId": "t1", "mode": "reply"})
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "accounts_need_reauth"


def test_http_request_id_is_required() -> None:
    with patch.dict(os.environ, _ENV), _client() as c:
        r = c.post(f"/api/sessions/{SID}/fork", json={"atTurnId": "t1", "mode": "reply"},
                   headers=_H)
    assert r.status_code == 422


def test_http_old_daemon_is_daemon_outdated_409() -> None:
    with patch.dict(os.environ, _ENV), _client() as c, \
            patch.object(SessionOrchestrator, "fork_session",
                         side_effect=DaemonError("UnknownMethod", "fork_session")):
        r = _post(c, {"atTurnId": "t1", "mode": "reply"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "daemon_outdated"


def test_http_requires_identity() -> None:
    with patch.dict(os.environ, _ENV), _client() as c:
        assert c.post(f"/api/sessions/{SID}/fork",
                      json={"atTurnId": "t1", "mode": "reply",
                            "requestId": str(uuid.uuid4())}).status_code == 401


def test_config_endpoint_reports_the_gated_capability() -> None:
    from server.api.config import _capabilities_for
    assert _capabilities_for(AgentEngine.CLAUDE_CODE)["fork"] is True
    with patch.dict(os.environ, {"VIBERPIT_FORK_ENABLED": "false"}):
        assert _capabilities_for(AgentEngine.CLAUDE_CODE)["fork"] is False
