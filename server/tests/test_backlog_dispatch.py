"""Manual dispatch: backlog_runner_dispatch + /candidates + /dispatch API."""
from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from server.config import Settings

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

    monkeypatch.setattr("server.automations.target_identity.resolve_repositories", repos)
    monkeypatch.setattr("server.automations.ownership_reconciliation.reconcile_legacy_targets",
                        AsyncMock())


@pytest_asyncio.fixture()
async def dispatch_orch(tmp_path, monkeypatch):
    """SessionOrchestrator with mocked backlog seams — no network, no LLM."""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    with patch.dict(os.environ, {"VIBERPIT_DB_URL": db_url, "VIBERPIT_DEV": "1",
                                  "VIBERPIT_TAILNET_ALLOWLIST": ""}):
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
        # The propose tap re-verifies its batch against live GitHub before it
        # spawns (#1523). Stubbed ready here so the rest of these tests keep
        # exercising the path each is about; the gate's own tests drive the seam
        # to False/None themselves.
        orch._backlog_verify_ready = AsyncMock(return_value=True)
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
        with patch.dict(os.environ, {"VIBERPIT_DB_URL": db_url, "VIBERPIT_DEV": "1",
                                      "VIBERPIT_TAILNET_ALLOWLIST": ""}):
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


async def _seed_held_issue(hold_kind: str = "owner_action", *, number: int = 7, repo: str = "o/r"):
    from server.db import db_session
    from server.db.models import WorkItem
    async with db_session() as db:
        db.add(WorkItem(repo=repo, number=number, kind="issue", state="open",
                        title=f"Fix {number}", hold_kind=hold_kind, hold_by="migration",
                        hold_reason="test"))
        await db.commit()


def test_manual_dispatch_releases_an_owner_action_hold_first():
    """The roadmap dialog's Implement-now tap: releasing a person's owner_action
    hold and dispatching a session happen as one action (spec 2026-09-18 §7,
    RELEASE_SIGNALS "implement")."""
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
        c.portal.call(_seed_held_issue)
        r = c.post("/api/automations/backlog-runner/dispatch",
                   json={"refs": ["o/r#7"]}, headers=H)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "spawned"
        c.app.state.orchestrator._backlog_spawn.assert_awaited_once()

        from sqlalchemy import select
        from server.db import db_session
        from server.db.models import AuditEvent, WorkItem

        async def _check():
            async with db_session() as db:
                wi = (await db.execute(select(WorkItem).where(
                    WorkItem.repo == "o/r", WorkItem.number == 7))).scalar_one()
                rows = (await db.execute(select(AuditEvent).where(
                    AuditEvent.type == "pm.hold.release"))).scalars().all()
                return wi.hold_kind, rows
        hold_kind, rows = c.portal.call(_check)
        assert hold_kind is None
        assert len(rows) == 1
        assert rows[0].detail["signal"] == "implement"
        assert rows[0].detail["actorClass"] == "person"
        assert rows[0].detail["kind"] == "owner_action"


def test_manual_dispatch_refuses_a_decision_hold():
    """A ``decision`` hold is resolved through its proposal (spec §8), never by
    dispatch — 422, and the seam is never called."""
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
        c.portal.call(lambda: _seed_held_issue("decision"))
        r = c.post("/api/automations/backlog-runner/dispatch",
                   json={"refs": ["o/r#7"]}, headers=H)
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["error"] == "bad_hold"
        c.app.state.orchestrator._backlog_spawn.assert_not_awaited()

        from sqlalchemy import select
        from server.db import db_session
        from server.db.models import WorkItem

        async def _check():
            async with db_session() as db:
                wi = (await db.execute(select(WorkItem).where(
                    WorkItem.repo == "o/r", WorkItem.number == 7))).scalar_one()
                return wi.hold_kind
        assert c.portal.call(_check) == "decision"


#: LAN-open mode: an unattributed loopback caller is `lan@local`, an agent-class
#: actor (spec §8). The env name comes off the settings field so no literal of it
#: lives in this file.
LAN_OPEN = {Settings.model_fields["allow_unauthenticated"].alias: "1"}


def test_manual_dispatch_refused_for_an_agent_class_caller():
    """An agent (the assistant, a session, any unattributed loopback caller) may not
    clear a person's owner_action hold — so it may not dispatch past one either: 422,
    the hold stands, and no session is spawned (spec §8)."""
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

    with patch.dict(os.environ, LAN_OPEN), _api_client(patch_orch) as c:
        _seed(c)
        c.portal.call(_seed_held_issue)
        r = c.post("/api/automations/backlog-runner/dispatch",
                   json={"refs": ["o/r#7"]})   # no identity header → lan@local
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["error"] == "hold_not_permitted"
        c.app.state.orchestrator._backlog_spawn.assert_not_awaited()

        from sqlalchemy import select
        from server.db import db_session
        from server.db.models import AuditEvent, WorkItem

        async def _check():
            async with db_session() as db:
                wi = (await db.execute(select(WorkItem).where(
                    WorkItem.repo == "o/r", WorkItem.number == 7))).scalar_one()
                rows = (await db.execute(select(AuditEvent).where(
                    AuditEvent.type == "pm.hold.release"))).scalars().all()
                return wi.hold_kind, rows
        hold_kind, rows = c.portal.call(_check)
        assert hold_kind == "owner_action"
        assert rows == []


def test_manual_dispatch_leaves_an_evidence_hold_untouched():
    """An ``evidence`` (or ``stronger_model``) hold has no release path through
    manual dispatch yet — it exits on its own (hold_guidance's docstring). Dispatch
    must still proceed, but must NOT silently clear or audit-release the hold."""
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
        c.portal.call(lambda: _seed_held_issue("evidence"))
        r = c.post("/api/automations/backlog-runner/dispatch",
                   json={"refs": ["o/r#7"]}, headers=H)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "spawned"
        c.app.state.orchestrator._backlog_spawn.assert_awaited_once()

        from sqlalchemy import select
        from server.db import db_session
        from server.db.models import AuditEvent, WorkItem

        async def _check():
            async with db_session() as db:
                wi = (await db.execute(select(WorkItem).where(
                    WorkItem.repo == "o/r", WorkItem.number == 7))).scalar_one()
                rows = (await db.execute(select(AuditEvent).where(
                    AuditEvent.type == "pm.hold.release"))).scalars().all()
                return wi.hold_kind, rows
        hold_kind, rows = c.portal.call(_check)
        assert hold_kind == "evidence"
        assert rows == []


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
    # …and it was re-verified live first, over the WHOLE stored batch (#1523).
    assert dispatch_orch._backlog_verify_ready.await_count == 1
    assert dispatch_orch._backlog_verify_ready.await_args[0][0].issues == ["o/r#7", "o/r#8"]


async def _proposal_claim(ctx, *, ref: str = "o/r#7") -> dict:
    return await ctx.claim_target_state(ref, {
        "issues": [ref], "repo": "o/r", "product": "test-product", "claimed": True,
        "proposed": True, "titles": {ref: "Seven"}, "attempts": 0},
        expected={}, source="automatic")


async def _live_check_results() -> list[str]:
    """``detail.result`` of every ``skipped_live_check`` event, oldest first."""
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import AutomationEvent
    async with db_session() as db:
        rows = (await db.execute(select(AutomationEvent).where(
            AutomationEvent.kind == "skipped_live_check").order_by(
            AutomationEvent.id))).scalars().all()
    return [(r.detail or {})["result"] for r in rows]


@pytest.mark.asyncio
@pytest.mark.parametrize("live,expected_result", [(False, "not_ready"),
                                                  (None, "check_failed")])
async def test_proposal_tap_refuses_when_the_live_recheck_does_not_pass(
        dispatch_orch, live, expected_result):
    """#1523: a proposal is verified when it is MADE, the tap spawns possibly
    hours later — so the tap re-verifies, and fails closed (spec 003 §10) on a
    check that could not be completed just as it does on one that said no."""
    ctx = dispatch_orch._backlog_ctx
    claim = await _proposal_claim(ctx)
    dispatch_orch._backlog_query_queue = AsyncMock(return_value=[])
    dispatch_orch._backlog_verify_ready = AsyncMock(return_value=live)

    assert await dispatch_orch.backlog_runner_implement_core("o/r#7") == "not_ready"

    # The PATCHED seam is what answered — not a real ``gh`` call failing closed,
    # which would make this pass for the wrong reason (the loop resolves
    # ``verify_ready`` per call precisely so this rebind lands).
    dispatch_orch._backlog_verify_ready.assert_awaited_once()
    dispatch_orch._backlog_spawn.assert_not_awaited()
    # The refusal is about THIS attempt, not the work: the claim survives so a
    # person can tap again once the blocker clears.
    state = await ctx.get_target_state("o/r#7")
    assert state["proposed"] and state["ownership_token"] == claim["ownership_token"]
    # One gate, so the tap gets the loop's own reason in the feed.
    assert await _live_check_results() == [expected_result]


@pytest.mark.asyncio
async def test_proposal_tap_refusal_reports_not_ready_to_the_action_caller(dispatch_orch):
    from server.automations.action_providers import BacklogRunnerActionProvider
    await _proposal_claim(dispatch_orch._backlog_ctx)
    dispatch_orch._backlog_query_queue = AsyncMock(return_value=[])
    dispatch_orch._backlog_verify_ready = AsyncMock(return_value=False)

    result = await BacklogRunnerActionProvider(dispatch_orch).trigger("o/r#7", "implement")

    # Mapped, not the unmapped-status fallback ("Action failed — see the feed").
    assert (result.status, result.level) == ("not_ready", "warn")
    assert "see the feed" in result.message and "failed" not in result.message


@pytest.mark.asyncio
async def test_manual_dispatch_stays_an_ungated_operator_override(dispatch_orch):
    """``backlog_runner_dispatch`` is a deliberate human override: it must NOT
    grow the tap's live gate (#1523), or an operator could no longer dispatch
    work the mirror or GitHub is currently wrong about."""
    dispatch_orch._backlog_verify_ready = AsyncMock(return_value=False)
    assert (await dispatch_orch.backlog_runner_dispatch(["o/r#7"]))["status"] == "spawned"
    dispatch_orch._backlog_verify_ready.assert_not_awaited()


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
    # #2684: the caller is told outright that nothing was queued, and why.
    assert result["queued"] is False
    assert result["message"].endswith("not queued: the product is paused (an operator cap of 0)")
    assert await dispatch_orch._backlog_ctx.get_target_state("o/r#7") == {}
    dispatch_orch._backlog_spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_dispatch_behind_a_plan_derived_zero_cap_queues(dispatch_orch, monkeypatch):
    """#2684: the capacity plan split its one fleet slot elsewhere and overlaid this
    auto product to max_in_flight 0. That 0 is load, not an operator pause — the
    next plan tick re-splits — so "dispatch #2679 now" must queue, not vanish."""
    from datetime import datetime, timezone
    from sqlalchemy import update
    from server.automations import product_admission
    from server.db import db_session
    from server.db.models import Project
    async with db_session() as db:
        await db.execute(update(Project).values(
            automation_policy={"dispatch": "off", "capacity_mode": "auto", "max_in_flight": 3}))
    plan = {"computedAt": datetime.now(timezone.utc).isoformat(), "applied": True,
            "fleetConcurrency": 1, "perProduct": {"test-product": 0},
            "budget": {"limiting": "account"}}
    monkeypatch.setattr(product_admission, "live_plan", AsyncMock(return_value=plan))
    dispatch_orch._backlog_spawn.return_value = SimpleNamespace(
        id="sess-43", name="backlog o/r#7", branch=None, model=None, status="queued")
    result = await dispatch_orch.backlog_runner_dispatch(["o/r#7"])
    assert result["status"] == "spawned" and result["queued"] is True
    assert await _wait_for_slot_intent(dispatch_orch._backlog_ctx, "o/r#7") is True


@pytest.mark.asyncio
async def test_manual_dispatch_zero_pipeline_cap_takes_no_claim(dispatch_orch):
    from sqlalchemy import update
    from server.db import db_session
    from server.db.models import Project
    async with db_session() as db:
        await db.execute(update(Project).values(automation_policy={"dispatch": "off", "max_pipeline_prs": 0}))
    result = await dispatch_orch.backlog_runner_dispatch(["o/r#7"])
    assert result["status"] == "capacity"
    assert "product_pipeline_full" in result["reason_codes"]
    assert await dispatch_orch._backlog_ctx.get_target_state("o/r#7") == {}
    dispatch_orch._backlog_spawn.assert_not_awaited()


async def _wait_for_slot_intent(ctx, ref: str) -> bool | None:
    from server.automations.ownership import owned
    token = (await ctx.get_target_state(ref))["ownership_token"]
    async with owned(token) as tx:
        return tx.operation.provenance["runner_dispatch"].get("wait_for_slot")


@pytest.mark.asyncio
async def test_manual_dispatch_pr_doctor_priority_queues_for_a_slot(dispatch_orch):
    """`_backlog_admission` must thread `_pr_doctor_pending_products` into real
    admission — and a MANUAL dispatch that only that capacity gate refuses is
    claimed and launched as a slot-waiting session, never failed ("Dispatch
    failed: capacity" on Implement now, 2026-10-04)."""
    dispatch_orch._pr_doctor_pending_products = AsyncMock(return_value={"test-product": ("o/r#9",)})
    dispatch_orch._backlog_spawn.return_value = SimpleNamespace(
        id="sess-42", name="backlog o/r#7", branch=None, model=None, status="queued")
    result = await dispatch_orch.backlog_runner_dispatch(["o/r#7"])
    assert result["status"] == "spawned" and result["queued"] is True
    dispatch_orch._backlog_spawn.assert_awaited_once()
    assert await _wait_for_slot_intent(dispatch_orch._backlog_ctx, "o/r#7") is True


@pytest.mark.asyncio
async def test_manual_dispatch_at_a_full_product_cap_queues_for_a_slot(dispatch_orch):
    from sqlalchemy import update
    from server.db import db_session
    from server.db.models import Project
    async with db_session() as db:
        await db.execute(update(Project).values(automation_policy={"dispatch": "off", "max_in_flight": 1}))
    await dispatch_orch._backlog_ctx.claim_target_state("o/r#8", {
        "repo": "o/r", "product": "test-product", "issues": ["o/r#8"], "claimed": True},
        expected={}, source="automatic")
    result = await dispatch_orch.backlog_runner_dispatch(["o/r#7"])
    assert result["status"] == "spawned"
    assert await _wait_for_slot_intent(dispatch_orch._backlog_ctx, "o/r#7") is True


@pytest.mark.asyncio
async def test_automatic_dispatch_at_capacity_still_skips(dispatch_orch):
    """Only a person's explicit request queues; the runner's own pick at a full
    product keeps the per-product skip (no claim) it always had."""
    from sqlalchemy import update
    from server.automations.backlog_selection import Batch
    from server.db import db_session
    from server.db.models import Project
    async with db_session() as db:
        await db.execute(update(Project).values(automation_policy={"dispatch": "auto"}))
    dispatch_orch._pr_doctor_pending_products = AsyncMock(return_value={"test-product": ("o/r#9",)})
    batch = Batch(primary="o/r#7", issues=["o/r#7"], repo="o/r", titles={})
    result = await dispatch_orch._backlog_dispatch_batch(batch, source="automatic")
    assert result["status"] == "capacity"
    assert "pr_doctor_priority" in result["reason_codes"]
    dispatch_orch._backlog_spawn.assert_not_awaited()
    assert await dispatch_orch._backlog_ctx.get_target_state("o/r#7") == {}


@pytest.mark.asyncio
async def test_unpressured_manual_dispatch_does_not_wait_for_a_slot(dispatch_orch):
    result = await dispatch_orch.backlog_runner_dispatch(["o/r#7"])
    assert result["status"] == "spawned" and "queued" not in result
    assert await _wait_for_slot_intent(dispatch_orch._backlog_ctx, "o/r#7") is None


@pytest.mark.asyncio
async def test_queued_claim_slot_free_excludes_its_own_claim(dispatch_orch):
    """The queued claim counts in-flight itself; the drain's re-check must not make
    it wait for the slot it already holds — but another claim still blocks it."""
    from sqlalchemy import update
    from server.automations.product_admission import queued_claim_slot_free
    from server.db import db_session
    from server.db.models import Project
    async with db_session() as db:
        await db.execute(update(Project).values(automation_policy={"dispatch": "off", "max_in_flight": 1}))
    ctx = dispatch_orch._backlog_ctx
    own = {"repo": "o/r", "product": "test-product", "issues": ["o/r#7"], "claimed": True,
           "session_id": "sess-own"}
    other = {"repo": "o/r", "product": "test-product", "issues": ["o/r#8"], "claimed": True,
             "session_id": "sess-other"}
    ctx.list_target_states = AsyncMock(return_value={"o/r#7": own})
    assert await queued_claim_slot_free("o/r", session_id="sess-own", context=ctx)
    ctx.list_target_states = AsyncMock(return_value={"o/r#7": own, "o/r#8": other})
    assert not await queued_claim_slot_free("o/r", session_id="sess-own", context=ctx)
    ctx.list_target_states = AsyncMock(return_value={"o/r#7": own})
    pending = AsyncMock(return_value={"test-product": ("o/r#9",)})
    assert not await queued_claim_slot_free("o/r", session_id="sess-own", context=ctx,
                                            pr_doctor_pending=pending)


@pytest.mark.asyncio
async def test_manual_dispatch_pr_doctor_priority_is_per_product(dispatch_orch):
    """The gate only refuses the pending product; a different product's dispatch
    is unaffected."""
    dispatch_orch._pr_doctor_pending_products = AsyncMock(return_value={"some-other-product": ("o/r#9",)})
    result = await dispatch_orch.backlog_runner_dispatch(["o/r#7"])
    assert result["status"] == "spawned"
    dispatch_orch._backlog_spawn.assert_awaited_once()


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


@pytest.mark.asyncio
async def test_dispatch_model_pin_reaches_the_spawn_and_the_claim(dispatch_orch):
    """The approve-escalation tap's one input: ``model`` pins the spawn in place of
    the product's ``default_model`` (which the loop would otherwise read off the
    admission policy) and is recorded on the claim like any other dispatch's model."""
    r = await dispatch_orch.backlog_runner_dispatch(["o/r#7"], model="fable")
    assert r["status"] == "spawned"
    assert dispatch_orch._backlog_spawn.await_args.kwargs["default_model"] == "fable"


@pytest.mark.asyncio
async def test_remote_orch_forwards_the_model_pin_only_when_set():
    """Wire compatibility: an unpinned call is byte-for-byte the pre-``model`` call,
    so a daemon from before the parameter still accepts it mid-rollout."""
    from server.sessions.remote import RemoteOrchestrator
    calls: list[dict] = []

    class _Client:
        async def call(self, name, **kw):
            calls.append({"name": name, **kw})
            return {"status": "spawned"}
    orch = RemoteOrchestrator.__new__(RemoteOrchestrator)
    orch._client = _Client()  # type: ignore[attr-defined]
    await orch.backlog_runner_dispatch(["o/r#7"])
    await orch.backlog_runner_dispatch(["o/r#7"], model="fable")
    assert "model" not in calls[0] and calls[0]["refs"] == ["o/r#7"]
    assert calls[1]["model"] == "fable"
