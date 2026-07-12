"""Orchestrator consolidation: candidate enumeration + de-duped session spawn.
Uses the __new__ + injected-fakes harness from test_pr_doctor_step.py."""
from __future__ import annotations

import asyncio
import types
import pytest

from server.github.pr_status import PullRequestInfo


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
    orch._consolidation_inflight = set()
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
    from ccswitch import gitrepos
    await _init_db(tmp_path)
    orch = _build_orch([_pr(1), _pr(2)])
    monkeypatch.setattr(gitrepos, "ensure_clone", lambda root, repo, url: str(tmp_path))

    spawned = {}

    async def fake_spawn(**kw):
        spawned.update(kw)
        return types.SimpleNamespace(id="new-sess")

    orch._pr_doctor_ctx.spawn_session = fake_spawn  # type: ignore[assignment]
    # The RPC-facing call answers immediately; clone/model/spawn run in a
    # background task so the edge RPC can never time out into a 503 (issue #567).
    status, sid = await orch._pr_doctor_consolidate("o/r", [2, 1])
    assert status == "spawning" and sid is None
    await _drain_consolidations(orch)
    assert spawned["dedupe_key"] == "consolidate:o/r#1+2"   # sorted, namespaced
    assert "Closes #1" in spawned["initial_prompt"]
    assert "Closes #2" in spawned["initial_prompt"]


@pytest.mark.asyncio
async def test_consolidate_passes_pinned_session_model(tmp_path, monkeypatch):
    from ccswitch import gitrepos
    await _init_db(tmp_path)
    orch = _build_orch([_pr(1), _pr(2)])
    monkeypatch.setattr(gitrepos, "ensure_clone", lambda root, repo, url: str(tmp_path))

    async def config():
        return {"default_model": "fable"}
    orch._pr_doctor_config = config  # type: ignore[method-assign]

    spawned = {}

    async def fake_spawn(**kw):
        spawned.update(kw)
        return types.SimpleNamespace(id="new-sess")

    orch._pr_doctor_ctx.spawn_session = fake_spawn  # type: ignore[assignment]
    status, _ = await orch._pr_doctor_consolidate("o/r", [1, 2])
    assert status == "spawning"
    await _drain_consolidations(orch)
    assert spawned["model"] == "fable"
    assert spawned["detail"] == {"model": "fable", "model_source": "pinned"}


@pytest.mark.asyncio
async def test_consolidate_too_few_and_not_actionable(tmp_path, monkeypatch):
    from ccswitch import gitrepos
    await _init_db(tmp_path)
    orch = _build_orch([_pr(1), _pr(2)])
    monkeypatch.setattr(gitrepos, "ensure_clone", lambda root, repo, url: str(tmp_path))
    status, _ = await orch._pr_doctor_consolidate("o/r", [1])
    assert status == "too_few"
    status2, _ = await orch._pr_doctor_consolidate("o/r", [1, 999])
    assert status2 == "not_actionable"               # #999 absent from cache


@pytest.mark.asyncio
async def test_consolidate_duplicate_when_live_session_exists(tmp_path, monkeypatch):
    from ccswitch import gitrepos
    from server.db import db_session
    from server.db.models import Session
    await _init_db(tmp_path)
    orch = _build_orch([_pr(1), _pr(2)])
    monkeypatch.setattr(gitrepos, "ensure_clone", lambda root, repo, url: str(tmp_path))
    async with db_session() as db:
        db.add(Session(id="existing", name="c", account_name="a", status="idle",
                       cwd=".", origin="pr-doctor", origin_ref="consolidate:o/r#1+2"))
    status, sid = await orch._pr_doctor_consolidate("o/r", [1, 2])
    assert status == "duplicate" and sid == "existing"


@pytest.mark.asyncio
async def test_consolidate_respawns_when_prior_session_archived(tmp_path, monkeypatch):
    """An archived consolidation session for the same PR set must not count as a
    live duplicate — regression for archived leftovers blocking re-spawn."""
    from ccswitch import gitrepos
    from server.db import db_session
    from server.db.models import Session
    await _init_db(tmp_path)
    orch = _build_orch([_pr(1), _pr(2)])
    monkeypatch.setattr(gitrepos, "ensure_clone", lambda root, repo, url: str(tmp_path))
    async with db_session() as db:
        db.add(Session(id="old", name="c", account_name="a", status="archived",
                       cwd=".", origin="pr-doctor", origin_ref="consolidate:o/r#1+2"))

    spawned = {}

    async def fake_spawn(**kw):
        spawned.update(kw)
        return types.SimpleNamespace(id="new-sess")

    orch._pr_doctor_ctx.spawn_session = fake_spawn  # type: ignore[assignment]
    status, sid = await orch._pr_doctor_consolidate("o/r", [1, 2])
    assert status == "spawning" and sid is None
    await _drain_consolidations(orch)
    assert spawned["dedupe_key"] == "consolidate:o/r#1+2"


@pytest.mark.asyncio
async def test_consolidate_inflight_second_call_is_duplicate(tmp_path, monkeypatch):
    """While the background clone/spawn for a PR set is still running (no DB row
    yet), a second consolidate for the same set must return `duplicate`, and
    exactly one session may spawn — the in-flight guard behind issue #567."""
    from ccswitch import gitrepos
    await _init_db(tmp_path)
    orch = _build_orch([_pr(1), _pr(2)])
    monkeypatch.setattr(gitrepos, "ensure_clone", lambda root, repo, url: str(tmp_path))

    gate = asyncio.Event()
    calls: list[dict] = []

    async def gated_spawn(**kw):
        await gate.wait()
        calls.append(kw)
        return types.SimpleNamespace(id="s1")

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
    from ccswitch import gitrepos
    await _init_db(tmp_path)
    orch = _build_orch([_pr(1), _pr(2)])
    monkeypatch.setattr(gitrepos, "ensure_clone", lambda root, repo, url: str(tmp_path))

    async def boom(**kw):
        raise RuntimeError("boom")

    orch._pr_doctor_ctx.spawn_session = boom  # type: ignore[assignment]
    status, _ = await orch._pr_doctor_consolidate("o/r", [1, 2])
    assert status == "spawning"
    await _drain_consolidations(orch)
    assert orch._consolidation_inflight == set()
    retry, _ = await orch._pr_doctor_consolidate("o/r", [1, 2])
    assert retry == "spawning"                       # not wedged as duplicate
