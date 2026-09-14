"""Orchestrator consolidation: candidate enumeration + de-duped session spawn.
Uses the __new__ + injected-fakes harness from test_pr_doctor_step.py."""
from __future__ import annotations

import asyncio
import pytest

from server.github.pr_status import PullRequestInfo
from server.tests.test_pr_doctor_step import github_identity as github_identity
from server.tests.test_pr_doctor_step import _install_spawn_recorder


@pytest.fixture(autouse=True)
def consolidation_github(monkeypatch, github_identity):
    from unittest.mock import AsyncMock
    from server.automations.ownership import CanonicalTarget

    async def resolve(refs, *, kind, include_linked_issues=False):
        assert kind == "pr" and include_linked_issues
        return tuple(target for ref in refs for target in (
            CanonicalTarget("PR_" + ref.split("#")[1], ref),
            CanonicalTarget("I_" + ref.split("#")[1], ref.replace("#", "#10"))))

    monkeypatch.setattr("server.automations.target_identity.resolve_targets", resolve)
    monkeypatch.setattr("server.sessions.orchestrator.SessionOrchestrator._sdlc_default_branch",
                        AsyncMock(return_value="main"))


def _pr(n, *, title="fix(deploy): x", state="open", is_draft=False,
        checks="passing", mergeable="MERGEABLE"):
    return PullRequestInfo(number=n, url=f"https://x/pull/{n}", state=state,
                           checks=checks, title=title, is_draft=is_draft,
                           mergeable=mergeable, head_ref=f"feat/{n}")


class _Population:
    def __init__(self, prs, repo="o/r"):
        self._prs, self._repo = prs, repo

    def all(self):
        return {self._repo: list(self._prs)}

    def cached(self, repo):
        return list(self._prs) if repo == self._repo else ()


async def _init_db(tmp_path):
    from server.db import init_db
    from server.automations.registry import registry
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'c.db'}")
    await registry.ensure_rows()


def _build_orch(prs):
    import server.sessions.orchestrator as orch_mod
    from server.automations.context import AutomationContext
    orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    orch._records = {}
    orch._drivers = {}
    orch._pr_population = _Population(prs)          # type: ignore[assignment]
    orch._pr_doctor_ctx = AutomationContext("pr-doctor")
    orch._consolidation_tasks = set()
    return orch


async def _drain_consolidations(orch) -> None:
    """Await the background consolidation tasks a 'spawning' return scheduled."""
    await asyncio.gather(*orch._consolidation_tasks)


@pytest.mark.asyncio
async def test_candidates_groups_open_prs_and_suggests_clusters(tmp_path):
    await _init_db(tmp_path)
    orch = _build_orch([_pr(1), _pr(2), _pr(9, state="draft", is_draft=True)])
    out = await orch.consolidation_candidates()
    repo = out["repos"]["o/r"]
    nums = {c["number"] for c in repo["candidates"]}
    assert nums == {1, 2}                            # draft excluded
    assert repo["clusters"][0]["numbers"] == [1, 2]  # same scope → cluster


@pytest.mark.asyncio
async def test_candidates_reports_active_consolidations(tmp_path):
    """A live consolidation session for a repo surfaces in that repo's `active`
    list (session id + covered PR numbers); archived/ended sessions and
    non-consolidation pr-doctor sessions do not."""
    from server.db import db_session
    from server.db.models import Session
    await _init_db(tmp_path)
    orch = _build_orch([_pr(1), _pr(2), _pr(3)])
    async with db_session() as db:
        db.add(Session(id="live", name="c", account_name="a", status="idle",
                       cwd=".", origin="pr-doctor", origin_ref="consolidate:o/r#1+2"))
        db.add(Session(id="old", name="c", account_name="a", status="archived",
                       cwd=".", origin="pr-doctor", origin_ref="consolidate:o/r#2+3"))
        db.add(Session(id="fix", name="c", account_name="a", status="idle",
                       cwd=".", origin="pr-doctor", origin_ref="pr:o/r#3"))
    out = await orch.consolidation_candidates()
    assert out["repos"]["o/r"]["active"] == [
        {"session_id": "live", "numbers": [1, 2]}]


@pytest.mark.asyncio
async def test_consolidate_spawns_one_deduped_session(tmp_path, monkeypatch):
    await _init_db(tmp_path)
    orch = _build_orch([_pr(1), _pr(2)])

    spawns = []
    _install_spawn_recorder(orch, spawns)
    # The RPC-facing call answers immediately; clone/model/spawn run in a
    # background task so the edge RPC can never time out into a 503 (issue #567).
    status, sid = await orch._pr_doctor_consolidate("o/r", [2, 1])
    assert status == "spawning" and sid is None
    await _drain_consolidations(orch)
    assert spawns[0]["dedupe_key"] == "consolidate:o/r#1+2"   # sorted, namespaced
    assert "Closes #1" in spawns[0]["initial_prompt"]
    assert "Closes #2" in spawns[0]["initial_prompt"]
    from server.automations.ownership import owned
    async with owned(spawns[0]["ownership_token"]) as tx:
        assert set(tx.operation.targets) == {"github:PR_1", "github:PR_2", "github:I_1", "github:I_2"}



@pytest.mark.asyncio
async def test_consolidate_passes_pinned_session_model(tmp_path, monkeypatch):
    await _init_db(tmp_path)
    orch = _build_orch([_pr(1), _pr(2)])

    async def config():
        return {"default_model": "fable"}
    orch._pr_doctor_config = config  # type: ignore[method-assign]

    spawns = []
    _install_spawn_recorder(orch, spawns)
    status, _ = await orch._pr_doctor_consolidate("o/r", [1, 2])
    assert status == "spawning"
    await _drain_consolidations(orch)
    assert spawns[0]["model"] == "fable"
    assert spawns[0]["detail"] == {
        "engine": "claude_code", "model": "fable", "model_source": "pinned"}


@pytest.mark.asyncio
async def test_consolidate_too_few_and_not_actionable(tmp_path, monkeypatch):
    await _init_db(tmp_path)
    orch = _build_orch([_pr(1), _pr(2)])
    status, _ = await orch._pr_doctor_consolidate("o/r", [1])
    assert status == "too_few"
    status2, _ = await orch._pr_doctor_consolidate("o/r", [1, 999])
    assert status2 == "not_actionable"               # #999 absent from cache


@pytest.mark.asyncio
async def test_consolidate_duplicate_when_live_session_exists(tmp_path, monkeypatch):
    from server.db import db_session
    from server.db.models import Session
    await _init_db(tmp_path)
    orch = _build_orch([_pr(1), _pr(2)])
    async with db_session() as db:
        db.add(Session(id="existing", name="c", account_name="a", status="idle",
                       cwd=".", origin="pr-doctor", origin_ref="consolidate:o/r#1+2"))
    status, sid = await orch._pr_doctor_consolidate("o/r", [1, 2])
    assert status == "duplicate" and sid is None
    events = await orch._pr_doctor_ctx.list_events()
    assert any(e["kind"] == "ownership_wait" for e in events)


@pytest.mark.asyncio
async def test_consolidate_respawns_when_prior_session_archived(tmp_path, monkeypatch):
    """An archived consolidation session for the same PR set must not count as a
    live duplicate — regression for archived leftovers blocking re-spawn."""
    from server.db import db_session
    from server.db.models import Session
    await _init_db(tmp_path)
    orch = _build_orch([_pr(1), _pr(2)])
    async with db_session() as db:
        db.add(Session(id="old", name="c", account_name="a", status="archived",
                       cwd=".", origin="pr-doctor", origin_ref="consolidate:o/r#1+2"))

    spawns = []
    _install_spawn_recorder(orch, spawns)
    status, sid = await orch._pr_doctor_consolidate("o/r", [1, 2])
    assert status == "spawning" and sid is None
    await _drain_consolidations(orch)
    assert spawns[0]["dedupe_key"] == "consolidate:o/r#1+2"

    # Unlike inactive legacy history, the new canonical actor needs native proof.
    from server.automations.ownership import begin_session_execution, execution_snapshot, attest_session_stopped
    from server.tests.test_pr_doctor_step import _make_record
    state = await orch._pr_doctor_ctx.get_target_state("consolidate:o/r#1+2")
    launched_id = state["session_id"]
    await begin_session_execution(launched_id)
    record = _make_record(launched_id)
    record.status = "archived"
    orch._records[launched_id] = record
    assert (await orch._pr_doctor_consolidate("o/r", [1, 2]))[0] == "duplicate"
    proof = await execution_snapshot(launched_id)
    assert proof is not None and proof.generation is not None
    assert await attest_session_stopped(proof.token, proof.generation, session_id=launched_id)
    assert (await orch._pr_doctor_consolidate("o/r", [1, 2]))[0] == "spawning"
    await _drain_consolidations(orch)
    assert len(spawns) == 2
    assert spawns[1]["ownership_token"] != spawns[0]["ownership_token"]


@pytest.mark.asyncio
async def test_consolidate_inflight_second_call_is_duplicate(tmp_path, monkeypatch):
    """While the background clone/spawn for a PR set is still running (no DB row
    yet), a second consolidate for the same set must return `duplicate`, and
    exactly one session may spawn — the in-flight guard behind issue #567."""
    await _init_db(tmp_path)
    orch = _build_orch([_pr(1), _pr(2)])

    gate = asyncio.Event()
    calls: list[dict] = []

    spawn = _install_spawn_recorder(orch, calls)

    async def gated_spawn(**kw):
        await gate.wait()
        return await spawn(**kw)

    orch._pr_doctor_ctx.spawn_session = gated_spawn  # type: ignore[assignment]
    # wait_for: if consolidate regresses to awaiting the spawn inline, this
    # fails fast instead of deadlocking the suite on the gate.
    first, _ = await asyncio.wait_for(
        orch._pr_doctor_consolidate("o/r", [1, 2]), timeout=5)
    second, _ = await asyncio.wait_for(
        orch._pr_doctor_consolidate("o/r", [1, 2]), timeout=5)
    assert first == "spawning"
    assert second == "duplicate"
    gate.set()
    await _drain_consolidations(orch)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_consolidate_background_failure_releases_inflight(tmp_path, monkeypatch):
    """A failed background spawn must release the in-flight guard so the user
    can retry — otherwise one failure wedges the PR set forever."""
    await _init_db(tmp_path)
    orch = _build_orch([_pr(1), _pr(2)])

    async def boom(**kw):
        raise RuntimeError("boom")

    orch._pr_doctor_ctx.spawn_session = boom  # type: ignore[assignment]
    status, _ = await orch._pr_doctor_consolidate("o/r", [1, 2])
    assert status == "spawning"
    await _drain_consolidations(orch)
    state = await orch._pr_doctor_ctx.get_target_state("consolidate:o/r#1+2")
    assert not state.get("ownership_token")
    retry, _ = await orch._pr_doctor_consolidate("o/r", [1, 2])
    assert retry == "spawning"                       # definitely never started
    await _drain_consolidations(orch)
