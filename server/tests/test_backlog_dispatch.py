"""Manual dispatch: backlog_runner_dispatch + /candidates + /dispatch API."""
from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

H = {"Tailscale-User-Login": "testuser@example.com"}

# ---------------------------------------------------------------------------
# Unit tests for SessionOrchestrator.backlog_runner_dispatch
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_identity(monkeypatch):
    from server.automations.ownership import CanonicalTarget

    async def resolve(refs, *, kind):
        return tuple(CanonicalTarget("I_" + ref.split("#")[1], ref) for ref in refs)

    monkeypatch.setattr("server.automations.target_identity.resolve_targets", resolve)
    async def repos(names):
        return {name: name.casefold() for name in names}

    monkeypatch.setattr("server.automations.product_admission.resolve_repositories", repos)
    monkeypatch.setattr("server.automations.ownership_reconciliation.reconcile_legacy_targets",
                        AsyncMock())


@pytest_asyncio.fixture()
async def dispatch_orch(tmp_path, monkeypatch):
    """SessionOrchestrator with mocked backlog seams — no network, no LLM."""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url, "CCSWITCH_DEV": "1",
                                  "CCSWITCH_TAILNET_ALLOWLIST": ""}):
        from server.sessions.orchestrator import SessionOrchestrator
        from server.tests.fakes.fake_factory import fake_sdk_factory
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        # Patch the seams used by backlog_runner_dispatch
        orch._backlog_read_config = AsyncMock(return_value=(
            "auto",
            {"enabled_repos": ["o/r"],
             "max_concurrent": 1, "max_spawns_per_day": 10,
             "max_issues_per_pr": 3, "default_model": None},
        ))
        orch._backlog_announce = AsyncMock(return_value=None)
        orch._backlog_healthy_account = AsyncMock(return_value="acct-1")
        rec = SimpleNamespace(id="sess-42", name="backlog o/r#7",
                              branch="backlog/o-r-7", model=None)
        orch._backlog_spawn = AsyncMock(return_value=rec)
        # The candidate pool is the priority-queue read the loop itself selects
        # from — dispatch resolves repo/title through the same seam.
        orch._backlog_query_queue = AsyncMock(return_value=_CANDIDATES)
        from server.db import db_session, init_db
        from server.db.models import Automation
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(Automation(id="backlog-runner"))

        await _seed_product()
        orch._backlog_count_in_flight = AsyncMock(return_value=0)
        orch._backlog_loop._announce = lambda batch: orch._backlog_announce(batch)
        orch._backlog_ctx.record_outcome = AsyncMock()
        return orch


_CANDIDATES = [
    {"ref": "o/r#7", "number": 7, "repo": "o/r", "title": "Fix seven",
     "body": "...", "url": "https://github.com/o/r/issues/7"},
    {"ref": "o/r#8", "number": 8, "repo": "o/r", "title": "Fix eight",
     "body": "...", "url": "https://github.com/o/r/issues/8"},
]


@pytest.mark.asyncio
async def test_dispatch_single_ref_spawns(dispatch_orch):
    r = await dispatch_orch.backlog_runner_dispatch(["o/r#7"])
    assert r["status"] == "spawned"
    assert r["session_id"] == "sess-42"
    assert r["session_name"] == "backlog o/r#7"
    dispatch_orch._backlog_announce.assert_awaited_once()
    dispatch_orch._backlog_spawn.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_multi_ref_bundles_all(dispatch_orch):
    r = await dispatch_orch.backlog_runner_dispatch(["o/r#7", "o/r#8"])
    assert r["status"] == "spawned"
    # The batch passed to _backlog_spawn must include both refs
    batch = dispatch_orch._backlog_spawn.call_args[0][0]
    assert batch.issues == ["o/r#7", "o/r#8"]
    assert batch.primary == "o/r#7"
    assert batch.titles == {"o/r#7": "Fix seven", "o/r#8": "Fix eight"}


@pytest.mark.asyncio
async def test_dispatch_announce_failure_does_not_block_the_spawn(dispatch_orch):
    """Phase D: the claim is the durable row, the announce is a best-effort
    comment — a ``gh`` failure inside it must never cost the dispatch its spawn.
    Drives the REAL ``_backlog_announce`` so the guard under test is the one
    production runs."""
    del dispatch_orch._backlog_announce          # restore the real seam
    with patch("server.automations.backlog_github.announce_claim",
               side_effect=RuntimeError("gh down")):
        r = await dispatch_orch.backlog_runner_dispatch(["o/r#7"])
    assert r["status"] == "spawned"
    dispatch_orch._backlog_spawn.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_no_account(dispatch_orch):
    dispatch_orch._backlog_healthy_account = AsyncMock(return_value=None)
    r = await dispatch_orch.backlog_runner_dispatch(["o/r#7"])
    assert r["status"] == "no_account"
    dispatch_orch._backlog_spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_ref_not_in_candidates_uses_fallback_repo(dispatch_orch):
    """A custom ref not in current candidates must still dispatch (uses ref split for repo)."""
    dispatch_orch._backlog_query_queue = AsyncMock(return_value=[])
    r = await dispatch_orch.backlog_runner_dispatch(["o/r#99"])
    assert r["status"] == "spawned"
    batch = dispatch_orch._backlog_spawn.call_args[0][0]
    assert batch.repo == "o/r"
    assert batch.titles == {}


# ---------------------------------------------------------------------------
# API tests: GET /candidates + POST /dispatch
# ---------------------------------------------------------------------------

@contextmanager
def _api_client(monkeypatch_orch=None):
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url, "CCSWITCH_DEV": "1",
                                      "CCSWITCH_TAILNET_ALLOWLIST": ""}):
            from fastapi.testclient import TestClient
            from server.app import create_app
            from server.sessions.orchestrator import SessionOrchestrator
            from server.tests.fakes.fake_factory import fake_sdk_factory
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            if monkeypatch_orch:
                monkeypatch_orch(orch)
            app = create_app(orchestrator=orch)
            with TestClient(app, raise_server_exceptions=False) as c:
                yield c


def test_candidates_returns_list():
    def patch_orch(orch):
        orch._backlog_read_config = AsyncMock(return_value=("auto", {
            "enabled_repos": ["o/r"],
            "max_concurrent": 1, "max_spawns_per_day": 10,
            "max_issues_per_pr": 1, "default_model": None,
        }))
        orch._backlog_query_queue = AsyncMock(return_value=_CANDIDATES)
    with _api_client(patch_orch) as c:
        _seed(c)
        r = c.get("/api/automations/backlog-runner/candidates", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert "candidates" in body
    assert body["candidates"][0]["ref"] == "o/r#7"


def test_dispatch_api_happy_path():
    rec = SimpleNamespace(id="sess-1", name="backlog o/r#7", branch="b", model=None)

    def patch_orch(orch):
        orch._backlog_read_config = AsyncMock(return_value=("auto", {
            "enabled_repos": ["o/r"],
            "max_concurrent": 1, "max_spawns_per_day": 10,
            "max_issues_per_pr": 3, "default_model": None,
        }))
        orch._backlog_announce = AsyncMock(return_value=None)
        orch._backlog_healthy_account = AsyncMock(return_value="acct-1")
        orch._backlog_spawn = AsyncMock(return_value=rec)
        orch._backlog_query_queue = AsyncMock(return_value=_CANDIDATES)

    with _api_client(patch_orch) as c:
        _seed(c)
        r = c.post("/api/automations/backlog-runner/dispatch",
                   json={"refs": ["o/r#7"]}, headers=H)
    assert r.status_code == 200
    assert r.json()["status"] == "spawned"
    assert r.json()["session_id"] == "sess-1"


def test_dispatch_api_empty_refs_returns_400():
    with _api_client() as c:
        _seed(c)
        r = c.post("/api/automations/backlog-runner/dispatch",
                   json={"refs": []}, headers=H)
    assert r.status_code == 400


def test_dispatch_api_requires_auth():
    with _api_client() as c:
        r = c.post("/api/automations/backlog-runner/dispatch", json={"refs": ["o/r#1"]})
    assert r.status_code == 401


def test_remote_orch_has_backlog_candidates():
    from server.sessions.remote import RemoteOrchestrator
    assert hasattr(RemoteOrchestrator, "backlog_candidates")


def test_remote_orch_has_backlog_runner_dispatch():
    from server.sessions.remote import RemoteOrchestrator
    assert hasattr(RemoteOrchestrator, "backlog_runner_dispatch")


async def _seed_automation():
    """Ensure the backlog-runner row exists so _automation_or_404 passes."""
    from server.db import db_session
    from server.db.models import Automation
    async with db_session() as db:
        from sqlalchemy import select
        row = (await db.execute(
            select(Automation).where(Automation.id == "backlog-runner")
        )).scalar_one_or_none()
        if row is None:
            db.add(Automation(id="backlog-runner", enabled=True))
            await db.commit()


def _seed(c) -> None:
    """Seed on the app's own loop, never via a bare ``asyncio.run``.

    The live TestClient holds the module-level engine on its portal loop; a
    throwaway loop shares that pool's ``asyncio.Queue``, which binds to whichever
    loop first *awaits* a checkout. It only fails once startup saturates the pool
    ("bound to a different event loop"), so it hides locally and dies on CI (#1386).
    """
    assert c.portal is not None
    c.portal.call(_seed_automation)
    c.portal.call(_seed_product)


@pytest.mark.asyncio
async def test_proposal_tap_uses_stored_entire_batch_outside_candidate_pool(dispatch_orch):
    ctx = dispatch_orch._backlog_ctx
    proposal = await ctx.claim_target_state("o/r#7", {
        "issues": ["o/r#7", "o/r#8"], "repo": "o/r", "product": "test-product", "claimed": True,
        "proposed": True, "titles": {"o/r#7": "Seven", "o/r#8": "Eight"},
        "attempts": 2}, expected={}, source="automatic")
    dispatch_orch._backlog_query_queue = AsyncMock(return_value=[])
    assert await dispatch_orch.backlog_runner_implement_core("o/r#7") == "spawned"
    batch = dispatch_orch._backlog_spawn.call_args[0][0]
    assert batch.issues == ["o/r#7", "o/r#8"]
    assert batch.titles == {"o/r#7": "Seven", "o/r#8": "Eight"}
    state = await ctx.get_target_state("o/r#7")
    assert state["ownership_token"] == proposal["ownership_token"] and state["attempts"] == 2


@pytest.mark.asyncio
async def test_public_proposal_retry_after_no_account_restores_all_issues(dispatch_orch):
    from server.automations.action_providers import BacklogRunnerActionProvider
    provider = BacklogRunnerActionProvider(dispatch_orch)
    ctx = dispatch_orch._backlog_ctx
    original = await ctx.claim_target_state("o/r#7", {
        "issues": ["o/r#7", "o/r#8"], "repo": "o/r", "product": "test-product", "claimed": True,
        "proposed": True, "titles": {"o/r#7": "Seven", "o/r#8": "Eight"},
        "attempts": 2}, expected={}, source="automatic")
    dispatch_orch._backlog_query_queue = AsyncMock(return_value=[])
    assert await dispatch_orch.backlog_candidates() == []  # no claimed subset in batch picker
    offered = await provider.actionable()
    assert [item.id for item in offered] == ["o/r#7"]
    assert offered[0].title == "Seven"
    dispatch_orch._backlog_healthy_account = AsyncMock(return_value=None)
    assert (await provider.trigger("o/r#7", "implement")).status == "no_account"
    waiting = await ctx.get_target_state("o/r#7")
    assert waiting["waiting_account"] and "proposed" not in waiting
    offered = await provider.actionable()
    assert [item.id for item in offered] == ["o/r#7"]
    assert offered[0].actions[0].label == "Retry implementation"
    dispatch_orch._backlog_healthy_account = AsyncMock(return_value="acct-1")
    assert (await provider.trigger("o/r#7", "implement")).status == "spawned"
    spawned_batch = dispatch_orch._backlog_spawn.call_args[0][0]
    assert spawned_batch.issues == ["o/r#7", "o/r#8"]
    assert spawned_batch.titles == {"o/r#7": "Seven", "o/r#8": "Eight"}
    state = await ctx.get_target_state("o/r#7")
    assert state["ownership_token"] == original["ownership_token"]
    assert state["attempts"] == 2 and not state.get("waiting_account")
    assert await provider.actionable() == []


async def _seed_product():
    from server.db import db_session
    from server.db.models import Project, ProjectRepo
    async with db_session() as db:
        project = Project(slug="test-product", name="Test", automation_policy={"dispatch": "off"})
        db.add(project)
        await db.flush()
        db.add(ProjectRepo(project_id=project.id, repo="o/r", is_primary=True))


@pytest.mark.asyncio
async def test_manual_dispatch_resolves_owner_even_without_candidate_product(dispatch_orch):
    result = await dispatch_orch.backlog_runner_dispatch(["o/r#7"])
    assert result["status"] == "spawned"
    assert dispatch_orch._backlog_spawn.call_args.args[0].product == "test-product"


@pytest.mark.asyncio
async def test_manual_dispatch_unknown_repository_takes_no_claim(dispatch_orch):
    result = await dispatch_orch.backlog_runner_dispatch(["other/repo#7"])
    assert result["status"] == "refused"
    assert "ownership_missing" in result["reason_codes"]
    assert await dispatch_orch._backlog_ctx.get_target_state("other/repo#7") == {}
    dispatch_orch._backlog_spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_dispatch_zero_owner_cap_takes_no_claim(dispatch_orch):
    from sqlalchemy import update
    from server.db import db_session
    from server.db.models import Project
    async with db_session() as db:
        await db.execute(update(Project).values(automation_policy={"dispatch": "off", "max_in_flight": 0}))
    result = await dispatch_orch.backlog_runner_dispatch(["o/r#7"])
    assert result["status"] == "capacity"
    assert "product_capacity_full" in result["reason_codes"]
    assert await dispatch_orch._backlog_ctx.get_target_state("o/r#7") == {}
    dispatch_orch._backlog_spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_dispatch_rejects_stale_candidate_product(dispatch_orch):
    dispatch_orch._backlog_query_queue = AsyncMock(return_value=[
        {**_CANDIDATES[0], "product": "stale-owner"}])
    result = await dispatch_orch.backlog_runner_dispatch(["o/r#7"])
    assert result["status"] == "refused"
    dispatch_orch._backlog_spawn.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("source,allowed", [("automatic", False), ("manual", True), (None, False)])
@pytest.mark.parametrize("started", [False, True])
async def test_first_native_start_rechecks_current_policy(dispatch_orch, source, allowed, started):
    from server.automations.ownership import (
        StaleOwnership, begin_session_execution, execution_snapshot, owned,
    )
    ctx = dispatch_orch._backlog_ctx
    state = await ctx.claim_target_state("o/r#7", {
        "repo": "o/r", "product": "test-product", "issues": ["o/r#7"], "claimed": True},
        expected={}, source="automatic")
    async with owned(state["ownership_token"]) as tx:
        tx.operation.provenance = {"runner_dispatch": {"expected": state, "source": source}}
        session_id = tx.reserve_session()
    if started:
        await begin_session_execution(session_id)
    before = await execution_snapshot(session_id)
    assert before.tree_generation == int(started)
    if allowed or started:
        await dispatch_orch._automation_before_execution(session_id)
        after = await execution_snapshot(session_id)
        assert after.tree_generation == before.tree_generation + 1
    else:
        with pytest.raises(StaleOwnership, match="admission refused"):
            await dispatch_orch._automation_before_execution(session_id)
        assert await execution_snapshot(session_id) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("started", [False, True])
async def test_missing_runner_provenance_blocks_only_first_native_start(dispatch_orch, started):
    from server.automations.ownership import (
        StaleOwnership, begin_session_execution, execution_snapshot, owned,
    )
    state = await dispatch_orch._backlog_ctx.claim_target_state("o/r#7", {
        "repo": "o/r", "issues": ["o/r#7"], "claimed": True}, expected={}, source="automatic")
    async with owned(state["ownership_token"]) as tx:
        session_id = tx.reserve_session()
    if started:
        await begin_session_execution(session_id)
    before = await execution_snapshot(session_id)
    if not started:
        with pytest.raises(StaleOwnership, match="missing dispatch provenance"):
            await dispatch_orch._automation_before_execution(session_id)
        assert await execution_snapshot(session_id) == before
    else:
        await dispatch_orch._automation_before_execution(session_id)
        after = await execution_snapshot(session_id)
        assert after.tree_generation == before.tree_generation + 1


@pytest.mark.asyncio
async def test_manual_dispatch_rejects_cross_repository_batch(dispatch_orch):
    result = await dispatch_orch.backlog_runner_dispatch(["o/r#7", "other/repo#8"])
    assert result["status"] == "refused"
    dispatch_orch._backlog_spawn.assert_not_awaited()
    assert await dispatch_orch._backlog_ctx.get_target_state("o/r#7") == {}


@pytest.mark.asyncio
async def test_manual_dispatch_rejects_candidate_repo_unrelated_to_ref(dispatch_orch):
    dispatch_orch._backlog_query_queue = AsyncMock(return_value=[
        {**_CANDIDATES[0], "ref": "other/repo#7"}])
    result = await dispatch_orch.backlog_runner_dispatch(["other/repo#7"])
    assert result["status"] == "refused"
    dispatch_orch._backlog_spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_runner_status_reports_same_refusal_codes_as_action(dispatch_orch):
    from server.automations.backlog_runner import BacklogRunner
    result = await dispatch_orch.backlog_runner_dispatch(["other/repo#7"])
    status = BacklogRunner().status(dispatch_orch._backlog_ctx)
    assert status.waiting_reason == "policy"
    assert status.observed_at
    assert all(code in status.text for code in result["reason_codes"])
