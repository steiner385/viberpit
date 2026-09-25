"""Orchestrator PM onboarding/classification seams (design 2026-08-31 §5).

Bare-orchestrator + real-SQLite tests: the seams' DB reads/writes and the
prompt/parse wiring, with spawn stubbed to a recorder."""
from __future__ import annotations

from server.tests.pm_ownership_fixtures import identity_repositories
import pytest





import os
import tempfile
from types import SimpleNamespace
from unittest.mock import patch


from server.db import db_session, init_db
from server.tests.fakes.fake_automation_ctx import FakeAutomationContext
from server.db.models import (
    Feature, Project, ProjectRepo, TriageProposal, WorkItem)

# Single definition so the brand ratchet's python-package pattern is counted once
# for this whole file instead of once per call site (#1374/#2138 headroom).
_ENSURE_CLONE = "ccswitch.gitrepos.ensure_clone"


@pytest.fixture(autouse=True)
def canonical_mutation_repositories(monkeypatch):
    monkeypatch.setattr("server.automations.target_identity.resolve_repositories", identity_repositories)


def _bare_orch():
    from server.sessions.orchestrator import SessionOrchestrator
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    return orch


async def _seed(db_url: str) -> str:
    await init_db(db_url)
    async with db_session() as db:
        p = Project(slug="viberpit", name="viberpit", onboarding_state="map_merged")
        db.add(p)
        await db.flush()
        db.add(ProjectRepo(project_id=p.id, repo="steiner385/viberpit",
                           is_primary=True, backfill_done=True))
        db.add(Feature(project_id=p.id, slug="sessions", title="Sessions",
                       state="live", summary="Session manager."))
        for n in range(3):
            db.add(WorkItem(repo="steiner385/viberpit", number=100 + n,
                            kind="issue", title=f"Bug {n}", state="open",
                            triage_state="unclassified"))
        return p.id


@pytest.fixture
def db_env():
    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite+aiosqlite:///{tmp}/t.db"
        with patch.dict(os.environ, {"VIBERPIT_DB_URL": url, "VIBERPIT_DEV": "1",
                                     "VIBERPIT_TAILNET_ALLOWLIST": ""}):
            yield url


@pytest.mark.asyncio
async def test_spawn_map_sets_state_and_dedup(db_env):
    await _seed(db_env)
    orch = _bare_orch()
    rec = FakeAutomationContext()
    orch._pm_onboard_ctx = rec
    orch._backlog_healthy_account = _mk_async("acct1")
    orch._effective_repo_root = _mk_async("/tmp/root")
    orch._sdlc_default_branch = _mk_async("master")
    with patch(_ENSURE_CLONE, return_value="/tmp/clone"):
        out = await orch.pm_onboard_spawn_map("viberpit")
    assert out is not None
    assert rec.calls[0]["origin_ref"] == "viberpit:feature-map"
    assert rec.calls[0]["dedupe_key"] == "viberpit:feature-map"
    async with db_session() as db:
        from sqlalchemy import select
        p = (await db.execute(select(Project).where(
            Project.slug == "viberpit"))).scalar_one()
        assert p.onboarding_state == "map_proposed"


@pytest.mark.asyncio
async def test_spawn_map_no_account_returns_none(db_env):
    await _seed(db_env)
    orch = _bare_orch()
    orch._pm_onboard_ctx = FakeAutomationContext()
    orch._backlog_healthy_account = _mk_async(None)
    orch._effective_repo_root = _mk_async("/tmp/root")
    orch._sdlc_default_branch = _mk_async("master")
    with patch(_ENSURE_CLONE, return_value="/tmp/clone"):
        assert await orch.pm_onboard_spawn_map("viberpit") is None


@pytest.mark.asyncio
async def test_spawn_constitution_dedupes_and_never_touches_onboarding_state(db_env):
    # issue #2259: a SEPARATE spawn seam/origin from pm_onboard_spawn_map, and
    # unlike it, must never advance onboarding_state — offering the proposal
    # is independent of the required onboarding ladder.
    await _seed(db_env)
    orch = _bare_orch()
    rec = FakeAutomationContext()
    orch._pm_onboard_constitution_ctx = rec
    orch._backlog_healthy_account = _mk_async("acct1")
    orch._effective_repo_root = _mk_async("/tmp/root")
    orch._sdlc_default_branch = _mk_async("master")
    with patch(_ENSURE_CLONE, return_value="/tmp/clone"):
        out = await orch.pm_onboard_spawn_constitution("viberpit")
    assert out is not None
    assert rec.calls[0]["origin_ref"] == "viberpit:constitution"
    assert rec.calls[0]["dedupe_key"] == "viberpit:constitution"
    async with db_session() as db:
        from sqlalchemy import select
        p = (await db.execute(select(Project).where(
            Project.slug == "viberpit"))).scalar_one()
        assert p.onboarding_state == "map_merged"  # unchanged by this seam


@pytest.mark.asyncio
async def test_spawn_constitution_no_account_returns_none(db_env):
    await _seed(db_env)
    orch = _bare_orch()
    orch._pm_onboard_constitution_ctx = FakeAutomationContext()
    orch._backlog_healthy_account = _mk_async(None)
    orch._effective_repo_root = _mk_async("/tmp/root")
    orch._sdlc_default_branch = _mk_async("master")
    with patch(_ENSURE_CLONE, return_value="/tmp/clone"):
        assert await orch.pm_onboard_spawn_constitution("viberpit") is None


@pytest.mark.asyncio
async def test_spawn_constitution_no_primary_repo_returns_none(db_env):
    await init_db(db_env)
    async with db_session() as db:
        db.add(Project(slug="bare", name="bare", onboarding_state="map_merged"))
    orch = _bare_orch()
    orch._pm_onboard_constitution_ctx = FakeAutomationContext()
    assert await orch.pm_onboard_spawn_constitution("bare") is None
    assert await orch.pm_onboard_spawn_constitution("does-not-exist") is None


@pytest.mark.asyncio
async def test_classify_run_dispatches_open_items(db_env):
    await _seed(db_env)
    orch = _bare_orch()
    rec = FakeAutomationContext()
    orch._pm_classify_ctx = rec
    orch._backlog_healthy_account = _mk_async("acct1")
    orch._effective_repo_root = _mk_async("/tmp/root")
    with patch(_ENSURE_CLONE, return_value="/tmp/clone"):
        n = await orch.pm_classify_run("viberpit")
    assert n == 3
    assert rec.calls[0]["origin_ref"].startswith("viberpit:classify")
    async with db_session() as db:
        from sqlalchemy import select
        p = (await db.execute(select(Project).where(
            Project.slug == "viberpit"))).scalar_one()
        assert p.onboarding_state == "classifying"


@pytest.mark.asyncio
async def test_spawn_map_diff_scope_dedup_and_no_state_change(db_env):
    # Phase-3 reuse: scope="diff" uses the map-diff dedupe key and must NOT
    # advance onboarding_state (the seam is one method, parameterized).
    await _seed(db_env)
    orch = _bare_orch()
    rec = FakeAutomationContext()
    orch._pm_onboard_ctx = rec
    orch._backlog_healthy_account = _mk_async("acct1")
    orch._effective_repo_root = _mk_async("/tmp/root")
    orch._sdlc_default_branch = _mk_async("master")
    with patch(_ENSURE_CLONE, return_value="/tmp/clone"):
        out = await orch.pm_onboard_spawn_map(
            "viberpit", scope="diff", suggestion="billing",
            item_numbers=(100, 101), repo="steiner385/viberpit")
    assert out is not None
    assert rec.calls[0]["dedupe_key"] == "viberpit:map-diff:billing"
    async with db_session() as db:
        from sqlalchemy import select
        p = (await db.execute(select(Project).where(
            Project.slug == "viberpit"))).scalar_one()
        assert p.onboarding_state == "map_merged"  # unchanged by diff scope


@pytest.mark.asyncio
async def test_classify_run_accepts_injected_batch(db_env):
    # Phase-3 reuse: items=[(repo, number, kind), ...] classifies exactly that set.
    await _seed(db_env)
    orch = _bare_orch()
    rec = FakeAutomationContext()
    orch._pm_classify_ctx = rec
    orch._backlog_healthy_account = _mk_async("acct1")
    orch._effective_repo_root = _mk_async("/tmp/root")
    with patch(_ENSURE_CLONE, return_value="/tmp/clone"):
        n = await orch.pm_classify_run(
            "viberpit",
            items=[("steiner385/viberpit", 100, "issue"),
                   ("steiner385/viberpit", 101, "issue")])
    assert n == 2  # exactly the injected set, not the full open chunk


@pytest.mark.asyncio
async def test_classify_run_never_rechains_a_deduped_session(db_env):
    """#2235: a busy triage tick can re-call ``pm_classify_run`` for a chunk
    whose first harvest has not landed yet. ``spawn_or_join_session`` hands
    back the SAME session (created=False) — chaining a second ingest onto it
    produces N settle-polls/harvests per session, and reporting the chunk size
    back to the caller would burn a daily spawn-cap slot that spawned nothing."""
    await _seed(db_env)
    orch = _bare_orch()
    existing = SimpleNamespace(id="already-chained", status="working")
    orch._pm_classify_ctx = FakeAutomationContext(existing=existing)
    orch._backlog_healthy_account = _mk_async("acct1")
    orch._effective_repo_root = _mk_async("/tmp/root")
    scheduled = []
    orch._spawn = lambda coro, name=None: scheduled.append(coro)
    with patch(_ENSURE_CLONE, return_value="/tmp/clone"):
        n = await orch.pm_classify_run("viberpit")
    assert n == 0            # nothing NEW was dispatched — must not burn the cap
    assert scheduled == []   # no second ingest chained onto the joined session


@pytest.mark.asyncio
async def test_classify_ingest_writes_pending_proposals(db_env):
    await _seed(db_env)
    orch = _bare_orch()
    output = """```json
    [{"number": 100, "kind": "issue", "feature_slug": "sessions",
      "reach": 3, "impact": 2, "effort": 1, "confidence": 2,
      "rationale": "session work", "confidence_level": "high"},
     {"number": 101, "kind": "issue", "feature_slug": null,
      "no_fit_suggestion": "billing", "rationale": "no fit",
      "confidence_level": "low"}]
    ```"""
    n = await orch._pm_classify_ingest("sess-1", "viberpit", output)
    assert n == 2
    async with db_session() as db:
        from sqlalchemy import select
        props = (await db.execute(select(TriageProposal))).scalars().all()
        assert {p.status for p in props} == {"pending"}
        by_num = {  # proposal → work item number, via join through work_items
            (await db.execute(select(WorkItem.number).where(
                WorkItem.id == p.work_item_id))).scalar_one(): p
            for p in props}
        assert by_num[100].proposed_feature_id is not None  # resolved to Feature
        assert by_num[101].proposed_feature_id is None       # no_fit
        assert by_num[101].no_fit_suggestion == "billing"

        # Fix 1: _pm_classify_ingest sets triage_state="proposed" as a scheduling
        # marker on every written WorkItem, but NEVER touches the SCORE overlay.
        wi_100 = (await db.execute(select(WorkItem).where(
            WorkItem.repo == "steiner385/viberpit",
            WorkItem.number == 100))).scalar_one()
        wi_101 = (await db.execute(select(WorkItem).where(
            WorkItem.repo == "steiner385/viberpit",
            WorkItem.number == 101))).scalar_one()
        # Both touched items advance to "proposed"
        assert wi_100.triage_state == "proposed"
        assert wi_101.triage_state == "proposed"
        # Un-touched item (102) stays "unclassified"
        wi_102 = (await db.execute(select(WorkItem).where(
            WorkItem.repo == "steiner385/viberpit",
            WorkItem.number == 102))).scalar_one()
        assert wi_102.triage_state == "unclassified"
        # Score-overlay columns are all still None (human-gate intact)
        _score_overlay = ("feature_id", "reach", "impact", "effort",
                          "confidence", "classified_by", "classified_at")
        for wi in (wi_100, wi_101):
            for col in _score_overlay:
                assert getattr(wi, col) is None, (
                    f"WorkItem #{wi.number}.{col} should be None before human accept"
                )


@pytest.mark.asyncio
async def test_classify_ingest_reports_an_empty_harvest_as_no_output(caplog):
    """An empty harvest is not a malformed payload (#1572).

    ``pm_classify.parse_failed`` carries a traceback and reads as "the classify
    session answered something the parser could not understand" — a quality
    signal worth a human. Raising it for text that is not there blamed the parser
    for having nothing to read, and filed the auto-error issue this fixes. The
    WARNING stays for output that IS present and unparseable."""
    import logging
    orch = _bare_orch()
    with caplog.at_level(logging.INFO, logger="server.sessions.orchestrator"):
        assert await orch._pm_classify_ingest("sess-1", "viberpit", "  \n ") == 0
    msgs = [r.getMessage() for r in caplog.records]
    assert any("pm_classify.no_output" in m for m in msgs), msgs
    assert not any("parse_failed" in m for m in msgs), msgs
    assert all(r.exc_info is None for r in caplog.records)


@pytest.mark.asyncio
async def test_classify_ingest_still_warns_on_output_it_cannot_parse(db_env, caplog):
    """The other half of the split: real output with no JSON array in it keeps
    the WARNING + traceback, because a human should look at it."""
    import logging
    await _seed(db_env)
    orch = _bare_orch()
    with caplog.at_level(logging.INFO, logger="server.sessions.orchestrator"):
        assert await orch._pm_classify_ingest(
            "sess-1", "viberpit", "sorry, I could not classify these") == 0
    assert any("parse_failed" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_classify_ingest_is_idempotent(db_env):
    await _seed(db_env)
    orch = _bare_orch()
    output = ('[{"number": 100, "kind": "issue", "feature_slug": "sessions",'
              ' "rationale": "x", "confidence_level": "medium"}]')
    first = await orch._pm_classify_ingest("sess-1", "viberpit", output)
    second = await orch._pm_classify_ingest("sess-1", "viberpit", output)
    assert (first, second) == (1, 0)  # second pass writes nothing (pending exists)


@pytest.mark.asyncio
async def test_apply_feature_plan_map_proposed_advances_to_map_merged(db_env):
    """Bug #2 regression: applying ≥1 feature to a map_proposed project must
    advance onboarding_state to map_merged."""
    await init_db(db_env)
    async with db_session() as db:
        p = Project(slug="test-proj", name="test-proj",
                    onboarding_state="map_proposed")
        db.add(p)
        await db.flush()
        project_id = p.id

    orch = _bare_orch()

    # Build a minimal plan with one upsert (simulating an indexed feature doc —
    # read off the default branch, so it carries a commit_sha).
    from server.pm.feature_index import FeatureIndexPlan, FeatureUpsert
    plan = FeatureIndexPlan(
        upserts=(FeatureUpsert(
            slug="core", title="Core", state="live",
            summary="Core feature.", repo_path="docs/product/features/core.md",
            parent_slug=None, links=(), commit_sha="sha-core",
        ),),
        orphan_slugs=(),
        revive_slugs=(),
        warnings=(),
    )
    await orch._pm_sync_apply_feature_plan(project_id, plan)

    async with db_session() as db:
        from sqlalchemy import select
        proj = (await db.execute(
            select(Project).where(Project.id == project_id)
        )).scalar_one()
        assert proj.onboarding_state == "map_merged"


@pytest.mark.asyncio
async def test_apply_feature_plan_a_proposed_only_project_does_not_advance(db_env):
    """A project whose only feature is a PROPOSED one (an open map-changes PR,
    commit_sha=None) must NOT advance — only a feature indexed off the default
    branch (commit_sha set) counts as a merged map (#h, matching the
    docstring: 'Indexed docs from the default branch ARE a merged map')."""
    await init_db(db_env)
    async with db_session() as db:
        p = Project(slug="proposed-only-proj", name="proposed-only-proj",
                    onboarding_state="map_proposed")
        db.add(p)
        await db.flush()
        project_id = p.id

    orch = _bare_orch()

    from server.pm.feature_index import FeatureIndexPlan, FeatureUpsert
    plan = FeatureIndexPlan(
        upserts=(FeatureUpsert(
            slug="core", title="Core", state="proposed",
            summary="Core feature.", repo_path="docs/product/features/core.md",
            parent_slug=None, links=(), commit_sha=None, pr_number=1,
        ),),
        orphan_slugs=(),
        revive_slugs=(),
        warnings=(),
    )
    await orch._pm_sync_apply_feature_plan(project_id, plan)

    async with db_session() as db:
        from sqlalchemy import select
        proj = (await db.execute(
            select(Project).where(Project.id == project_id)
        )).scalar_one()
        assert proj.onboarding_state == "map_proposed"  # still not merged


@pytest.mark.asyncio
async def test_apply_feature_plan_non_map_proposed_state_unchanged(db_env):
    """Bug #2 regression: applying a plan to a project NOT in map_proposed must
    leave onboarding_state unchanged (e.g. map_merged stays map_merged)."""
    await init_db(db_env)
    async with db_session() as db:
        p = Project(slug="stable-proj", name="stable-proj",
                    onboarding_state="map_merged")
        db.add(p)
        await db.flush()
        project_id = p.id

    orch = _bare_orch()

    from server.pm.feature_index import FeatureIndexPlan, FeatureUpsert
    plan = FeatureIndexPlan(
        upserts=(FeatureUpsert(
            slug="feature-a", title="A", state="live",
            summary="A feature.", repo_path="docs/product/features/a.md",
            parent_slug=None, links=(),
        ),),
        orphan_slugs=(),
        revive_slugs=(),
        warnings=(),
    )
    await orch._pm_sync_apply_feature_plan(project_id, plan)

    async with db_session() as db:
        from sqlalchemy import select
        proj = (await db.execute(
            select(Project).where(Project.id == project_id)
        )).scalar_one()
        assert proj.onboarding_state == "map_merged"  # unchanged


@pytest.mark.asyncio
async def test_apply_feature_plan_map_proposed_no_upserts_stays(db_env):
    """map_proposed project with 0 new features (empty plan) must NOT advance."""
    await init_db(db_env)
    async with db_session() as db:
        p = Project(slug="empty-proj", name="empty-proj",
                    onboarding_state="map_proposed")
        db.add(p)
        await db.flush()
        project_id = p.id

    orch = _bare_orch()

    from server.pm.feature_index import FeatureIndexPlan
    plan = FeatureIndexPlan(upserts=[], orphan_slugs=(), revive_slugs=(), warnings=())
    await orch._pm_sync_apply_feature_plan(project_id, plan)

    async with db_session() as db:
        from sqlalchemy import select
        proj = (await db.execute(
            select(Project).where(Project.id == project_id)
        )).scalar_one()
        assert proj.onboarding_state == "map_proposed"  # no features → no advance


@pytest.mark.asyncio
async def test_apply_feature_plan_writes_commit_sha_and_the_pr_columns(db_env):
    """R8b-4: the three provenance columns the planner now fills have to reach
    the row. ``commit_sha`` is what ``CapabilityDetail``'s doc link renders from
    (it was documented as "stamped by sync" and never written), and
    ``pr_number``+NULL ``commit_sha`` is the exact pair
    ``cleanup_after_abandon`` deletes on — so a proposed row that keeps a stale
    ``commit_sha`` survives its own abandoned PR forever."""
    await init_db(db_env)
    async with db_session() as db:
        p = Project(slug="stamp-proj", name="stamp-proj",
                    onboarding_state="map_merged")
        db.add(p)
        await db.flush()
        project_id = p.id

    orch = _bare_orch()
    from server.pm.feature_index import FeatureIndexPlan, FeatureUpsert

    def _up(slug, **over):
        base = dict(slug=slug, title=slug.title(), state="live",
                    summary=f"{slug}.", repo_path=f"docs/product/features/{slug}.md",
                    parent_slug=None, links=())
        base.update(over)
        return FeatureUpsert(**base)

    plan = FeatureIndexPlan(
        upserts=(
            _up("merged", commit_sha="sha-main"),
            _up("proposed", state="proposed", pr_number=12,
                pr_url="https://github.com/o/r/pull/12"),
        ),
        orphan_slugs=(), revive_slugs=(), warnings=())
    await orch._pm_sync_apply_feature_plan(project_id, plan)

    async with db_session() as db:
        from sqlalchemy import select
        rows = {f.slug: f for f in (await db.execute(select(Feature).where(
            Feature.project_id == project_id))).scalars().all()}
    assert (rows["merged"].commit_sha, rows["merged"].pr_number,
            rows["merged"].pr_url) == ("sha-main", None, None)
    assert (rows["proposed"].commit_sha, rows["proposed"].pr_number,
            rows["proposed"].pr_url) == (
        None, 12, "https://github.com/o/r/pull/12")

    # …and a later pass moves a row BACK: the merge stamps the SHA and clears
    # the PR columns, which is how a merged proposal stops being a proposal.
    await orch._pm_sync_apply_feature_plan(project_id, FeatureIndexPlan(
        upserts=(_up("proposed", commit_sha="sha-main2"),),
        orphan_slugs=(), revive_slugs=(), warnings=()))
    async with db_session() as db:
        from sqlalchemy import select
        row = (await db.execute(select(Feature).where(
            Feature.slug == "proposed"))).scalars().one()
    assert (row.commit_sha, row.pr_number, row.pr_url) == ("sha-main2", None, None)


@pytest.mark.asyncio
async def test_apply_feature_plan_registered_project_with_docs_advances_to_map_merged(db_env):
    """KinDash's map was merged by hand, never via propose-map, so the project
    sat at `registered` with 18 indexed features and the wizard called it
    un-onboarded. Indexed docs ARE a merged map: registered/syncing advance."""
    await init_db(db_env)
    async with db_session() as db:
        p = Project(slug="hand-merged", name="hand-merged", onboarding_state="registered")
        db.add(p)
        await db.flush()
        project_id = p.id
    orch = _bare_orch()
    from server.pm.feature_index import FeatureIndexPlan, FeatureUpsert
    plan = FeatureIndexPlan(
        upserts=(FeatureUpsert(slug="tasks", title="Task management", state="live",
                               summary="s", repo_path="docs/product/features/tasks.md",
                               parent_slug=None, links=(), commit_sha="sha-tasks"),),
        orphan_slugs=(), revive_slugs=(), warnings=())
    await orch._pm_sync_apply_feature_plan(project_id, plan)
    async with db_session() as db:
        from sqlalchemy import select
        assert (await db.execute(select(Project).where(
            Project.id == project_id))).scalar_one().onboarding_state == "map_merged"


def _mk_async(value):
    async def _f(*a, **k):
        return value
    return _f


def _rec(status: str = "done", *, ran: bool = True) -> tuple[str, bool]:
    """``_session_settle_signal`` stub value: (status, ran).

    ``ran`` is whether ``last_turn_at`` is set: the poll accepts a settled
    status only once a turn has handed output back, so every stub has to say
    which it is (#1572). ``ran=False`` reads as never-run, the safe direction."""
    return status, ran


@pytest.mark.asyncio
async def test_classify_chain_ingest_waits_for_settled_then_ingests_once(db_env, monkeypatch):
    """The chain polls the session until it settles, then harvests + ingests ONCE.

    This is the wiring the dogfood found missing: classify sessions completed but
    ``_pm_classify_ingest`` had no caller, so proposals never landed (0 of 13)."""
    orch = _bare_orch()
    orch._records = {}   # no record → _certify_harvested_complete no-ops; see its own tests
    statuses = ["running", "running", "complete"]
    async def fake_settle_signal(sid):
        return _rec(statuses.pop(0) if statuses else "complete")
    orch._session_settle_signal = fake_settle_signal
    orch._chained_session_output_text = _mk_async('```json\n[{"number": 1}]\n```')
    ingested = []
    async def fake_ingest(session_id, project_slug, output_text):
        ingested.append((session_id, project_slug, output_text)); return 1
    orch._pm_classify_ingest = fake_ingest
    import asyncio as _a
    monkeypatch.setattr(_a, "sleep", _mk_async(None))
    written = await orch._pm_classify_chain_ingest("sess-1", "viberpit",
                                                  attempts=10, delay=0.0)
    assert written == 1
    assert ingested == [("sess-1", "viberpit", '```json\n[{"number": 1}]\n```')]


@pytest.mark.asyncio
async def test_classify_chain_ingest_never_harvests_a_session_that_never_ran(
        db_env, monkeypatch):
    """#1572 end to end: the chain must not reach the parser at all.

    Production shape — the classify automation spawns during a deploy drain,
    ``send_prompt`` is rejected, the session sits at ``idle`` with an empty
    transcript, and the harvest ran 13 ms later. The prompt is recovered and the
    session runs after the restart, so the only output of that early harvest was
    a WARNING with a traceback."""
    orch = _bare_orch()
    orch._session_settle_signal = _mk_async(_rec("idle", ran=False))
    orch._chained_session_output_text = _mk_async("")
    ingested = []

    async def fake_ingest(*a):
        ingested.append(a)
        return 0

    orch._pm_classify_ingest = fake_ingest
    import asyncio as _a
    monkeypatch.setattr(_a, "sleep", _mk_async(None))
    assert await orch._pm_classify_chain_ingest("sess-1", "viberpit",
                                                attempts=3, delay=0.0) == 0
    assert ingested == []


@pytest.mark.asyncio
async def test_materialize_chain_ingest_never_harvests_a_session_that_never_ran(
        db_env, monkeypatch):
    """The classify chain's sibling, and the costlier half of the same bug.

    ``_sdlc_materialize_ingest`` records a REJECTED MaterializationProposal on a
    parse failure and that rejection moves the attempt key, so harvesting a
    never-run session would burn one of ``MATERIALIZE_MAX_ATTEMPTS`` for a
    decompose the model never performed. Both chains share
    ``_await_session_settled``, which is why the fix belongs there."""
    await _seed_initiative(db_env)
    orch = _bare_orch()
    orch._sdlc_synth_ctx = FakeAutomationContext()
    orch._session_settle_signal = _mk_async(_rec("idle", ran=False))
    ingested = []

    async def fake_ingest(*a):
        ingested.append(a)
        return 0

    orch._sdlc_materialize_ingest = fake_ingest
    import asyncio as _a
    monkeypatch.setattr(_a, "sleep", _mk_async(None))
    assert await orch._sdlc_materialize_chain_ingest("s1", "dark-mode",
                                                     attempts=3, delay=0.0) == 0
    assert ingested == []


@pytest.mark.asyncio
async def test_classify_chain_ingest_stops_when_session_vanishes(db_env, monkeypatch):
    orch = _bare_orch()
    orch._session_settle_signal = _mk_async(None)
    ingested = []
    async def fake_ingest(*a):
        ingested.append(a); return 1
    orch._pm_classify_ingest = fake_ingest
    import asyncio as _a
    monkeypatch.setattr(_a, "sleep", _mk_async(None))
    written = await orch._pm_classify_chain_ingest("gone", "viberpit",
                                                  attempts=3, delay=0.0)
    assert written == 0
    assert ingested == []


@pytest.mark.asyncio
async def test_classify_chain_ingest_certifies_completion_after_harvest(db_env, monkeypatch):
    """A harvested classify session is driven to `complete` BY THE HARNESS.

    Its prompt says "Return ONLY a JSON array", so the session emits the JSON and
    ends the turn without ever calling `mark_complete`/`mark_no_pr`. derive_status
    then returns `done` → tier `your-turn`, and `QUIET_TIERS` lifts the maintenance
    session out of Background into Active as if it had stalled. The deliverable
    landing in TriageProposal rows is the real completion signal, so certify it
    here — where the harness, not the session, is the one that verified it."""
    orch = _bare_orch()
    orch._session_settle_signal = _mk_async(_rec("done"))
    orch._chained_session_output_text = _mk_async('```json\n[{"number": 1}]\n```')
    orch._pm_classify_ingest = _mk_async(1)
    certified = []
    async def fake_certify(session_id):
        certified.append(session_id)
    orch._certify_harvested_complete = fake_certify
    import asyncio as _a
    monkeypatch.setattr(_a, "sleep", _mk_async(None))
    assert await orch._pm_classify_chain_ingest("sess-1", "viberpit",
                                                attempts=2, delay=0.0) == 1
    assert certified == ["sess-1"]


@pytest.mark.asyncio
async def test_classify_chain_ingest_leaves_unharvested_session_for_a_human(db_env, monkeypatch):
    """Nothing harvested → do NOT certify: a parse failure or an output that
    matched no work item is exactly the case a human must look at, and `done`
    lifting into Active is how they find it. Certifying on a 0-row ingest would
    archive the one session that needed attention."""
    orch = _bare_orch()
    orch._session_settle_signal = _mk_async(_rec("done"))
    orch._chained_session_output_text = _mk_async("sorry, I could not classify these")
    orch._pm_classify_ingest = _mk_async(0)
    certified = []
    async def fake_certify(session_id):
        certified.append(session_id)
    orch._certify_harvested_complete = fake_certify
    import asyncio as _a
    monkeypatch.setattr(_a, "sleep", _mk_async(None))
    assert await orch._pm_classify_chain_ingest("sess-1", "viberpit",
                                                attempts=2, delay=0.0) == 0
    assert certified == []


async def _seed_session(status: str = "done") -> str:
    from server.db.models import Session as DbSession
    async with db_session() as db:
        row = DbSession(id="sess-h", name="pm classify viberpit",
                        account_name="acct1", cwd="/tmp/clone",
                        repo="steiner385/viberpit", status=status)
        db.add(row)
        await db.flush()
        return row.id


def _certify_orch(rec):
    orch = _bare_orch()
    orch._records = {rec.id: rec}
    persisted = []
    async def fake_persist(session_id, status):
        rec.status = status
        persisted.append((session_id, status))
        return status
    orch._persist_status = fake_persist
    return orch, persisted


@pytest.mark.asyncio
async def test_certify_harvested_complete_latches_a_non_self_certified_resolution(db_env):
    """The resolution's SOURCE is the whole point. `met_bar` trusts any
    `pr_resolution` whose source is outside `SELF_CERTIFIED_SOURCES` — so writing
    `harvest` is what keeps the completion guard from nudging "open a PR and arm
    auto-merge" at a session whose entire objective was to print JSON. A
    `mark_no_pr` tool call would land on source `tool` and trip the
    unverified-self-claim branch instead."""
    from server.automations.completion_bar import (
        HARVEST_RESOLUTION_SOURCE, SELF_CERTIFIED_SOURCES)
    from server.db.models import Session as DbSession
    await init_db(db_env)
    sid = await _seed_session()
    rec = SimpleNamespace(id=sid, status="done", pr_resolution=None,
                          pr_resolution_source=None)
    orch, persisted = _certify_orch(rec)

    await orch._certify_harvested_complete(sid)

    assert HARVEST_RESOLUTION_SOURCE not in SELF_CERTIFIED_SOURCES
    assert persisted == [(sid, "complete")]
    assert (rec.pr_resolution, rec.pr_resolution_source) == (
        "not-needed", HARVEST_RESOLUTION_SOURCE)
    async with db_session() as db:
        from sqlalchemy import select
        row = (await db.execute(select(DbSession).where(
            DbSession.id == sid))).scalar_one()
        assert row.pr_resolution == "not-needed"
        assert row.pr_resolution_source == HARVEST_RESOLUTION_SOURCE


@pytest.mark.asyncio
async def test_certify_harvested_complete_passes_the_completion_bar(db_env):
    """End-to-end on the predicate that actually gates the nudge: the latched
    pair must make `met_bar` return met for a repo-attached classify goal, whose
    wording ("pm classify …") reads as CODE work to `_is_code_goal`."""
    from server.automations.completion_bar import PrFacts, met_bar
    await init_db(db_env)
    sid = await _seed_session()
    rec = SimpleNamespace(id=sid, status="done", pr_resolution=None,
                          pr_resolution_source=None)
    orch, _ = _certify_orch(rec)

    await orch._certify_harvested_complete(sid)

    verdict = met_bar("pm classify viberpit", "initial_prompt",
                      PrFacts(has_repo=True), rec.pr_resolution,
                      pr_resolution_source=rec.pr_resolution_source)
    assert verdict.met, verdict.reason
    # Same goal WITHOUT the harness certification is the bug being fixed.
    assert not met_bar("pm classify viberpit", "initial_prompt",
                       PrFacts(has_repo=True)).met
    # ...and a `mark_no_pr` tool call would NOT have escaped it either.
    assert not met_bar("pm classify viberpit", "initial_prompt",
                       PrFacts(has_repo=True), "not-needed",
                       pr_resolution_source="tool").met


@pytest.mark.asyncio
async def test_certify_harvested_complete_never_overrides_a_terminal_status(db_env):
    """A session the user already ended/archived while the chain was polling must
    not be resurrected to `complete` by a late harvest."""
    await init_db(db_env)
    sid = await _seed_session(status="ended")
    rec = SimpleNamespace(id=sid, status="ended", pr_resolution=None,
                          pr_resolution_source=None)
    orch, persisted = _certify_orch(rec)

    await orch._certify_harvested_complete(sid)

    assert persisted == []
    assert rec.pr_resolution is None


@pytest.mark.asyncio
async def test_certify_harvested_complete_latches_without_repersisting_a_complete_session(db_env):
    """A session that reached `complete` on its own was already judged by the
    completion guard (it fires from `_persist_status`, long before this chain's
    30s poll notices). Re-persisting would only re-run that judgement — but the
    latch must still land so the retro archive_audit pass reads a verified
    resolution instead of the session's own `tool` claim."""
    from server.automations.completion_bar import HARVEST_RESOLUTION_SOURCE
    await init_db(db_env)
    sid = await _seed_session(status="complete")
    rec = SimpleNamespace(id=sid, status="complete", pr_resolution=None,
                          pr_resolution_source=None)
    orch, persisted = _certify_orch(rec)

    await orch._certify_harvested_complete(sid)

    assert persisted == []
    assert (rec.pr_resolution, rec.pr_resolution_source) == (
        "not-needed", HARVEST_RESOLUTION_SOURCE)


@pytest.mark.asyncio
async def test_certify_harvested_complete_keeps_an_existing_resolution(db_env):
    """Never clobber a resolution already decided elsewhere (a classifier verdict,
    a queue-drain) — same no-clobber rule `_persist_status` applies."""
    await init_db(db_env)
    sid = await _seed_session()
    rec = SimpleNamespace(id=sid, status="done", pr_resolution="cancelled",
                          pr_resolution_source="classifier")
    orch, persisted = _certify_orch(rec)

    await orch._certify_harvested_complete(sid)

    assert persisted == [(sid, "complete")]
    assert (rec.pr_resolution, rec.pr_resolution_source) == (
        "cancelled", "classifier")


async def _seed_hosted_session(status: str, host: str | None) -> str:
    from server.db.models import Session as DbSession
    async with db_session() as db:
        row = DbSession(id="sess-remote", name="pm classify fridgesheet",
                        account_name="acct1", cwd="/tmp/clone",
                        repo="steiner385/fridgesheet", status=status, host=host)
        db.add(row)
        await db.flush()
        return row.id


def _forwarding_orch(monkeypatch, *, raise_exc: Exception | None = None):
    """An orchestrator with NO local record (the primary's view of a session the
    edge placed on another fleet host) and the via-edge forward captured."""
    import server.sessions.internal_dispatch as dispatch
    orch = _bare_orch()
    orch._records = {}
    persisted = []
    async def fake_persist(session_id, status):
        persisted.append((session_id, status))
        return status
    orch._persist_status = fake_persist
    forwarded = []
    async def fake_forward(*, port, session_id, transport=None):
        forwarded.append(session_id)
        if raise_exc is not None:
            raise raise_exc
    monkeypatch.setattr(dispatch, "certify_harvested_via_edge", fake_forward)
    return orch, persisted, forwarded


@pytest.mark.asyncio
async def test_certify_harvested_complete_forwards_to_the_owning_host(db_env, monkeypatch):
    """The chain runs on the primary; the session it harvested may live on ANY
    fleet host (the edge ranks automation spawns across hosts, #1574). The primary's
    `_records` never held that session, so a host-local read silently no-ops: the
    harvest wrote 29 proposals and the session still sat at `done` -> "Your turn",
    escalated to a human (prod 2026-09-23, fridgesheet on rocky). The status
    change has to happen where the driver is, so it is forwarded through the
    co-located edge, whose FleetOrchestrator routes by the row's host stamp."""
    await init_db(db_env)
    sid = await _seed_hosted_session("done", "rocky")
    orch, persisted, forwarded = _forwarding_orch(monkeypatch)

    await orch._certify_harvested_complete(sid)

    assert forwarded == [sid]
    assert persisted == []


@pytest.mark.asyncio
async def test_certify_harvested_complete_does_not_forward_a_terminal_row(db_env, monkeypatch):
    """Same no-resurrection rule as the local path: a session the user archived
    while the chain polled is left alone, on every host."""
    await init_db(db_env)
    sid = await _seed_hosted_session("archived", "rocky")
    orch, persisted, forwarded = _forwarding_orch(monkeypatch)

    await orch._certify_harvested_complete(sid)

    assert forwarded == []
    assert persisted == []


@pytest.mark.asyncio
async def test_certify_harvested_complete_does_not_forward_a_vanished_row(db_env, monkeypatch):
    await init_db(db_env)
    orch, persisted, forwarded = _forwarding_orch(monkeypatch)

    await orch._certify_harvested_complete("never-existed")

    assert forwarded == []
    assert persisted == []


@pytest.mark.asyncio
async def test_certify_harvested_complete_swallows_an_unreachable_edge(db_env, monkeypatch):
    """The chain is fire-and-forget; a transport failure is logged, never raised —
    the proposals are already written, and the classify can be re-dispatched."""
    from server.sessions.internal_dispatch import EdgeUnreachable
    await init_db(db_env)
    sid = await _seed_hosted_session("done", "rocky")
    orch, persisted, forwarded = _forwarding_orch(
        monkeypatch, raise_exc=EdgeUnreachable("refused"))

    await orch._certify_harvested_complete(sid)

    assert forwarded == [sid]
    assert persisted == []


@pytest.mark.asyncio
async def test_certify_harvested_complete_public_entry_no_ops_without_a_local_record(db_env):
    """The RPC lands on the host the row is stamped with. If that host does not
    actually hold the record (stale stamp, session already torn down), it must
    answer quietly rather than forward again or raise — no ping-pong."""
    await init_db(db_env)
    sid = await _seed_hosted_session("done", "rocky")
    orch = _bare_orch()
    orch._records = {}
    persisted = []
    async def fake_persist(session_id, status):
        persisted.append((session_id, status))
        return status
    orch._persist_status = fake_persist

    await orch.certify_harvested_complete(sid)

    assert persisted == []


@pytest.mark.asyncio
async def test_classify_run_schedules_ingest_chain(db_env):
    """pm_classify_run must schedule the ingest chain for the spawned session —
    without it the session's JSON output is never harvested into proposals."""
    await _seed(db_env)
    orch = _bare_orch()
    orch._backlog_healthy_account = _mk_async("acct1")
    orch._effective_repo_root = _mk_async("/tmp/root")
    orch._pm_classify_ctx = FakeAutomationContext()
    chained = []
    async def fake_chain(session_id, slug, **kw):
        chained.append((session_id, slug)); return 0
    orch._pm_classify_chain_ingest = fake_chain
    scheduled = []
    orch._spawn = lambda coro, name=None: scheduled.append(coro)
    with patch(_ENSURE_CLONE, return_value="/tmp/clone"):
        n = await orch.pm_classify_run("viberpit")
    assert n > 0
    assert len(scheduled) == 1
    await scheduled[0]
    assert chained == [("sess-1", "viberpit")]


def _git(*args, cwd):
    import subprocess
    subprocess.run(["git", "-C", cwd, *args], check=True,
                   capture_output=True, text=True)


def _make_feature_repo(root: str, slugs) -> str:
    """A real git repo with docs/product/features/<slug>.md committed, plus a
    ``refs/remotes/origin/master`` ref (what _pm_sync_read_feature_docs resolves)."""
    import os
    import subprocess
    clone = os.path.join(root, "clone")
    os.makedirs(os.path.join(clone, "docs", "product", "features"))
    subprocess.run(["git", "init", "-q", clone], check=True,
                   capture_output=True, text=True)
    _git("config", "user.email", "t@t", cwd=clone)
    _git("config", "user.name", "t", cwd=clone)
    for slug in slugs:
        p = os.path.join(clone, "docs", "product", "features", f"{slug}.md")
        with open(p, "w") as fh:
            fh.write(f"---\nslug: {slug}\ntitle: T{slug}\nstate: live\n---\n\nbody {slug}\n")
    _git("add", "-A", cwd=clone)
    _git("commit", "-q", "-m", "docs", cwd=clone)
    head = subprocess.run(["git", "-C", clone, "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()
    _git("update-ref", "refs/remotes/origin/master", head, cwd=clone)
    return clone


@pytest.mark.asyncio
async def test_read_feature_docs_returns_all_docs_from_pinned_snapshot(db_env):
    """The reader returns EVERY feature doc in the tree (atomic snapshot) — never
    a silent subset.  Guards the regression that orphaned 21 live features."""
    orch = _bare_orch()
    orch._effective_repo_root = _mk_async("/tmp/root")
    with tempfile.TemporaryDirectory() as root:
        clone = _make_feature_repo(root, ["alpha", "beta", "gamma"])
        with patch(_ENSURE_CLONE, return_value=clone):
            read = await orch._pm_sync_read_feature_docs("o/r")
    paths = sorted(p for p, _ in read.docs)
    assert paths == [
        "docs/product/features/alpha.md",
        "docs/product/features/beta.md",
        "docs/product/features/gamma.md",
    ]
    assert all("body" in text for _, text in read.docs)
    assert read.sha  # the pinned snapshot the docs were read at (R8b-4)


def _make_persona_repo(root: str, slugs) -> str:
    """A real git repo with docs/product/personas/<slug>.md committed, plus a
    ``refs/remotes/origin/master`` ref — the persona twin of _make_feature_repo."""
    import os
    import subprocess
    clone = os.path.join(root, "clone")
    os.makedirs(os.path.join(clone, "docs", "product", "personas"))
    subprocess.run(["git", "init", "-q", clone], check=True,
                   capture_output=True, text=True)
    _git("config", "user.email", "t@t", cwd=clone)
    _git("config", "user.name", "t", cwd=clone)
    for slug in slugs:
        p = os.path.join(clone, "docs", "product", "personas", f"{slug}.md")
        with open(p, "w") as fh:
            fh.write(f"---\nslug: {slug}\ntitle: T{slug}\n---\n\nbody {slug}\n")
    _git("add", "-A", cwd=clone)
    _git("commit", "-q", "-m", "docs", cwd=clone)
    head = subprocess.run(["git", "-C", clone, "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()
    _git("update-ref", "refs/remotes/origin/master", head, cwd=clone)
    return clone


@pytest.mark.asyncio
async def test_read_persona_docs_returns_all_docs_from_pinned_snapshot(db_env):
    """Persona twin of test_read_feature_docs_returns_all_docs_from_pinned_snapshot
    — same ATOMIC-OR-RAISE contract, different subdir, via the shared reader."""
    orch = _bare_orch()
    orch._effective_repo_root = _mk_async("/tmp/root")
    with tempfile.TemporaryDirectory() as root:
        clone = _make_persona_repo(root, ["teen", "parent-admin"])
        with patch(_ENSURE_CLONE, return_value=clone):
            read = await orch._pm_sync_read_persona_docs("o/r")
    paths = sorted(p for p, _ in read.docs)
    assert paths == [
        "docs/product/personas/parent-admin.md",
        "docs/product/personas/teen.md",
    ]
    assert all("body" in text for _, text in read.docs)
    assert read.sha


@pytest.mark.asyncio
async def test_read_feature_docs_still_reads_only_the_features_subdir_after_extraction(db_env):
    """Regression guard for the _pm_sync_read_docs_at_ref extraction: a repo
    with BOTH features/ and personas/ docs must have _pm_sync_read_feature_docs
    return only the features/ ones."""
    import os
    import subprocess
    orch = _bare_orch()
    orch._effective_repo_root = _mk_async("/tmp/root")
    with tempfile.TemporaryDirectory() as root:
        clone = _make_feature_repo(root, ["alpha"])
        os.makedirs(os.path.join(clone, "docs", "product", "personas"))
        with open(os.path.join(clone, "docs", "product", "personas", "teen.md"), "w") as fh:
            fh.write("---\nslug: teen\ntitle: Teen\n---\nbody\n")
        _git("add", "-A", cwd=clone)
        _git("commit", "-q", "-m", "persona too", cwd=clone)
        head = subprocess.run(["git", "-C", clone, "rev-parse", "HEAD"],
                              check=True, capture_output=True, text=True).stdout.strip()
        _git("update-ref", "refs/remotes/origin/master", head, cwd=clone)
        with patch(_ENSURE_CLONE, return_value=clone):
            read = await orch._pm_sync_read_feature_docs("o/r")
    paths = [p for p, _ in read.docs]
    assert paths == ["docs/product/features/alpha.md"]


@pytest.mark.asyncio
async def test_read_feature_docs_raises_on_partial_read(db_env, monkeypatch):
    """A single unreadable blob makes the whole read raise — never a silent
    subset.  Returning a subset here is what orphaned the missing valid docs."""
    import subprocess
    orch = _bare_orch()
    orch._effective_repo_root = _mk_async("/tmp/root")

    def cp(cmd, rc=0, out=""):
        return subprocess.CompletedProcess(
            cmd, rc, stdout=out, stderr=("boom" if rc else ""))

    def fake_run(cmd, *a, **k):
        if "fetch" in cmd:
            return cp(cmd)
        if "symbolic-ref" in cmd:
            return cp(cmd, 0, "")            # no origin/HEAD → fallback to origin/master
        if "rev-parse" in cmd:
            return cp(cmd, 0, "deadbeefcafe\n")
        if "ls-tree" in cmd:
            return cp(cmd, 0, "docs/product/features/a.md\n"
                              "docs/product/features/b.md\n")
        if "show" in cmd:
            if cmd[-1].endswith("a.md"):
                return cp(cmd, 0, "---\nslug: a\ntitle: A\nstate: live\n---\nx")
            return cp(cmd, 1)                # b.md blob read FAILS
        return cp(cmd)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with patch(_ENSURE_CLONE, return_value="/tmp/clone"):
        with pytest.raises(RuntimeError):
            await orch._pm_sync_read_feature_docs("o/r")


def _make_repo_with_constitution(root: str, text: str | None) -> str:
    """A real git repo, optionally with `.specify/memory/constitution.md` committed,
    plus `refs/remotes/origin/master` (what `_pm_sync_read_constitution` resolves)."""
    import os
    import subprocess
    clone = os.path.join(root, "clone")
    os.makedirs(clone)
    subprocess.run(["git", "init", "-q", clone], check=True, capture_output=True, text=True)
    _git("config", "user.email", "t@t", cwd=clone)
    _git("config", "user.name", "t", cwd=clone)
    if text is not None:
        os.makedirs(os.path.join(clone, ".specify", "memory"))
        with open(os.path.join(clone, ".specify", "memory", "constitution.md"), "w") as fh:
            fh.write(text)
    else:
        # An empty repo has no commit at all otherwise — need SOME commit for HEAD
        # to resolve, so the "missing file" case is a real commit without the file.
        with open(os.path.join(clone, "README.md"), "w") as fh:
            fh.write("no constitution here\n")
    _git("add", "-A", cwd=clone)
    _git("commit", "-q", "-m", "init", cwd=clone)
    head = subprocess.run(["git", "-C", clone, "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()
    _git("update-ref", "refs/remotes/origin/master", head, cwd=clone)
    return clone


@pytest.mark.asyncio
async def test_read_constitution_returns_text_when_present(db_env):
    orch = _bare_orch()
    orch._effective_repo_root = _mk_async("/tmp/root")
    with tempfile.TemporaryDirectory() as root:
        clone = _make_repo_with_constitution(root, "# Real Constitution\n\nbody\n")
        with patch(_ENSURE_CLONE, return_value=clone):
            text = await orch._pm_sync_read_constitution("o/r")
    assert text == "# Real Constitution\n\nbody\n"


@pytest.mark.asyncio
async def test_read_constitution_returns_none_when_file_absent(db_env):
    """A repo with no constitution at all is the expected common case (issue
    #2165), never an error — unlike the feature-doc reader's atomic-or-raise
    contract, a single missing file here must not raise."""
    orch = _bare_orch()
    orch._effective_repo_root = _mk_async("/tmp/root")
    with tempfile.TemporaryDirectory() as root:
        clone = _make_repo_with_constitution(root, None)
        with patch(_ENSURE_CLONE, return_value=clone):
            text = await orch._pm_sync_read_constitution("o/r")
    assert text is None


def _make_repo_with_artifact(root: str, path: str, text: str | None) -> str:
    """A real git repo, optionally with *path* committed, plus
    `refs/remotes/origin/master` — mirrors `_make_repo_with_constitution` for
    `_pm_sync_read_artifact_doc` (issue #2161), which shares its git plumbing."""
    import os
    import subprocess
    clone = os.path.join(root, "clone")
    os.makedirs(clone)
    subprocess.run(["git", "init", "-q", clone], check=True, capture_output=True, text=True)
    _git("config", "user.email", "t@t", cwd=clone)
    _git("config", "user.name", "t", cwd=clone)
    if text is not None:
        full = os.path.join(clone, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as fh:
            fh.write(text)
    else:
        with open(os.path.join(clone, "README.md"), "w") as fh:
            fh.write("no artifact here\n")
    _git("add", "-A", cwd=clone)
    _git("commit", "-q", "-m", "init", cwd=clone)
    head = subprocess.run(["git", "-C", clone, "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()
    _git("update-ref", "refs/remotes/origin/master", head, cwd=clone)
    return clone


@pytest.mark.asyncio
async def test_read_artifact_doc_returns_text_when_present(db_env):
    orch = _bare_orch()
    orch._effective_repo_root = _mk_async("/tmp/root")
    path = "docs/sdlc/my-init/spec.md"
    with tempfile.TemporaryDirectory() as root:
        clone = _make_repo_with_artifact(root, path, "---\ninitiative: my-init\nkind: spec\n---\nbody\n")
        with patch(_ENSURE_CLONE, return_value=clone):
            text = await orch._pm_sync_read_artifact_doc("o/r", path)
    assert text == "---\ninitiative: my-init\nkind: spec\n---\nbody\n"


@pytest.mark.asyncio
async def test_read_artifact_doc_returns_none_when_file_absent(db_env):
    orch = _bare_orch()
    orch._effective_repo_root = _mk_async("/tmp/root")
    path = "docs/sdlc/my-init/spec.md"
    with tempfile.TemporaryDirectory() as root:
        clone = _make_repo_with_artifact(root, path, None)
        with patch(_ENSURE_CLONE, return_value=clone):
            text = await orch._pm_sync_read_artifact_doc("o/r", path)
    assert text is None


@pytest.mark.asyncio
async def test_list_committed_artifacts_delegates_to_the_seam_module(db_env, monkeypatch):
    orch = _bare_orch()
    calls = []

    async def fake_list(project_id, repo):
        calls.append((project_id, repo))
        return ["row"]

    monkeypatch.setattr("server.pm.artifact_sync_seams.list_committed_artifacts", fake_list)
    out = await orch._pm_sync_list_committed_artifacts("proj-1", "o/r")
    assert out == ["row"]
    assert calls == [("proj-1", "o/r")]


@pytest.mark.asyncio
async def test_apply_artifact_frontmatter_delegates_to_the_seam_module(db_env, monkeypatch):
    orch = _bare_orch()
    calls = []

    async def fake_apply(project_id, results):
        calls.append((project_id, results))

    monkeypatch.setattr("server.pm.artifact_sync_seams.apply_artifact_frontmatter", fake_apply)
    await orch._pm_sync_apply_artifact_frontmatter("proj-1", ["result"])
    assert calls == [("proj-1", ["result"])]


@pytest.mark.asyncio
async def test_spawn_critic_dedupes_and_builds_prompt(db_env):
    orch = _bare_orch()
    orch._backlog_healthy_account = _mk_async("acct1")
    orch._effective_repo_root = _mk_async("/tmp/root")
    orch._sdlc_default_branch = _mk_async("master")
    rec = FakeAutomationContext()
    orch._pm_onboard_ctx = rec
    with patch(_ENSURE_CLONE, return_value="/tmp/clone"):
        out = await orch._pm_onboard_spawn_critic(
            "viberpit", "steiner385/viberpit", "pm/feature-map-viberpit")
    assert out is not None
    assert len(rec.calls) == 1
    call = rec.calls[0]
    assert call["origin_ref"] == "viberpit:feature-map-critic"
    assert call["dedupe_key"] == "viberpit:feature-map-critic"
    assert call["detail"]["kind"] == "feature-map-critic"
    assert "pm/feature-map-viberpit" in call["initial_prompt"]


@pytest.mark.asyncio
async def test_spawn_critic_returns_none_without_account(db_env):
    orch = _bare_orch()
    orch._backlog_healthy_account = _mk_async(None)
    orch._effective_repo_root = _mk_async("/tmp/root")
    orch._sdlc_default_branch = _mk_async("master")
    orch._pm_onboard_ctx = FakeAutomationContext()
    with patch(_ENSURE_CLONE, return_value="/tmp/clone"):
        out = await orch._pm_onboard_spawn_critic("viberpit", "o/r", "pm/feature-map-viberpit")
    assert out is None


@pytest.mark.asyncio
async def test_chain_critic_waits_for_pr_then_spawns_once(db_env, monkeypatch):
    orch = _bare_orch()
    seen = {"pr": [None, None, object()]}   # PR appears on the 3rd poll
    def fake_pr_for(repo, branch):
        return seen["pr"].pop(0) if seen["pr"] else object()
    orch.pr_for = fake_pr_for
    spawned = []
    async def fake_spawn(slug, repo, branch):
        spawned.append((slug, repo, branch)); return object()
    orch._pm_onboard_spawn_critic = fake_spawn
    import asyncio as _a
    monkeypatch.setattr(_a, "sleep", _mk_async(None))   # no real waiting
    out = await orch._pm_onboard_chain_critic(
        "viberpit", "o/r", "pm/feature-map-viberpit", attempts=5, delay=0.0)
    assert out is not None
    assert spawned == [("viberpit", "o/r", "pm/feature-map-viberpit")]  # exactly once


@pytest.mark.asyncio
async def test_chain_critic_times_out_without_spawning(db_env, monkeypatch):
    orch = _bare_orch()
    orch.pr_for = lambda repo, branch: None   # PR never appears
    spawned = []
    async def fake_spawn(*a): spawned.append(a); return object()
    orch._pm_onboard_spawn_critic = fake_spawn
    import asyncio as _a
    monkeypatch.setattr(_a, "sleep", _mk_async(None))
    out = await orch._pm_onboard_chain_critic("s", "o/r", "b", attempts=3, delay=0.0)
    assert out is None
    assert spawned == []


async def _seed_project_for_map(db_url: str) -> str:
    """Project with slug 'viberpit', primary repo 'steiner385/viberpit',
    state 'syncing' — ready for pm_onboard_spawn_map(scope='initial')."""
    await init_db(db_url)
    async with db_session() as db:
        p = Project(slug="viberpit", name="viberpit", onboarding_state="syncing")
        db.add(p)
        await db.flush()
        db.add(ProjectRepo(project_id=p.id,
                           repo="steiner385/viberpit",
                           is_primary=True, backfill_done=True))
        return p.id


@pytest.mark.asyncio
async def test_initial_map_spawn_schedules_critic_chain(db_env):
    proj_id = await _seed_project_for_map(db_env)  # noqa: F841
    orch = _bare_orch()
    orch._backlog_healthy_account = _mk_async("acct1")
    orch._effective_repo_root = _mk_async("/tmp/root")
    orch._sdlc_default_branch = _mk_async("master")
    orch._pm_onboard_ctx = FakeAutomationContext()
    chained = []
    async def fake_chain(slug, repo, branch, **kw):
        chained.append((slug, repo, branch))
    orch._pm_onboard_chain_critic = fake_chain
    # capture the bg-task instead of really backgrounding it
    scheduled = []
    orch._spawn = lambda coro, name=None: scheduled.append(coro)
    with patch(_ENSURE_CLONE, return_value="/tmp/clone"):
        rec = await orch.pm_onboard_spawn_map("viberpit", scope="initial")
    assert rec is not None
    assert len(scheduled) == 1          # exactly one chain task scheduled
    await scheduled[0]                  # run it
    assert chained == [("viberpit", "steiner385/viberpit",
                        "pm/feature-map-viberpit")]


@pytest.mark.asyncio
async def test_initial_map_spawn_never_rechains_critic_on_a_dedupe_hit(db_env):
    """#2235: a re-tap while the map session is still live gets the SAME session
    back from the dedupe (created=False). It must still return that session (the
    caller's contract) but must not chain a second critic poller onto it."""
    await _seed_project_for_map(db_env)
    orch = _bare_orch()
    orch._backlog_healthy_account = _mk_async("acct1")
    orch._effective_repo_root = _mk_async("/tmp/root")
    orch._sdlc_default_branch = _mk_async("master")
    existing = SimpleNamespace(id="live-map-session", status="working")
    orch._pm_onboard_ctx = FakeAutomationContext(existing=existing)
    scheduled = []
    orch._spawn = lambda coro, name=None: scheduled.append(coro)
    with patch(_ENSURE_CLONE, return_value="/tmp/clone"):
        rec = await orch.pm_onboard_spawn_map("viberpit", scope="initial")
    assert rec is existing   # the caller still gets the session back
    assert scheduled == []   # ...but no second critic chain


@pytest.mark.asyncio
async def test_diff_map_spawn_does_not_schedule_critic(db_env):
    await _seed_project_for_map(db_env)
    orch = _bare_orch()
    orch._backlog_healthy_account = _mk_async("acct1")
    orch._effective_repo_root = _mk_async("/tmp/root")
    orch._sdlc_default_branch = _mk_async("master")
    orch._pm_onboard_ctx = FakeAutomationContext()
    scheduled = []
    orch._spawn = lambda coro, name=None: scheduled.append(coro)
    with patch(_ENSURE_CLONE, return_value="/tmp/clone"):
        await orch.pm_onboard_spawn_map("viberpit", scope="diff", suggestion="x")
    assert scheduled == []   # drift path never chains a critic


async def _diff_prompt(db_env, *, pointer_status: str | None) -> str:
    """Spawn one ``scope="diff"`` session and return the prompt it was seeded
    with; *pointer_status* seeds a ``feature_map_changes`` row in that status
    (None seeds none)."""
    from server.db.models import FeatureMapChange
    pid = await _seed_project_for_map(db_env)
    if pointer_status is not None:
        async with db_session() as db:
            db.add(FeatureMapChange(
                project_id=pid, repo="steiner385/viberpit",
                branch="pm/map-changes-viberpit", pr_number=12,
                status=pointer_status))
    orch = _bare_orch()
    orch._backlog_healthy_account = _mk_async("acct1")
    orch._effective_repo_root = _mk_async("/tmp/root")
    orch._sdlc_default_branch = _mk_async("master")
    rec = FakeAutomationContext()
    orch._pm_onboard_ctx = rec
    orch._spawn = lambda coro, name=None: coro.close()
    with patch(_ENSURE_CLONE, return_value="/tmp/clone"):
        await orch.pm_onboard_spawn_map("viberpit", scope="diff", suggestion="x")
    return rec.calls[0]["initial_prompt"]


@pytest.mark.asyncio
async def test_diff_map_spawn_targets_the_open_map_changes_branch(db_env):
    """R8b-9: with a map-changes PR open, the drift session is told to commit to
    that branch — the same PR the roadmap UI's inline edits ride."""
    prompt = await _diff_prompt(db_env, pointer_status="open")
    assert "pm/map-changes-viberpit" in prompt
    assert "pm/feature-map-viberpit" not in prompt


@pytest.mark.asyncio
async def test_diff_map_spawn_ignores_a_closed_pointer_and_uses_the_map_branch(db_env):
    """An abandoned/merged pointer is not a target: only an ``open`` row is."""
    prompt = await _diff_prompt(db_env, pointer_status="abandoned")
    assert "pm/map-changes-viberpit" not in prompt
    assert "pm/feature-map-viberpit" in prompt


@pytest.mark.asyncio
async def test_diff_map_spawn_without_a_pointer_uses_the_map_branch(db_env):
    prompt = await _diff_prompt(db_env, pointer_status=None)
    assert "pm/feature-map-viberpit" in prompt


@pytest.mark.asyncio
async def test_backlog_verify_ready_seam_threads_batch_issues_and_gh_run():
    """The runner's live re-check (spec 003 §7a safety net) must run the real
    ``verify_ready_live`` over the WHOLE batch with the orchestrator's ``gh``
    seam, off the event loop, and pass its tri-state answer through unchanged
    (``None`` stays ``None`` — the loop is what fails closed on it)."""
    from server.automations.backlog_selection import Batch
    orch = _bare_orch()
    sentinel_gh = object()
    orch._gh_run = sentinel_gh
    seen: list = []

    def fake_verify(refs, *, gh_run):
        seen.append((list(refs), gh_run))
        return None

    batch = Batch(primary="o/r#1", issues=["o/r#1", "o/r#2"], repo="o/r")
    with patch("server.automations.backlog_github.verify_ready_live", fake_verify):
        assert await orch._backlog_verify_ready(batch) is None
    assert seen == [(["o/r#1", "o/r#2"], sentinel_gh)]


@pytest.mark.asyncio
async def test_backlog_announce_threads_batch_and_gh_run_into_announce_claim():
    """Phase D: the claim is the durable row, and the orchestrator seam only
    announces it — the whole batch and the orchestrator's ``gh`` seam go into
    ``announce_claim``, with a comment naming the runner and the primary."""
    from server.automations.backlog_selection import Batch
    orch = _bare_orch()
    sentinel_gh = object()
    orch._gh_run = sentinel_gh
    seen: list = []

    def fake_announce(batch, comment, *, gh_run):
        seen.append((list(batch.issues), comment, gh_run))

    batch = Batch(primary="o/r#1", issues=["o/r#1", "o/r#2"], repo="o/r")
    with patch("server.automations.backlog_github.announce_claim", fake_announce):
        assert await orch._backlog_announce(batch) is None
    (issues, comment, gh_run) = seen[0]
    assert issues == ["o/r#1", "o/r#2"] and gh_run is sentinel_gh
    assert "Backlog Runner" in comment and "o/r#1" in comment


@pytest.mark.asyncio
async def test_backlog_needs_human_threads_batch_and_reason(db_env):
    """The give-up announcement carries the REASON (what a human needs in order
    to pick the target up) and the batch reconstructed from the durable row."""
    from server.automations.context import AutomationContext
    await init_db(db_env)
    orch = _bare_orch()
    orch._gh_run = object()
    orch._backlog_ctx = AutomationContext("backlog-runner")
    async with db_session() as db:
        from server.db.models import Automation
        db.add(Automation(id="backlog-runner"))
    await orch._backlog_ctx.set_target_state(
        "o/r#1", {"repo": "o/r", "issues": ["o/r#1", "o/r#2"], "gave_up": True})
    seen: list = []

    def fake_announce(batch, reason, *, gh_run):
        seen.append((list(batch.issues), reason, gh_run))

    with patch("server.automations.backlog_github.announce_needs_human", fake_announce):
        await orch._backlog_needs_human("o/r#1", "max attempts (2) reached")
    assert seen == [(["o/r#1", "o/r#2"], "max attempts (2) reached", orch._gh_run)]


@pytest.mark.asyncio
async def test_backlog_needs_human_writes_the_hold_and_audit_row(db_env):
    """Spec 2026-09-18 §3: the hold IS the handoff. It must be written and
    audited even when the best-effort GitHub comment fails (monkeypatched to
    raise here, since forcing ``self._gh_run`` itself to fail would only prove
    ``_issue_comment`` handles a bad ``gh`` — the seam under test is
    ``announce_needs_human``), and an UNMIRRORED ref in the same batch
    (``o/r#2`` here has no ``WorkItem`` row) must not block the mirrored one's
    hold — it only logs."""
    from sqlalchemy import select

    from server.automations.context import AutomationContext
    from server.db.models import Automation, AuditEvent, WorkItem
    await init_db(db_env)
    orch = _bare_orch()
    orch._gh_run = object()
    orch._backlog_ctx = AutomationContext("backlog-runner")
    async with db_session() as db:
        db.add(Automation(id="backlog-runner"))
        db.add(WorkItem(repo="o/r", number=1, kind="issue", state="open",
                        title="T", body="B"))
    await orch._backlog_ctx.set_target_state(
        "o/r#1", {"repo": "o/r", "issues": ["o/r#1", "o/r#2"], "gave_up": True})

    def raising_announce(batch, reason, *, gh_run):
        raise RuntimeError("gh comment failed (injected)")

    with patch("server.automations.backlog_github.announce_needs_human", raising_announce):
        # Must not raise: the comment is best-effort, the hold is not.
        await orch._backlog_needs_human("o/r#1", "max attempts (2) reached")

    async with db_session() as db:
        wi = (await db.execute(select(WorkItem).where(
            WorkItem.repo == "o/r", WorkItem.number == 1))).scalar_one()
        assert wi.hold_kind == "owner_action"
        assert wi.hold_by == "runner"
        assert wi.hold_reason == "max attempts (2) reached"
        assert wi.hold_set_at is not None

        events = (await db.execute(select(AuditEvent).where(
            AuditEvent.type == "pm.hold.set"))).scalars().all()
        assert len(events) == 1
        ev = events[0]
        assert ev.identity == "runner" and ev.via == "runner"
        assert ev.detail["workItemId"] == wi.id
        assert ev.detail["kind"] == "owner_action"
        assert ev.detail["reason"] == "max attempts (2) reached"
        assert ev.detail["actorClass"] == "system"
        # o/r#2 has no mirror row: skipped, not a hold, not a second audit row.
        no_mirror = (await db.execute(select(WorkItem).where(
            WorkItem.repo == "o/r", WorkItem.number == 2))).scalar_one_or_none()
        assert no_mirror is None


@pytest.mark.asyncio
async def test_backlog_escalate_writes_a_stronger_model_hold_and_never_comments(db_env):
    """Spec 2026-09-18 §4, review R1 I2/I4: ``_backlog_escalate`` is rung-only —
    a bounce at the top routes through ``_backlog_needs_human`` instead (its own
    test above), never this seam with ``rung=None``. A rung climb parks the
    issue ``stronger_model`` (agent-settable, queue-eligible — the next refill
    re-claims it) and writes exactly one ``pm.hold.set`` audit row; it never
    comments — only the final hand-off does."""
    from sqlalchemy import select

    from server.automations.context import AutomationContext
    from server.db.models import Automation, AuditEvent, WorkItem
    await init_db(db_env)
    orch = _bare_orch()
    orch._gh_run = object()
    orch._backlog_ctx = AutomationContext("backlog-runner")
    async with db_session() as db:
        db.add(Automation(id="backlog-runner"))
        db.add(WorkItem(repo="o/r", number=1, kind="issue", state="open",
                        title="T", body="B"))
    await orch._backlog_ctx.set_target_state("o/r#1", {"repo": "o/r", "issues": ["o/r#1"]})

    announced: list = []

    def recording_announce(batch, reason, *, gh_run):
        announced.append((batch.primary, reason))

    with patch("server.automations.backlog_github.announce_needs_human", recording_announce):
        await orch._backlog_escalate("o/r#1", "bounced at sonnet; next attempt on opus", "opus")
    assert announced == []

    async with db_session() as db:
        wi = (await db.execute(select(WorkItem).where(
            WorkItem.repo == "o/r", WorkItem.number == 1))).scalar_one()
        assert wi.hold_kind == "stronger_model"
        assert wi.hold_by == "runner"
        assert wi.hold_reason == "bounced at sonnet; next attempt on opus"

        events = (await db.execute(select(AuditEvent).where(
            AuditEvent.type == "pm.hold.set"))).scalars().all()
        assert len(events) == 1
        assert events[0].detail["kind"] == "stronger_model"


@pytest.mark.asyncio
async def test_backlog_write_runner_hold_is_idempotent_on_a_retry(db_env):
    """Review R1 I2 item 3: a retry that would write the IDENTICAL hold (same
    kind/actor/reason) must be a no-op — no re-stamped ``hold_set_at`` /
    ``hold_fingerprint`` (which would lose a pending body-edit release) and no
    duplicate audit row. A DIFFERENT reason at the same kind still writes."""
    from sqlalchemy import select

    from server.automations.context import AutomationContext
    from server.db.models import Automation, AuditEvent, WorkItem
    await init_db(db_env)
    orch = _bare_orch()
    orch._backlog_ctx = AutomationContext("backlog-runner")
    async with db_session() as db:
        db.add(Automation(id="backlog-runner"))
        db.add(WorkItem(repo="o/r", number=1, kind="issue", state="open",
                        title="T", body="B"))
    await orch._backlog_ctx.set_target_state("o/r#1", {"repo": "o/r", "issues": ["o/r#1"]})

    await orch._backlog_escalate("o/r#1", "bounced at sonnet; next attempt on opus", "opus")
    async with db_session() as db:
        wi = (await db.execute(select(WorkItem).where(
            WorkItem.repo == "o/r", WorkItem.number == 1))).scalar_one()
        first_stamp = wi.hold_set_at
        first_fingerprint = wi.hold_fingerprint

    # Identical retry: no write, no audit, no re-stamp.
    await orch._backlog_escalate("o/r#1", "bounced at sonnet; next attempt on opus", "opus")
    async with db_session() as db:
        wi = (await db.execute(select(WorkItem).where(
            WorkItem.repo == "o/r", WorkItem.number == 1))).scalar_one()
        assert wi.hold_set_at == first_stamp
        assert wi.hold_fingerprint == first_fingerprint
        events = (await db.execute(select(AuditEvent).where(
            AuditEvent.type == "pm.hold.set"))).scalars().all()
        assert len(events) == 1

    # A genuinely new rung: writes again.
    await orch._backlog_escalate("o/r#1", "bounced at opus; next attempt on fable", "fable")
    async with db_session() as db:
        wi = (await db.execute(select(WorkItem).where(
            WorkItem.repo == "o/r", WorkItem.number == 1))).scalar_one()
        assert wi.hold_reason == "bounced at opus; next attempt on fable"
        events = (await db.execute(select(AuditEvent).where(
            AuditEvent.type == "pm.hold.set"))).scalars().all()
        assert len(events) == 2


@pytest.mark.asyncio
async def test_backlog_write_runner_hold_never_replaces_a_persons_hold(db_env):
    """Review R1 I6: a person's ``owner_action``/``decision`` hold — not the
    runner's own — must never be overwritten by a queue-eligible
    ``stronger_model`` hold (that would silently re-dispatch work a human
    explicitly stopped). Applies symmetrically: the runner's OWN give-up must
    not downgrade a person's ``decision`` hold to ``owner_action`` either."""
    from datetime import datetime, timezone
    from sqlalchemy import select

    from server.automations.context import AutomationContext
    from server.db.models import Automation, AuditEvent, WorkItem
    from server.pm.holds import set_decision_hold
    await init_db(db_env)
    orch = _bare_orch()
    orch._gh_run = object()
    orch._backlog_ctx = AutomationContext("backlog-runner")
    now = datetime.now(timezone.utc)
    async with db_session() as db:
        db.add(Automation(id="backlog-runner"))
        wi = WorkItem(repo="o/r", number=1, kind="issue", state="open", title="T", body="B")
        # Through THE one writer of a decision hold (spec §8): ``set_hold`` refuses
        # the kind, so a proposal is the only way an item ever reaches this state.
        set_decision_hold(wi, reason="human called it", actor="tony@example.com", now=now)
        db.add(wi)
    await orch._backlog_ctx.set_target_state("o/r#1", {"repo": "o/r", "issues": ["o/r#1"]})

    with patch("server.automations.backlog_github.announce_needs_human", lambda *a, **k: None):
        await orch._backlog_escalate("o/r#1", "bounced at sonnet; next attempt on opus", "opus")
        await orch._backlog_needs_human("o/r#1", "bounced at fable: no mergeable PR after 1 attempts")

    async with db_session() as db:
        wi = (await db.execute(select(WorkItem).where(
            WorkItem.repo == "o/r", WorkItem.number == 1))).scalar_one()
        assert wi.hold_kind == "decision"          # untouched, both times
        assert wi.hold_by == "tony@example.com"
        assert wi.hold_reason == "human called it"
        events = (await db.execute(select(AuditEvent).where(
            AuditEvent.type == "pm.hold.set"))).scalars().all()
        assert events == []                         # no audit for a kept hold


@pytest.mark.asyncio
async def test_escalate_never_replaces_an_evidence_hold_but_the_handoff_does(db_env):
    """Spec 2026-09-18 §5: an ``evidence`` hold is not in ``HELD_KINDS`` — the item
    stays queue-eligible, because it IS dispatched, as an investigation. The ladder
    must still leave it alone: an evidence item never climbs, and replacing its hold
    with ``stronger_model`` would re-queue an untriaged error report as an implement
    candidate. The give-up writer DOES replace it — that is where the
    investigation's escalate exit (and the no-exit hand-off) lands."""
    from datetime import datetime, timezone
    from sqlalchemy import select

    from server.automations.context import AutomationContext
    from server.db.models import Automation, AuditEvent, WorkItem
    from server.pm.holds import ERROR_TRIAGE_ACTOR, RUNNER_ACTOR, set_evidence_hold
    await init_db(db_env)
    orch = _bare_orch()
    orch._gh_run = object()
    orch._backlog_ctx = AutomationContext("backlog-runner")
    async with db_session() as db:
        db.add(Automation(id="backlog-runner"))
        wi = WorkItem(repo="o/r", number=1, kind="issue", state="open", title="T", body="B")
        set_evidence_hold(wi, reason="auto-filed error report", actor=ERROR_TRIAGE_ACTOR,
                          now=datetime.now(timezone.utc))
        db.add(wi)
    await orch._backlog_ctx.set_target_state("o/r#1", {"repo": "o/r", "issues": ["o/r#1"]})

    await orch._backlog_escalate("o/r#1", "bounced at sonnet; next attempt on opus", "opus")
    async with db_session() as db:
        wi = (await db.execute(select(WorkItem).where(
            WorkItem.repo == "o/r", WorkItem.number == 1))).scalar_one()
        assert wi.hold_kind == "evidence"
        assert wi.hold_by == ERROR_TRIAGE_ACTOR
        assert (await db.execute(select(AuditEvent).where(
            AuditEvent.type == "pm.hold.set"))).scalars().all() == []

    with patch("server.automations.backlog_github.announce_needs_human", lambda *a, **k: None):
        await orch._backlog_needs_human("o/r#1", "investigation ended with no exit")
    async with db_session() as db:
        wi = (await db.execute(select(WorkItem).where(
            WorkItem.repo == "o/r", WorkItem.number == 1))).scalar_one()
        assert wi.hold_kind == "owner_action"
        assert wi.hold_by == RUNNER_ACTOR
        assert wi.hold_reason == "investigation ended with no exit"
        events = (await db.execute(select(AuditEvent).where(
            AuditEvent.type == "pm.hold.set"))).scalars().all()
        assert [e.detail["kind"] for e in events] == ["owner_action"]


@pytest.mark.asyncio
async def test_backlog_investigation_state_reads_the_issue_the_hold_and_the_note(db_env):
    """Spec 2026-09-18 §5: an ended investigation is settled from the ISSUE (live
    GitHub, because the mirror lags a sync interval), the hold still on it, and the
    session's own ``mark_no_pr`` note — the note the tool has always asked for and
    nothing recorded until now."""
    from datetime import datetime, timezone

    from server.db.models import Session as DbSession, WorkItem
    from server.pm.holds import ERROR_TRIAGE_ACTOR, set_evidence_hold
    await init_db(db_env)
    orch = _bare_orch()
    orch._gh_run = object()
    async with db_session() as db:
        wi = WorkItem(repo="o/r", number=1, kind="issue", state="open", title="T", body="B")
        set_evidence_hold(wi, reason="auto-filed error report", actor=ERROR_TRIAGE_ACTOR,
                          now=datetime.now(timezone.utc))
        db.add(wi)
        db.add(DbSession(id="sess-1", name="x", account_name="a", cwd="/tmp",
                         status="ended", pr_resolution="not-needed",
                         pr_resolution_source="tool",
                         pr_resolution_note="converted to a fix: the counter never resets"))

    with patch("server.automations.backlog_github.issue_state_live", return_value="open"):
        info = await orch._backlog_investigation_state("o/r#1", "sess-1")
    assert info == {"issue_state": "open", "mirrored": True, "hold_kind": "evidence",
                    "hold_by": ERROR_TRIAGE_ACTOR,
                    "no_pr_note": "converted to a fix: the counter never resets"}

    # Unknown stays unknown, and a dispatch with no session id still answers the
    # issue/hold half rather than raising.
    with patch("server.automations.backlog_github.issue_state_live", return_value=None):
        info = await orch._backlog_investigation_state("o/r#1", None)
    assert info == {"issue_state": None, "mirrored": True, "hold_kind": "evidence",
                    "hold_by": ERROR_TRIAGE_ACTOR, "no_pr_note": None}

    # An issue with no mirror row says so, instead of reporting the same
    # ``hold_kind: None`` a CLEARED hold does — only the second is a conversion.
    with patch("server.automations.backlog_github.issue_state_live", return_value="open"):
        info = await orch._backlog_investigation_state("o/r#404", "sess-1")
    assert info["mirrored"] is False and info["hold_kind"] is None
    assert info["hold_by"] is None


@pytest.mark.asyncio
async def test_backlog_session_resolution_reads_the_ended_sessions_own_verdict(db_env):
    """Spec 2026-09-18 §6: the decision settle asks one question — what did this
    dispatch's session declare through ``mark_no_pr``? — and both sweeps read the
    resolution/note pair through the SAME query (``_session_no_pr``), so they can
    never be looking at different rows."""
    from server.db.models import Session as DbSession
    await init_db(db_env)
    orch = _bare_orch()
    async with db_session() as db:
        db.add(DbSession(id="sess-1", name="x", account_name="a", cwd="/tmp",
                         status="ended", pr_resolution="awaiting-decision",
                         pr_resolution_source="tool",
                         pr_resolution_note="stop the turn or let it finish?"))
        db.add(DbSession(id="sess-2", name="y", account_name="a", cwd="/tmp",
                         status="ended"))

    assert await orch._backlog_session_resolution("sess-1") == "awaiting-decision"
    # No resolution, no session id and no row are all simply "not a decision ending".
    assert await orch._backlog_session_resolution("sess-2") is None
    assert await orch._backlog_session_resolution(None) is None
    assert await orch._backlog_session_resolution("sess-404") is None

    async with db_session() as db:
        assert await orch._session_no_pr(db, "sess-1") == {
            "resolution": "awaiting-decision", "note": "stop the turn or let it finish?"}


@pytest.mark.asyncio
async def test_backlog_release_investigation_hold_clears_only_an_evidence_hold(db_env):
    """The convert-to-a-fix exit: the runner clears the ``evidence`` hold itself
    (engine-neutral — Codex has no hold-setting tool), audited as ``investigation``
    so the release is traceable to the exit that caused it. A person's park is not
    this signal's to end, and a second call has nothing left to release."""
    from datetime import datetime, timezone
    from sqlalchemy import select

    from server.db.models import AuditEvent, WorkItem
    from server.pm.holds import (
        ERROR_TRIAGE_ACTOR, RUNNER_ACTOR, set_evidence_hold, set_runner_hold,
    )
    await init_db(db_env)
    orch = _bare_orch()
    now = datetime.now(timezone.utc)
    async with db_session() as db:
        evidence = WorkItem(repo="o/r", number=1, kind="issue", state="open",
                            title="T", body="B")
        set_evidence_hold(evidence, reason="auto-filed error report",
                          actor=ERROR_TRIAGE_ACTOR, now=now)
        owned = WorkItem(repo="o/r", number=2, kind="issue", state="open",
                         title="T", body="B")
        set_runner_hold(owned, reason="handed over", now=now)
        db.add_all([evidence, owned])

    assert await orch._backlog_release_investigation_hold("o/r#1") is True
    assert await orch._backlog_release_investigation_hold("o/r#1") is False  # idempotent
    assert await orch._backlog_release_investigation_hold("o/r#2") is False  # not evidence
    assert await orch._backlog_release_investigation_hold("not-a-ref") is False

    async with db_session() as db:
        rows = (await db.execute(select(WorkItem).order_by(WorkItem.number))).scalars().all()
        assert [r.hold_kind for r in rows] == [None, "owner_action"]
        events = (await db.execute(select(AuditEvent).where(
            AuditEvent.type == "pm.hold.release"))).scalars().all()
        assert [(e.detail["kind"], e.detail["signal"], e.identity, e.via) for e in events] == [
            ("evidence", "investigation", RUNNER_ACTOR, "runner")]


@pytest.mark.asyncio
async def test_escalate_never_replaces_the_runners_own_owner_action_handoff(db_env):
    """Final review I2: the ``hold_by == RUNNER_ACTOR`` exemption exists so a
    RETRIED give-up can re-stamp its own hold. It must not extend to the
    ``stronger_model`` writer.

    A grouping plan built before a hand-off can still name that issue as a
    non-primary member of a later batch; when THAT batch bounces below the top,
    ``_backlog_escalate`` runs over every issue in it. Replacing the
    ``owner_action`` would end the hand-off — ``stronger_model`` is not in
    ``HELD_KINDS``, so the item goes back on the queue and ``_release_give_ups``
    drops the parked ``gave_up`` row — a machine ending a hold spec decision 6
    reserves for a human signal. The give-up writer keeps the exemption, so its
    own retry still re-stamps a NEW reason."""
    from sqlalchemy import select

    from server.automations.context import AutomationContext
    from server.db.models import Automation, AuditEvent, WorkItem
    await init_db(db_env)
    orch = _bare_orch()
    orch._gh_run = object()
    orch._backlog_ctx = AutomationContext("backlog-runner")
    async with db_session() as db:
        db.add(Automation(id="backlog-runner"))
        db.add(WorkItem(repo="o/r", number=1, kind="issue", state="open",
                        title="T", body="B"))
    await orch._backlog_ctx.set_target_state("o/r#1", {"repo": "o/r", "issues": ["o/r#1"]})

    with patch("server.automations.backlog_github.announce_needs_human", lambda *a, **k: None):
        await orch._backlog_needs_human("o/r#1", "bounced at fable: no mergeable PR")
        # A later batch bounces below the top and sweeps this issue with it.
        await orch._backlog_escalate("o/r#1", "bounced at sonnet; next attempt on opus",
                                     "opus")

    async with db_session() as db:
        wi = (await db.execute(select(WorkItem).where(
            WorkItem.repo == "o/r", WorkItem.number == 1))).scalar_one()
        assert wi.hold_kind == "owner_action"       # the hand-off survives
        assert wi.hold_reason == "bounced at fable: no mergeable PR"
        events = (await db.execute(select(AuditEvent).where(
            AuditEvent.type == "pm.hold.set"))).scalars().all()
        assert len(events) == 1                     # only the give-up's own row

    # The give-up writer keeps its runner exemption: a new reason re-stamps.
    with patch("server.automations.backlog_github.announce_needs_human", lambda *a, **k: None):
        await orch._backlog_needs_human("o/r#1", "PR Doctor gave up at the top")
    async with db_session() as db:
        wi = (await db.execute(select(WorkItem).where(
            WorkItem.repo == "o/r", WorkItem.number == 1))).scalar_one()
        assert wi.hold_kind == "owner_action"
        assert wi.hold_reason == "PR Doctor gave up at the top"


@pytest.mark.asyncio
async def test_backlog_ladder_rung_reads_the_hold_and_the_last_attempt(db_env):
    """Spec 2026-09-18 §4: the rung exists only while the primary issue is parked
    ``stronger_model``, and it climbs from the model the last finalised implement
    dispatch ran on, capped by the PRODUCT's ``ladder_top`` (read off the tick's
    policy snapshot, never re-read at the spawn)."""
    from datetime import datetime, timezone

    from server.automations.product_policy import effective_policy
    from server.db.models import DispatchOutcome, WorkItem
    from server.pm.holds import set_runner_stronger_hold
    await init_db(db_env)
    orch = _bare_orch()
    orch._backlog_last_policies = {
        "widget": effective_policy({"dispatch": "auto", "ladder_top": "opus"})}
    now = datetime.now(timezone.utc)
    async with db_session() as db:
        db.add(WorkItem(repo="o/r", number=1, kind="issue", state="open",
                        title="unheld", body="B"))
        wi = WorkItem(repo="o/r", number=2, kind="issue", state="open", title="T", body="B")
        set_runner_stronger_hold(wi, reason="bounced at sonnet", now=now)
        db.add(wi)
        db.add(DispatchOutcome(target_ref="o/r#2", outcome="no_pr", model="sonnet",
                               attempt_no=0, ended_at=now))

    async def rung(ref, *, product="widget", floor=None):
        return await orch._backlog_ladder_rung(product, ref,
                                               default_model="auto", floor=floor)

    assert await rung("o/r#1") is None            # no hold: not on the ladder
    assert await rung("o/r#9") is None            # not mirrored
    assert await rung("nonsense") is None         # unparseable ref
    assert await rung("o/r#2") == "opus"          # sonnet + 1
    # A pinned product default never lets the climb land below it, and
    # ``ladder_top`` ends it: a bounce at the top is the owner's (the sweep routes
    # that through the give-up path, so a None here is the unreachable case).
    assert await rung("o/r#2", product="unknown-product") == "opus"
    # Review F4: the D9 capability floor is applied AFTER the climb, not as an
    # input to ``next_rung`` (where a floor above ``ladder_top`` would END the
    # ladder on an ordinary configuration). It raises the rung and never lowers
    # it, and a floor with no fixed tier is ignored rather than compared by name.
    # Review r1 F1: the raise stops at the top — this rung is PINNED, so a floor
    # above the top would spawn on a tier the ladder decided not to climb to.
    assert await rung("o/r#2", floor="fable") == "opus"
    assert await rung("o/r#2", floor="haiku") == "opus"
    assert await rung("o/r#2", floor="best") == "opus"


@pytest.mark.asyncio
async def test_backlog_ladder_rung_floor_raise_stops_at_an_approval_clamped_top(db_env):
    """Review r1 F1: the rung is pinned into the spawn's ``default_model``, so the
    capability-floor raise must respect a ``ladder_top`` the model-approval gate
    clamped. A capability whose D9 floor is ``fable`` on a product clamped to
    ``opus`` climbs to ``opus`` — pinning ``fable`` would park the dispatch as a
    driverless ``pending_approval`` row and burn the ``stronger_model`` hold."""
    from datetime import datetime, timezone

    from server.automations.product_policy import effective_policy
    from server.db.models import DispatchOutcome, WorkItem
    from server.pm.holds import set_runner_stronger_hold
    await init_db(db_env)
    orch = _bare_orch()
    # The shape the clamped loader hands the tick snapshot: the effective top plus
    # the configured value it was lowered from.
    orch._backlog_last_policies = {
        "widget": {**effective_policy({"dispatch": "auto", "ladder_top": "opus"}),
                   "ladder_top_configured": "fable"}}
    now = datetime.now(timezone.utc)
    async with db_session() as db:
        wi = WorkItem(repo="o/r", number=2, kind="issue", state="open", title="T", body="B")
        set_runner_stronger_hold(wi, reason="bounced at sonnet", now=now)
        db.add(wi)
        db.add(DispatchOutcome(target_ref="o/r#2", outcome="no_pr", model="sonnet",
                               attempt_no=0, ended_at=now))

    async def rung(floor):
        return await orch._backlog_ladder_rung("widget", "o/r#2",
                                               default_model="auto", floor=floor)

    assert await rung("fable") == "opus"      # the floor never pins the gated tier
    assert await rung(None) == "opus"
    assert await rung("opus") == "opus"


@pytest.mark.asyncio
async def test_backlog_accepted_decisions_returns_the_decision_in_force(db_env):
    """Spec 2026-09-18 §6: the spawn's one door to ``server/pm/decisions.py``.
    It answers the ACCEPTED decision for each ref — the newest one where an issue
    has been decided more than once — and says nothing at all about a ref that is
    undecided, still pending, or not mirrored."""
    from datetime import datetime, timedelta, timezone

    from server.db.models import WorkItem
    from server.pm import decisions
    await init_db(db_env)
    orch = _bare_orch()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    later = now + timedelta(minutes=5)
    options = [{"key": "server", "label": "Server-side", "detail": "render it on the server"},
               {"key": "client", "label": "Client-side", "detail": None}]

    async with db_session() as db:
        decided = WorkItem(repo="o/r", number=1, kind="issue", state="open",
                           title="decided", body="b")
        redecided = WorkItem(repo="o/r", number=2, kind="issue", state="open",
                             title="redecided", body="b")
        pending = WorkItem(repo="o/r", number=3, kind="issue", state="open",
                           title="pending", body="b")
        db.add_all([decided, redecided, pending])
        await db.flush()
        first = await decisions.propose(db, decided, options=options,
                                        recommended_key="server", rationale="the ledger",
                                        actor="session:s1", now=now)
        await decisions.accept(db, first, chosen_key="server",
                               actor="tony@example.com", now=now)
        stale = await decisions.propose(db, redecided, options=options,
                                        recommended_key="client", rationale="first answer",
                                        actor="session:s2", now=now)
        await decisions.accept(db, stale, chosen_key="client",
                               actor="tony@example.com", now=now)
        current = await decisions.propose(db, redecided, options=options,
                                          recommended_key="server",
                                          rationale="second answer",
                                          actor="session:s3", now=later)
        await decisions.accept(db, current, chosen_key="server",
                               actor="tony@example.com", now=later)
        await decisions.propose(db, pending, options=options, recommended_key="server",
                                rationale="still open", actor="session:s4", now=now)

    got = await orch._backlog_accepted_decisions(
        ["o/r#1", "o/r#2", "o/r#3", "o/r#404", "nonsense"])
    assert got == {
        "o/r#1": "Decision (Server-side): render it on the server — the ledger",
        "o/r#2": "Decision (Server-side): render it on the server — second answer",
    }
    assert await orch._backlog_accepted_decisions([]) == {}


def test_backlog_queue_tail_returns_copy_of_cache():
    """The status-card queue-provenance seam (#990) must hand the loop a COPY
    of the cached ranked pool — mutating what it returns must never corrupt
    ``_backlog_last_queue``, which ``_backlog_select_batch`` /
    ``_backlog_refresh_groups`` / the card's counts all share."""
    orch = _bare_orch()
    cached = [{"ref": "o/r#1", "score": 5, "product": "widget"}]
    orch._backlog_last_queue = cached
    tail = orch._backlog_queue_tail()
    assert tail == cached
    tail.append({"ref": "o/r#2", "score": 1, "product": "widget"})
    assert orch._backlog_last_queue == cached  # unaffected by the caller's mutation


# --------------------------------------------------------------------------- #
# GraphQL transport: partial per-alias errors reach the reconciler (#1508)     #
# --------------------------------------------------------------------------- #

_PARTIAL = ('{"data": {"m0": {"issue": {"id": "I_1"}}, "m1": null}, '
            '"errors": [{"path": ["m1"], "message": "Validation failed: this dependency '
            'would create a cycle where the target is already blocked by the source"}]}')


def _gh(stdout: str, returncode: int, stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)


@pytest.mark.asyncio
async def test_reconcile_mutate_returns_payload_with_per_alias_errors():
    """``gh api graphql`` exits 1 whenever the response carries ``errors`` — even when
    every other alias succeeded. The mutate seam must hand that payload to the
    reconciler (which maps errors per alias and parks a mutation on its second
    failure) instead of raising and aborting the whole product cycle."""
    orch = _bare_orch()
    seen: list = []

    def gh_run(args, token=None):
        seen.append((list(args), token))
        return _gh(_PARTIAL, 1, "gh: Validation failed: this dependency would create a cycle")

    orch._gh_run = gh_run
    with patch.dict(os.environ, {"VIBERPIT_GH_PROJECT_TOKEN": "tok-pat"}):
        payload = await orch._pm_reconcile_mutate("steiner385", "mutation { m0: … }")
    assert payload["errors"][0]["path"] == ["m1"]
    assert payload["data"]["m0"]["issue"]["id"] == "I_1"
    assert seen[0][1] == "tok-pat"


@pytest.mark.asyncio
async def test_reconcile_mutate_still_raises_on_transport_failure():
    orch = _bare_orch()
    orch._gh_run = lambda args, token=None: _gh("", 1, "gh: HTTP 502 Bad Gateway")
    with patch.dict(os.environ, {"VIBERPIT_GH_PROJECT_TOKEN": "tok-pat"}):
        with pytest.raises(RuntimeError, match="502"):
            await orch._pm_reconcile_mutate("steiner385", "mutation { m0: … }")


@pytest.mark.asyncio
async def test_mirror_read_seam_stays_strict_on_graphql_errors():
    """Reads keep failing loudly: a payload with errors could read as 'board deleted'
    to the mirror (node → null), so the read seams must not swallow it."""
    orch = _bare_orch()
    orch._gh_run = lambda args, token=None: _gh(_PARTIAL, 1, "gh: Validation failed")
    with patch.dict(os.environ, {"VIBERPIT_GH_PROJECT_TOKEN": "tok-pat"}):
        with pytest.raises(RuntimeError):
            await orch._pm_sync_fetch_projects("steiner385", "query { … }")
    with pytest.raises(RuntimeError):
        await orch._pm_sync_fetch("query { … }")


# --------------------------------------------------------------------------- #
# Plan materialization (spec 003 §7b, #992)                                    #
# --------------------------------------------------------------------------- #

_DECOMP = """Here is how I would split it.

```json
{"epic_title": "Dark mode", "epic_body": "Ship dark mode.",
 "tasks": [{"title": "Tokens", "body": "b1", "depends_on": []},
           {"title": "Toggle", "body": "b2", "depends_on": [0]},
           {"title": "Docs", "body": "b3"}]}
```
"""


async def _seed_initiative(db_url: str, *, status: str = "awaiting_human") -> str:
    from server.db.models import Initiative
    await init_db(db_url)
    async with db_session() as db:
        ini = Initiative(slug="dark-mode", repo="o/r", title="Dark mode",
                         stage="plan", status=status)
        db.add(ini)
        await db.flush()
        return ini.id


def _materialize_orch(text: str = _DECOMP):
    orch = _bare_orch()
    orch._sdlc_synth_ctx = FakeAutomationContext()
    orch._chained_session_output_text = _mk_async(text)
    return orch


@pytest.mark.asyncio
async def test_await_session_settled_returns_true_once_settled(monkeypatch):
    orch = _bare_orch()
    statuses = ["running", "running", "complete"]
    async def _next_signal(sid):
        return _rec(statuses.pop(0) if statuses else "complete")
    orch._session_settle_signal = _next_signal
    import asyncio as _a
    monkeypatch.setattr(_a, "sleep", _mk_async(None))
    assert await orch._await_session_settled("s1", attempts=10, interval_s=0.0) is True


@pytest.mark.asyncio
async def test_await_session_settled_gives_up_after_attempts(monkeypatch):
    orch = _bare_orch()
    orch._session_settle_signal = _mk_async(_rec("running"))
    import asyncio as _a
    monkeypatch.setattr(_a, "sleep", _mk_async(None))
    assert await orch._await_session_settled("s1", attempts=3, interval_s=0.0) is False


@pytest.mark.asyncio
async def test_await_session_settled_does_not_accept_a_session_that_never_ran(monkeypatch):
    """#1572: a session is CREATED at ``idle``, which is in the settled tuple.

    When its prompt never lands — ``send_prompt`` returns "draining" for the whole
    of a deploy drain — it stays there, and a status-only poll called it settled
    13 ms after spawn, harvested an empty transcript, and logged
    ``pm_classify.parse_failed`` (109 occurrences in production). ``last_turn_at``
    is the fact the status lacks: ``_persist_status`` sets it only when a turn
    hands output back (ACTIVITY_STATUSES), and ``server/sessions/status.py``
    documents ``idle`` as "never ran"."""
    orch = _bare_orch()
    polls = 0

    async def _idle_never_prompted(sid):
        nonlocal polls
        polls += 1
        return _rec("idle", ran=False)

    orch._session_settle_signal = _idle_never_prompted
    import asyncio as _a
    monkeypatch.setattr(_a, "sleep", _mk_async(None))
    assert await orch._await_session_settled("s1", attempts=3, interval_s=0.0) is False
    assert polls == 3  # it WAITED out the budget, it did not answer on first look


@pytest.mark.asyncio
async def test_await_session_settled_accepts_the_turn_the_moment_it_lands(monkeypatch):
    """The wait ends when the session is settled AND has a finished turn — the
    drain case recovers on its own once the daemon comes back and resumes it."""
    orch = _bare_orch()
    seq = [("idle", False), ("in_progress", False), ("done", True)]

    async def _next(sid):
        status, ran = seq.pop(0)
        return _rec(status, ran=ran)

    orch._session_settle_signal = _next
    import asyncio as _a
    monkeypatch.setattr(_a, "sleep", _mk_async(None))
    assert await orch._await_session_settled("s1", attempts=10, interval_s=0.0) is True
    assert seq == []


@pytest.mark.asyncio
async def test_await_session_settled_gives_up_at_once_on_a_terminal_no_turn(monkeypatch):
    """``ended``/``archived`` with no finished turn is definitive: nothing will
    ever run under this id, so spending the rest of the budget (30 min for
    classify, 2 h for materialize) only delays the same answer."""
    orch = _bare_orch()
    polls = 0

    async def _archived(sid):
        nonlocal polls
        polls += 1
        return _rec("archived", ran=False)

    orch._session_settle_signal = _archived
    import asyncio as _a
    monkeypatch.setattr(_a, "sleep", _mk_async(None))
    assert await orch._await_session_settled("s1", attempts=30, interval_s=0.0) is False
    assert polls == 1


@pytest.mark.asyncio
async def test_materialize_ingest_writes_one_pending_proposal(db_env):
    from server.db.models import MaterializationProposal
    ini_id = await _seed_initiative(db_env)
    orch = _materialize_orch()
    assert await orch._sdlc_materialize_ingest("sess-9", "dark-mode") == 1
    async with db_session() as db:
        from sqlalchemy import select
        rows = (await db.execute(select(MaterializationProposal))).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.initiative_id == ini_id
    assert row.status == "pending"
    assert row.session_id == "sess-9"
    assert len(row.payload["tasks"]) == 3
    assert row.payload["epic_title"] == "Dark mode"
    assert row.payload["tasks"][1]["depends_on"] == [0]
    assert row.payload["source_session"] == "sess-9"
    assert row.rationale and row.rationale.startswith("Here is how")


@pytest.mark.asyncio
async def test_materialize_ingest_is_idempotent_while_pending(db_env):
    from server.db.models import MaterializationProposal
    await _seed_initiative(db_env)
    orch = _materialize_orch()
    assert await orch._sdlc_materialize_ingest("sess-9", "dark-mode") == 1
    assert await orch._sdlc_materialize_ingest("sess-10", "dark-mode") == 0
    async with db_session() as db:
        from sqlalchemy import select
        rows = (await db.execute(select(MaterializationProposal))).scalars().all()
    assert len(rows) == 1
    kinds = [e[2].get("kind") for e in orch._sdlc_synth_ctx.events]
    assert "materialization_duplicate_ignored" in kinds


@pytest.mark.asyncio
async def test_materialize_ingest_parse_failure_warns_with_no_pending_proposal(db_env):
    """The emit is the human-visible half; the rejected row it also writes is
    pinned by ``…_records_a_rejected_proposal`` below."""
    from server.db.models import MaterializationProposal
    await _seed_initiative(db_env)
    orch = _materialize_orch("I could not do it, sorry.")
    assert await orch._sdlc_materialize_ingest("sess-9", "dark-mode") == 0
    async with db_session() as db:
        from sqlalchemy import select
        rows = (await db.execute(select(MaterializationProposal))).scalars().all()
        assert [r.status for r in rows] == ["rejected"]  # nothing to review
    ev = next(e for e in orch._sdlc_synth_ctx.events
              if e[2].get("kind") == "materialization_parse_failed")
    assert ev[0] == "warn"
    assert ev[2]["detail"]["error"]


@pytest.mark.asyncio
async def test_materialize_chain_ingest_swallows_ingest_failures(db_env, monkeypatch):
    """An exception inside the chained task must never escape — the task is
    fire-and-forget, so an escape is an unretrievable 'never retrieved' log."""
    orch = _bare_orch()
    orch._session_settle_signal = _mk_async(_rec("complete"))
    orch._sdlc_synth_ctx = FakeAutomationContext()

    async def boom(*a, **k):
        raise RuntimeError("db is gone")

    orch._sdlc_materialize_ingest = boom
    import asyncio as _a
    monkeypatch.setattr(_a, "sleep", _mk_async(None))
    assert await orch._sdlc_materialize_chain_ingest("s1", "dark-mode",
                                                     attempts=2, delay=0.0) == 0


@pytest.mark.asyncio
async def test_materialize_chain_ingest_certifies_only_a_real_proposal(db_env, monkeypatch):
    """The classify chain's sibling: `server/pm/materialize.py` also says "Return
    ONLY a JSON object", so a decompose session also ends at `done` with no
    completion signal — and since `_persist_status` releases the driver only on
    `complete`, each finished one pinned a live `claude` process. Certify on a
    written proposal; a parse failure (0) stays visible for a human."""
    import asyncio as _a
    monkeypatch.setattr(_a, "sleep", _mk_async(None))
    for written, expected in ((1, ["s1"]), (0, [])):
        orch = _bare_orch()
        orch._session_settle_signal = _mk_async(_rec("done"))
        orch._sdlc_synth_ctx = FakeAutomationContext()
        orch._sdlc_materialize_ingest = _mk_async(written)
        certified = []
        async def fake_certify(session_id, _c=certified):
            _c.append(session_id)
        orch._certify_harvested_complete = fake_certify
        assert await orch._sdlc_materialize_chain_ingest(
            "s1", "dark-mode", attempts=2, delay=0.0) == written
        assert certified == expected


_MAT_INI = {"id": "i1", "slug": "dark-mode", "repo": "o/r", "title": "Dark mode"}


def _spawn_materialize_orch(ctx=None, *, plan_body="# the plan", config=None):
    orch = _bare_orch()
    orch._sdlc_synth_ctx = ctx or FakeAutomationContext()
    orch._automation_account_for = _mk_async("acct1")
    orch._effective_repo_root = _mk_async("/tmp/root")
    orch._sdlc_default_branch = _mk_async("master")
    orch._sdlc_synth_read_config = _mk_async(("propose", config or {}))
    orch._sdlc_show_committed_file = lambda cwd, ref, path: f"{plan_body} at {ref}:{path}"
    orch.chained: list = []

    async def fake_chain(session_id, slug, **kw):
        orch.chained.append((session_id, slug))
        return 0

    orch._sdlc_materialize_chain_ingest = fake_chain
    orch.scheduled: list = []
    orch._spawn = lambda coro, name=None: orch.scheduled.append(coro)
    return orch


@pytest.mark.asyncio
async def test_spawn_materialize_seeds_the_plan_and_chains_the_ingest(db_env):
    await _seed_initiative(db_env)
    orch = _spawn_materialize_orch()
    with patch(_ENSURE_CLONE, return_value="/tmp/clone"):
        rec = await orch._sdlc_spawn_materialize(_MAT_INI)
    assert rec is not None
    call = orch._sdlc_synth_ctx.calls[0]
    # Attempt 0 — the key carries the attempt number (C4-R15).
    assert call["origin_ref"] == "dark-mode:materialize:0"
    assert call["dedupe_key"] == "dark-mode:materialize:0"
    assert call["cwd"] == "/tmp/clone"
    assert "docs/sdlc/dark-mode/plan.md" in call["initial_prompt"]
    assert "pull request" in call["initial_prompt"]
    assert len(orch.scheduled) == 1
    await orch.scheduled[0]
    assert orch.chained == [("sess-1", "dark-mode")]


_TASKS_MD = """# Tasks: Dark mode

## Phase 3.1: Setup
- [ ] T001 Create the toggle component in src/Toggle.tsx
- [ ] T002 [P] Wire the theme store

## Phase 3.2: Tests
- [ ] T003 Add a snapshot test
   Covers: FR-001, AC-001
"""


def _spawn_materialize_orch_with_tasks(ctx=None, *, plan_body="# the plan", tasks_md=_TASKS_MD):
    """Same fake as `_spawn_materialize_orch`, but the committed-file seam tells
    plan.md and tasks.md apart by path (#2417) instead of returning the same
    body for every path."""
    orch = _spawn_materialize_orch(ctx, plan_body=plan_body)

    def show(cwd, ref, path):
        if path.endswith("tasks.md"):
            return f"{tasks_md}\n<!-- {ref}:{path} -->"
        return f"{plan_body} at {ref}:{path}"

    orch._sdlc_show_committed_file = show
    return orch


@pytest.mark.asyncio
async def test_spawn_materialize_parses_tasks_md_directly_and_skips_the_session(db_env):
    """#2417: a merged plan whose directory already has a `tasks.md` is
    materialized straight into a pending proposal — no decompose session, no
    chained ingest."""
    from server.db.models import MaterializationProposal
    from server.pm.materialize import MaterializedDirectly
    ini_id = await _seed_initiative(db_env)
    ctx = FakeAutomationContext()
    orch = _spawn_materialize_orch_with_tasks(ctx)
    ini = dict(_MAT_INI, id=ini_id)
    with patch(_ENSURE_CLONE, return_value="/tmp/clone"):
        result = await orch._sdlc_spawn_materialize(ini)
    assert isinstance(result, MaterializedDirectly)
    assert ctx.calls == []       # no decompose session spawned
    assert orch.scheduled == []  # no ingest chained onto anything
    async with db_session() as db:
        from sqlalchemy import select
        rows = (await db.execute(select(MaterializationProposal))).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.id == result.proposal_id
    assert row.initiative_id == ini_id
    assert row.status == "pending"
    assert row.session_id is None
    assert [t["title"] for t in row.payload["tasks"]] == [
        "T001: Create the toggle component in src/Toggle.tsx",
        "T002: Wire the theme store",
        "T003: Add a snapshot test",
    ]
    assert "Covers: FR-001, AC-001" in row.payload["tasks"][2]["body"]
    assert row.payload["source"] == "tasks_file"
    assert row.payload["source_session"] is None


@pytest.mark.asyncio
async def test_spawn_materialize_falls_back_to_a_session_with_no_tasks_md(db_env):
    """The common case (no tasks.md next to the plan) must behave exactly as
    before #2417 — a decompose session, not a silent no-op."""
    await _seed_initiative(db_env)
    orch = _spawn_materialize_orch()
    from server.sessions import orchestrator as orch_mod

    # `_spawn_materialize_orch`'s default fake returns plan text for EVERY
    # path; swap in one that tells the plan and a missing tasks.md apart, the
    # real shape `_sdlc_show_committed_file` returns for a file that isn't there.

    def show(cwd, ref, path):
        if path.endswith("tasks.md"):
            return f"{orch_mod._SDLC_UNREADABLE_PREFIX} `{path}` from `{ref}`"
        return f"# the plan at {ref}:{path}"

    orch._sdlc_show_committed_file = show
    with patch(_ENSURE_CLONE, return_value="/tmp/clone"):
        rec = await orch._sdlc_spawn_materialize(_MAT_INI)
    assert rec is not None
    assert len(orch._sdlc_synth_ctx.calls) == 1
    assert len(orch.scheduled) == 1


@pytest.mark.asyncio
async def test_spawn_materialize_falls_back_when_tasks_md_does_not_parse(db_env):
    """A `tasks.md` that exists but has no `T###` lines must degrade to the
    normal decompose session rather than blocking materialization outright."""
    await _seed_initiative(db_env)
    ctx = FakeAutomationContext()
    orch = _spawn_materialize_orch_with_tasks(ctx, tasks_md="# Tasks: Dark mode\n\nNothing to see here.\n")
    with patch(_ENSURE_CLONE, return_value="/tmp/clone"):
        rec = await orch._sdlc_spawn_materialize(_MAT_INI)
    assert rec is not None
    assert len(ctx.calls) == 1
    kinds = [e[2].get("kind") for e in ctx.events]
    assert "materialization_tasks_unreadable" in kinds


async def _seed_materialize_session(status: str, *, attempt: int = 0) -> str:
    """A decompose session for dark-mode in *status*, as the spawn gate sees it."""
    from server.db.models import Session as DbSession
    sid = f"sess-{status}-{attempt}"
    async with db_session() as db:
        db.add(DbSession(id=sid, name="sdlc materialize dark-mode",
                         account_name="acct1", cwd="/tmp/clone", status=status,
                         origin="sdlc-synthesizer",
                         origin_ref=f"dark-mode:materialize:{attempt}"))
        await db.commit()
    return sid


async def _seed_materialization_proposal(ini_id: str, status: str) -> str:
    from server.db.models import MaterializationProposal
    async with db_session() as db:
        prop = MaterializationProposal(initiative_id=ini_id, status=status,
                                       session_id="sess-old", payload={"tasks": []})
        db.add(prop)
        await db.flush()
        return prop.id


@pytest.mark.asyncio
async def test_spawn_materialize_skips_a_live_decompose_session(db_env):
    """The dedupe key would return the SAME session from spawn_session, and the
    caller would then chain a second ingest onto it and re-emit. Asking first
    makes a tick during the decompose session a no-op (C4-R13a)."""
    await _seed_initiative(db_env)
    await _seed_materialize_session("in_progress")
    ctx = FakeAutomationContext()
    orch = _spawn_materialize_orch(ctx)
    with patch(_ENSURE_CLONE, return_value="/tmp/clone"):
        assert await orch._sdlc_spawn_materialize(_MAT_INI) is None
    assert ctx.calls == []
    assert ctx.plans == []  # not even the launch plan was paid for
    assert orch.scheduled == []


@pytest.mark.asyncio
async def test_spawn_materialize_after_a_reject_starts_a_new_attempt(db_env):
    """C4-R14 + C4-R15, the case that made the reject button a lie.

    The rejected attempt's ``complete`` session is not live (so the gate lets the
    tick through) AND its dedupe key is the previous attempt's, so
    ``spawn_session`` cannot hand it back — without the attempt number it did,
    and the chained ingest re-harvested its transcript into an identical pending
    proposal."""
    ini_id = await _seed_initiative(db_env)
    await _seed_materialization_proposal(ini_id, "rejected")
    old = await _seed_materialize_session("complete", attempt=0)
    ctx = FakeAutomationContext(existing=SimpleNamespace(
        id=old, origin_ref="dark-mode:materialize:0", status="complete"))
    orch = _spawn_materialize_orch(ctx)
    ini = dict(_MAT_INI, id=ini_id)
    with patch(_ENSURE_CLONE, return_value="/tmp/clone"):
        rec = await orch._sdlc_spawn_materialize(ini)
    assert rec is not None and rec.id != old
    assert ctx.calls[0]["dedupe_key"] == "dark-mode:materialize:1"
    assert len(orch.scheduled) == 1
    await orch.scheduled[0]
    assert orch.chained == [(rec.id, "dark-mode")]  # never the rejected session


@pytest.mark.asyncio
async def test_spawn_materialize_never_chains_a_deduped_session(db_env):
    """A finished session on the CURRENT attempt key (its ingest never wrote a
    proposal, so the attempt never moved) is what spawn_session's dedupe would
    hand back. Chaining onto it would re-harvest somebody else's transcript — and
    the refusal must cost nothing: no launch plan, no account probe, no clone."""
    ini_id = await _seed_initiative(db_env)
    old = await _seed_materialize_session("complete", attempt=0)
    ctx = FakeAutomationContext(existing=SimpleNamespace(
        id=old, origin_ref="dark-mode:materialize:0", status="complete"))
    orch = _spawn_materialize_orch(ctx)
    ini = dict(_MAT_INI, id=ini_id)  # no proposals yet → still attempt 0
    with patch(_ENSURE_CLONE, return_value="/tmp/clone"):
        assert await orch._sdlc_spawn_materialize(ini) is None
        assert await orch._sdlc_spawn_materialize(ini) is None
    assert orch.scheduled == []
    assert ctx.calls == []
    assert ctx.plans == []  # not even the launch plan was paid for
    warns = [e for e in ctx.events
             if e[2].get("kind") == "materialization_spawn_deduped"]
    # Latched: the condition holds until the attempt moves, so it is said once.
    assert len(warns) == 1 and warns[0][0] == "warn"


@pytest.mark.asyncio
async def test_spawn_materialize_waits_on_a_session_that_needs_a_human(db_env):
    """A decompose session parked on a question still holds the plan."""
    await _seed_initiative(db_env)
    await _seed_materialize_session("waiting_question")
    ctx = FakeAutomationContext()
    orch = _spawn_materialize_orch(ctx)
    with patch(_ENSURE_CLONE, return_value="/tmp/clone"):
        assert await orch._sdlc_spawn_materialize(_MAT_INI) is None
    assert ctx.plans == []


@pytest.mark.asyncio
async def test_spawn_materialize_skips_an_unreadable_plan(db_env):
    """Seeding the fallback text would have the session invent a decomposition
    out of the slug — worse than no proposal."""
    await _seed_initiative(db_env)
    ctx = FakeAutomationContext()
    orch = _spawn_materialize_orch(ctx, plan_body="(could not inline")
    with patch(_ENSURE_CLONE, return_value="/tmp/clone"):
        assert await orch._sdlc_spawn_materialize(_MAT_INI) is None
    assert ctx.calls == []
    assert orch.scheduled == []
    ev = next(e for e in ctx.events if e[2].get("kind") == "materialization_plan_unreadable")
    assert ev[0] == "warn"


@pytest.mark.asyncio
async def test_spawn_materialize_honours_the_pinned_model(db_env):
    """Same model policy as the spec/plan sessions — an operator pinning a model
    for the synthesizer must reach the decompose session too."""
    await _seed_initiative(db_env)
    orch = _spawn_materialize_orch(config={"default_model": "opus"})
    with patch(_ENSURE_CLONE, return_value="/tmp/clone"):
        await orch._sdlc_spawn_materialize(_MAT_INI)
    for coro in orch.scheduled:  # the chain is asserted elsewhere; don't leak it
        coro.close()
    call = orch._sdlc_synth_ctx.calls[0]
    assert call["model"] == "opus"
    assert call["detail"]["model_source"] == "pinned"


@pytest.mark.asyncio
async def test_spawn_materialize_refused_launch_spends_nothing(db_env):
    await _seed_initiative(db_env)
    orch = _bare_orch()
    orch._sdlc_synth_ctx = FakeAutomationContext(refuse=True)
    assert await orch._sdlc_spawn_materialize(_MAT_INI) is None
    assert orch._sdlc_synth_ctx.calls == []


@pytest.mark.asyncio
async def test_has_pending_materialization_tracks_the_proposal(db_env):
    """The loop's spawn gate and the ingest's duplicate guard must answer off the
    same query — this is the loop half of it."""
    ini_id = await _seed_initiative(db_env)
    orch = _materialize_orch()
    assert await orch._sdlc_has_pending_materialization(ini_id) is False
    assert await orch._sdlc_materialize_ingest("sess-9", "dark-mode") == 1
    assert await orch._sdlc_has_pending_materialization(ini_id) is True


@pytest.mark.asyncio
async def test_spawn_materialize_warns_once_per_slug_about_an_unreadable_plan(db_env):
    """Every tick would otherwise re-emit the same warn forever — the same
    once-per-slug suppression the loop's `materialization_unwired` warn has."""
    await _seed_initiative(db_env)
    ctx = FakeAutomationContext()
    orch = _spawn_materialize_orch(ctx, plan_body="(could not inline")
    with patch(_ENSURE_CLONE, return_value="/tmp/clone"):
        assert await orch._sdlc_spawn_materialize(_MAT_INI) is None
        assert await orch._sdlc_spawn_materialize(_MAT_INI) is None
    warns = [e for e in ctx.events if e[2].get("kind") == "materialization_plan_unreadable"]
    assert len(warns) == 1


async def _seed_proposal(db_url: str) -> tuple[str, str]:
    from server.db.models import MaterializationProposal
    ini_id = await _seed_initiative(db_url)
    async with db_session() as db:
        prop = MaterializationProposal(
            initiative_id=ini_id, status="pending", session_id="sess-9",
            payload={"epic_title": "Dark mode", "epic_body": "Ship it.",
                     "tasks": [{"title": "Tokens", "body": "b0", "depends_on": []},
                               {"title": "Toggle", "body": "b1", "depends_on": [0]}],
                     "source_session": "sess-9"})
        db.add(prop)
        await db.flush()
        return ini_id, prop.id


def _apply_orch(payloads):
    """Orchestrator whose mutate seam replays *payloads* (one per document)."""
    orch = _materialize_orch()
    orch.mutated: list = []

    async def fake_mutate(owner, doc):
        orch.mutated.append((owner, doc))
        return payloads.pop(0)

    orch._pm_reconcile_mutate = fake_mutate
    return orch


def _issue(n: int) -> dict:
    return {"data": {"createIssue": {"issue": {
        "id": f"I_{n}", "number": 100 + n,
        "url": f"https://github.com/o/r/issues/{100 + n}"}}}}


@pytest.mark.asyncio
async def test_materialize_apply_seam_emits_materialized(db_env):
    """The API calls THIS; the router must never build a GitHub call itself."""
    from server.db.models import Initiative, MaterializationProposal
    ini_id, prop_id = await _seed_proposal(db_env)
    orch = _apply_orch([
        {"data": {"repository": {"id": "R_1"}}}, _issue(1), _issue(2), _issue(3),
        {"data": {"m0": {"clientMutationId": None}}},
    ])
    out = await orch._sdlc_materialize_apply(prop_id)
    assert out["ok"] is True
    assert out["tasks"] == 2 and out["edges"] == 1
    ev = next(e for e in orch._sdlc_synth_ctx.events if e[2].get("kind") == "materialized")
    assert ev[0] == "info"
    assert ev[2]["detail"]["tasks"] == 2
    # A chain that dropped an edge must be visible in the feed, not only in the journal.
    assert ev[2]["detail"]["parkedEdges"] == 0
    async with db_session() as db:
        from sqlalchemy import select
        ini = (await db.execute(select(Initiative).where(
            Initiative.id == ini_id))).scalar_one()
        assert (ini.epic_repo, ini.epic_number) == ("o/r", 101)
        prop = (await db.execute(select(MaterializationProposal).where(
            MaterializationProposal.id == prop_id))).scalar_one()
        assert prop.status == "accepted"


@pytest.mark.asyncio
async def test_materialize_apply_seam_emits_the_failure(db_env):
    _, prop_id = await _seed_proposal(db_env)
    orch = _apply_orch([
        {"data": {"repository": {"id": "R_1"}}}, _issue(1),
        {"data": {"createIssue": None}, "errors": [{"message": "nope"}]},
    ])
    out = await orch._sdlc_materialize_apply(prop_id)
    assert out["ok"] is False
    assert out["step"] == "task:0"
    ev = next(e for e in orch._sdlc_synth_ctx.events
              if e[2].get("kind") == "materialization_failed")
    assert ev[0] == "warn"
    assert ev[2]["detail"]["step"] == "task:0"


@pytest.mark.asyncio
async def test_materialize_apply_seam_404s_on_an_unknown_proposal(db_env):
    await _seed_initiative(db_env)
    orch = _apply_orch([])
    out = await orch._sdlc_materialize_apply("nope")
    assert out == {"ok": False, "step": "load", "error": "no such proposal: nope"}


@pytest.mark.asyncio
async def test_materialize_apply_advances_the_initiative_to_build(db_env):
    """Materialize is the plan stage's TERMINAL action (spec §7b): the accept a
    human just confirmed IS that stage's gate, so the initiative moves on rather
    than waiting at ``plan`` for a second tap asking the same question."""
    from server.db.models import Initiative
    ini_id, prop_id = await _seed_proposal(db_env)
    orch = _apply_orch([
        {"data": {"repository": {"id": "R_1"}}}, _issue(1), _issue(2), _issue(3),
        {"data": {"m0": {"clientMutationId": None}}},
    ])
    out = await orch._sdlc_materialize_apply(prop_id)
    assert out["stage"] == "build"
    async with db_session() as db:
        from sqlalchemy import select
        ini = (await db.execute(select(Initiative).where(
            Initiative.id == ini_id))).scalar_one()
        assert (ini.stage, ini.status) == ("build", "active")


@pytest.mark.asyncio
async def test_a_second_apply_of_the_same_proposal_is_refused_while_one_runs(db_env):
    """C4-R17: the resume key is the ``created`` map the apply commits between
    calls, so two concurrent runs would both read 'nothing created yet' and both
    create the epic. The second caller is refused, not queued."""
    import asyncio
    _, prop_id = await _seed_proposal(db_env)
    orch = _apply_orch([
        {"data": {"repository": {"id": "R_1"}}}, _issue(1), _issue(2), _issue(3),
        {"data": {"m0": {"clientMutationId": None}}},
    ])
    started, release = asyncio.Event(), asyncio.Event()
    inner = orch._pm_reconcile_mutate

    async def gated(owner, doc):
        if "repository(" in doc:  # the first call of the apply — hold it open
            started.set()
            await release.wait()
        return await inner(owner, doc)

    orch._pm_reconcile_mutate = gated
    first = asyncio.create_task(orch._sdlc_materialize_apply(prop_id))
    await started.wait()
    second = await orch._sdlc_materialize_apply(prop_id)
    release.set()
    out = await first

    assert second == {"ok": False, "step": "in_flight",
                      "error": "an apply for this proposal is already running"}
    assert out["ok"] is True
    # ONE epic and its two tasks — the refused call reached no GitHub document.
    assert len([d for _o, d in orch.mutated if "createIssue" in d]) == 3
    # The lock is dropped once the run ends: a daemon must not keep one per
    # proposal it has ever applied.
    assert orch._materialize_apply_locks == {}


@pytest.mark.asyncio
async def test_materialize_ingest_parse_failure_records_a_rejected_proposal(db_env):
    """C4-R19b: a rejected row, NOT a blocked initiative. Blocking was a dead end
    (``next_action`` answers NeedsHuman for it), and the row is also what moves the
    attempt key so the next tick spawns a fresh decompose session."""
    from server.db.models import Initiative, MaterializationProposal
    ini_id = await _seed_initiative(db_env)
    orch = _materialize_orch("I could not do it, sorry.")
    assert await orch._sdlc_materialize_ingest("sess-9", "dark-mode") == 0
    async with db_session() as db:
        from sqlalchemy import select
        prop = (await db.execute(select(MaterializationProposal))).scalar_one()
        assert prop.status == "rejected"
        assert prop.session_id == "sess-9"
        assert prop.resolved_at is not None
        assert prop.rationale.startswith("parse failed: ")
        assert prop.payload["source_session"] == "sess-9"
        assert prop.payload["parse_error"]
        ini = (await db.execute(select(Initiative).where(
            Initiative.slug == "dark-mode"))).scalar_one()
        assert ini.status == "awaiting_human"  # still the loop's to retry
    # A failed attempt still counts, so the next decompose gets its own key.
    attempts = await orch._sdlc_materialization_attempts_for(ini_id)
    # Both counts, and they agree here: nothing has been retried, so the ordinal
    # the dedupe key uses and the budget the cap is measured against are the same
    # number. They diverge once a human taps "Decompose again" (#1582).
    assert (attempts.total, attempts.budget) == (1, 1)
    # …and it is not a proposal anyone can be asked to review.
    assert await orch._sdlc_has_pending_materialization(ini_id) is False


@pytest.mark.asyncio
async def test_the_attempts_seam_reads_the_retry_baseline(db_env):
    """#1582: the DB-backed seam is where `materialize_retry_baseline` is applied.

    The pure derivation and the route that writes the column are covered
    elsewhere; this is the read in between — the one the loop's exhaustion gate and
    the unblock refusal both go through. `total` must ignore the baseline entirely
    (it is the decompose session's dedupe ordinal, and a number that came round
    twice re-ingests a finished session — C4-R15) while `budget` must honour it."""
    from sqlalchemy import select

    from server.db.models import Initiative, MaterializationProposal
    ini_id = await _seed_initiative(db_env)
    orch = _bare_orch()
    async with db_session() as db:
        for _ in range(4):
            db.add(MaterializationProposal(initiative_id=ini_id, status="rejected",
                                           session_id="s", payload={}))
        await db.flush()

    before = await orch._sdlc_materialization_attempts_for(ini_id)
    assert (before.total, before.budget) == (4, 4)   # NULL baseline == 0

    async with db_session() as db:
        ini = (await db.execute(select(Initiative).where(
            Initiative.id == ini_id))).scalar_one()
        ini.materialize_retry_baseline = 3

    after = await orch._sdlc_materialization_attempts_for(ini_id)
    assert (after.total, after.budget) == (4, 1)     # the ordinal never moves back

    # A baseline ABOVE the total — rows deleted by hand after a retry, which should
    # not happen — floors at zero rather than making the gate unreachable.
    async with db_session() as db:
        ini = (await db.execute(select(Initiative).where(
            Initiative.id == ini_id))).scalar_one()
        ini.materialize_retry_baseline = 99
    impossible = await orch._sdlc_materialization_attempts_for(ini_id)
    assert (impossible.total, impossible.budget) == (4, 0)


@pytest.mark.asyncio
async def test_materialize_chain_ingest_spends_the_settle_budget(db_env, monkeypatch):
    """C4-R20: the decompose chain waits on the 2 h budget, not the classify
    chain's 30 min, and a give-up says so instead of blocking the initiative."""
    from server.sessions.orchestrator import _MATERIALIZE_SETTLE_ATTEMPTS
    await _seed_initiative(db_env)
    orch = _bare_orch()
    orch._sdlc_synth_ctx = FakeAutomationContext()
    polls = 0

    async def _never_settles(sid):
        nonlocal polls
        polls += 1
        return _rec("running")

    orch._session_settle_signal = _never_settles
    import asyncio as _a
    monkeypatch.setattr(_a, "sleep", _mk_async(None))
    assert await orch._sdlc_materialize_chain_ingest("s1", "dark-mode",
                                                     delay=0.0) == 0
    assert polls == _MATERIALIZE_SETTLE_ATTEMPTS == 240
    ev = next(e for e in orch._sdlc_synth_ctx.events
              if e[2].get("kind") == "materialization_ingest_timeout")
    assert ev[0] == "warn"
    assert ev[2]["detail"]["attempts"] == _MATERIALIZE_SETTLE_ATTEMPTS
    async with db_session() as db:
        from sqlalchemy import select
        from server.db.models import Initiative
        ini = (await db.execute(select(Initiative).where(
            Initiative.slug == "dark-mode"))).scalar_one()
        assert ini.status == "awaiting_human"  # NOT blocked


@pytest.mark.asyncio
async def test_apply_feature_plan_writes_route_labels(db_env):
    await init_db(db_env)
    async with db_session() as db:
        p = Project(slug="lbl", name="lbl", onboarding_state="live")
        db.add(p)
        await db.flush()
        project_id = p.id
    orch = _bare_orch()
    from server.pm.feature_index import FeatureIndexPlan, FeatureUpsert
    plan = FeatureIndexPlan(
        upserts=(FeatureUpsert(slug="flaky-test-triage", title="Flaky", state="live", summary="s",
                               repo_path="docs/product/features/flaky-test-triage.md",
                               parent_slug=None, links=(), labels=("flaky",)),
                 FeatureUpsert(slug="task-views", title="Views", state="live", summary="s",
                               repo_path="docs/product/features/task-views.md",
                               parent_slug=None, links=())),
        orphan_slugs=(), revive_slugs=(), warnings=())
    await orch._pm_sync_apply_feature_plan(project_id, plan)
    async with db_session() as db:
        from sqlalchemy import select
        rows = {f.slug: f.route_labels for f in (await db.execute(
            select(Feature).where(Feature.project_id == project_id))).scalars().all()}
    assert rows == {"flaky-test-triage": ["flaky"], "task-views": None}


# --------------------------------------------------------------------------- #
# _pm_onboard_observe_map_diff_not_needed — the harness reads the repository  #
# --------------------------------------------------------------------------- #
#
# A map-diff session answering `mark_no_pr` (not-needed) lands on source "tool" —
# a self-claim — and the completion guard nudged it toward a PR it rightly refused
# to open (prod 2026-09-18, session 91bf96b6). The seam below is the harness's own
# read of `origin/<default>`: the driving items already cited in the feature docs,
# or the suggested doc already present, is the deliverable being met.

_OBS_REPO = "steiner385/viberpit"
_DOCS = "docs/product/features"


def _clone_with_docs(root: str, docs: dict[str, str]) -> str:
    """A local clone whose fetchable origin/main carries the fixture docs."""
    # The seam resolves the clone through the orchestrator's own gitrepos binding,
    # so the test derives the path from the SAME binding rather than re-importing.
    from server.sessions import orchestrator as _orch_mod
    clone = _orch_mod.gitrepos.clone_path(root, _OBS_REPO)
    clone.mkdir(parents=True)
    _git("init", "-q", cwd=clone)
    _git("checkout", "-q", "-b", "main", cwd=clone)
    _git("config", "user.email", "t@example.com", cwd=clone)
    _git("config", "user.name", "t", cwd=clone)
    for path, text in docs.items():
        p = clone / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git("add", "-A", cwd=clone)
    _git("commit", "-q", "--allow-empty", "-m", "docs", cwd=clone)
    _git("remote", "add", "origin", str(clone), cwd=clone)
    _git("fetch", "-q", "origin", cwd=clone)
    return str(clone)


async def _no_fit_cluster(suggestion: str) -> None:
    """Every seeded work item (#100–#102) becomes an accepted no_fit proposal
    carrying *suggestion* — the cluster the map-diff session was spawned for."""
    async with db_session() as db:
        from sqlalchemy import select
        items = (await db.execute(select(WorkItem).where(
            WorkItem.repo == _OBS_REPO))).scalars().all()
        for w in items:
            db.add(TriageProposal(work_item_id=w.id, status="accepted",
                                  no_fit_suggestion=suggestion))


def _map_diff_rec(suggestion: str = "offline-mode", *, origin: str | None = "pm-onboard"):
    from server.sessions.engine import AgentEngine
    from server.sessions.types import SessionRecord
    rec = SessionRecord(id="s1", name="pm map-diff viberpit", account="a",
                        engine=AgentEngine.CLAUDE_CODE, engine_session_id="",
                        cols=80, rows=24, cwd="/tmp", repo=_OBS_REPO, branch="pm/x")
    rec.origin = origin
    rec.origin_ref = f"viberpit:map-diff:{suggestion}"
    return rec


def _observe_orch(root: str):
    from server.automations.context import AutomationContext
    orch = _bare_orch()
    orch._pm_onboard_ctx = AutomationContext("pm-onboard")

    async def _default_branch(repo: str) -> str:
        return "main"

    async def _repo_root() -> str:
        return root

    orch._sdlc_default_branch = _default_branch
    orch._effective_repo_root = _repo_root
    return orch


@pytest.mark.asyncio
async def test_observe_confirms_when_all_driving_items_are_already_cited(db_env):
    await _seed(db_env)
    await _no_fit_cluster("offline-mode")
    with tempfile.TemporaryDirectory() as root:
        _clone_with_docs(root, {
            f"{_DOCS}/gtk-targets.md": "# GTK\n\n## Evidence\n- #100 and #101 and #102 argue this.\n"})
        assert await _observe_orch(root)._pm_onboard_observe_map_diff_not_needed(
            _map_diff_rec()) is True


@pytest.mark.asyncio
async def test_observe_declines_when_only_a_minority_is_cited(db_env):
    await _seed(db_env)
    await _no_fit_cluster("offline-mode")
    with tempfile.TemporaryDirectory() as root:
        _clone_with_docs(root, {
            f"{_DOCS}/gtk-targets.md": "# GTK\n\n## Evidence\n- #100 only.\n"})
        assert await _observe_orch(root)._pm_onboard_observe_map_diff_not_needed(
            _map_diff_rec()) is False


@pytest.mark.asyncio
async def test_observe_confirms_when_the_suggested_doc_already_exists(db_env):
    """The slug landed (a sibling session, a human) but pm-sync has not indexed it
    yet, so latch (a) is silent: the file on the default branch is the fact."""
    await _seed(db_env)
    await _no_fit_cluster("offline-mode")
    with tempfile.TemporaryDirectory() as root:
        _clone_with_docs(root, {f"{_DOCS}/offline-mode.md": "# Offline\n"})
        assert await _observe_orch(root)._pm_onboard_observe_map_diff_not_needed(
            _map_diff_rec()) is True


@pytest.mark.asyncio
async def test_observe_is_a_map_diff_seam_only(db_env):
    """A human session, or any origin but pm-onboard, is never observed here —
    it goes to the salvage classifier exactly as before. No clone is needed to
    answer, so none is built."""
    await _seed(db_env)
    with tempfile.TemporaryDirectory() as root:
        orch = _observe_orch(root)
        assert await orch._pm_onboard_observe_map_diff_not_needed(
            _map_diff_rec(origin=None)) is False
        human = _map_diff_rec()
        human.origin_ref = "steiner385/viberpit#12"
        assert await orch._pm_onboard_observe_map_diff_not_needed(human) is False


@pytest.mark.asyncio
async def test_observe_declines_without_a_readable_clone(db_env):
    """No clone on disk (or an unresolvable ref) is "unobserved", never a crash:
    the claim falls through to the classifier."""
    await _seed(db_env)
    await _no_fit_cluster("offline-mode")
    with tempfile.TemporaryDirectory() as root:
        assert await _observe_orch(root)._pm_onboard_observe_map_diff_not_needed(
            _map_diff_rec()) is False


@pytest.mark.asyncio
async def test_map_resolution_survives_deleted_session_and_new_orchestrator(db_env):
    from sqlalchemy import delete, select
    from server.db.models import Session as DbSession
    await _seed(db_env)
    await _no_fit_cluster("offline-mode")
    with tempfile.TemporaryDirectory() as root:
        _clone_with_docs(root, {f"{_DOCS}/renamed.md": "# Feature\n#100 #101 #102\n"})
        orch = _observe_orch(root)
        async with db_session() as db:
            db.add(DbSession(id="s1", name="map diff", account_name="a", cwd="/tmp",
                             origin="pm-onboard", origin_ref="viberpit:map-diff:offline-mode",
                             repo=_OBS_REPO, status="archived", archived_by="completion"))
        assert await orch._pm_onboard_observe_map_diff_not_needed(_map_diff_rec())
        async with db_session() as db:
            await db.execute(delete(DbSession).where(DbSession.id == "s1"))
            proposals = (await db.execute(select(TriageProposal))).scalars().all()
            assert all(p.map_resolution["commit"] for p in proposals)
        fresh = _observe_orch(root)
        state = await fresh._pm_triage_count_state()
        assert state.no_fit_clusters == ()
        async with db_session() as db:
            item = (await db.execute(select(WorkItem).where(WorkItem.number == 100))).scalars().one()
            item.body = "new scope for the same work"
        state = await fresh._pm_triage_count_state()
        assert state.no_fit_clusters[0].item_numbers == (100,)


@pytest.mark.asyncio
async def test_map_resolution_never_disposes_uncovered_minority(db_env):
    from sqlalchemy import select
    await _seed(db_env)
    await _no_fit_cluster("offline-mode")
    with tempfile.TemporaryDirectory() as root:
        _clone_with_docs(root, {f"{_DOCS}/renamed.md": "# Feature\n#100 #101\n"})
        assert not await _observe_orch(root)._pm_onboard_observe_map_diff_not_needed(_map_diff_rec())
        async with db_session() as db:
            assert all(p.map_resolution is None for p in
                       (await db.execute(select(TriageProposal))).scalars())


@pytest.mark.asyncio
async def test_map_preflight_resolves_renamed_doc_without_spawning(db_env):
    await _seed(db_env)
    await _no_fit_cluster("offline-mode")
    with tempfile.TemporaryDirectory() as root:
        _clone_with_docs(root, {f"{_DOCS}/renamed.md": "# Feature\n#100 #101 #102\n"})
        orch = _observe_orch(root)
        async def never_spawn(*args, **kwargs):
            raise AssertionError("covered cluster must never launch a session")
        orch.pm_onboard_spawn_map = never_spawn
        assert await orch._pm_triage_propose_map_diff("offline-mode", (100, 101, 102), _OBS_REPO) is None
        assert (await orch._pm_triage_count_state()).no_fit_clusters == ()


@pytest.mark.asyncio
async def test_human_map_disposition_survives_cleanup_but_new_proposals_are_eligible(db_env):
    from sqlalchemy import select
    await _seed(db_env)
    await _no_fit_cluster("offline-mode")
    orch = _observe_orch("/missing-clone")
    assert await orch._pm_onboard_observe_map_diff_not_needed(
        _map_diff_rec(), disposition_source="human-archive")
    assert (await _observe_orch("/missing")._pm_triage_count_state()).no_fit_clusters == ()
    async with db_session() as db:
        item = (await db.execute(select(WorkItem).where(WorkItem.number == 100))).scalars().one()
        db.add(TriageProposal(work_item_id=item.id, status="accepted", no_fit_suggestion="offline-mode"))
    state = await _observe_orch("/missing")._pm_triage_count_state()
    assert state.no_fit_clusters[0].item_numbers == (100,)


@pytest.mark.asyncio
async def test_observation_does_not_absorb_proposals_arriving_during_git_read(db_env, monkeypatch):
    from sqlalchemy import select
    await _seed(db_env)
    await _no_fit_cluster("offline-mode")
    async def raced_read(*args, **kwargs):
        async with db_session() as db:
            item = (await db.execute(select(WorkItem).where(WorkItem.number == 100))).scalars().one()
            db.add(TriageProposal(id="new-proposal", work_item_id=item.id, status="accepted",
                                  no_fit_suggestion="offline-mode"))
        return False, {100, 101, 102}, "verified-sha", ["renamed.md"]
    monkeypatch.setattr("server.sessions.orchestrator.asyncio.to_thread", raced_read)
    assert not await _observe_orch("/missing")._pm_onboard_observe_map_diff_not_needed(_map_diff_rec())
    async with db_session() as db:
        assert (await db.get(TriageProposal, "new-proposal")).map_resolution is None
    state = await _observe_orch("/missing")._pm_triage_count_state()
    assert state.no_fit_clusters[0].item_numbers == (100,)


@pytest.mark.asyncio
async def test_observation_rejects_stale_cached_ref(db_env):
    await _seed(db_env)
    await _no_fit_cluster("offline-mode")
    with tempfile.TemporaryDirectory() as root:
        clone = _clone_with_docs(root, {f"{_DOCS}/offline-mode.md": "# Offline\n"})
        _git("remote", "set-url", "origin", "/nonexistent-remote", cwd=clone)
        assert not await _observe_orch(root)._pm_onboard_observe_map_diff_not_needed(_map_diff_rec())


@pytest.mark.asyncio
async def test_preflight_preserves_legacy_human_disposition(db_env):
    from datetime import datetime, timezone
    from sqlalchemy import select
    from server.db.models import Session as DbSession
    await _seed(db_env)
    await _no_fit_cluster("offline-mode")
    async with db_session() as db:
        db.add(DbSession(id="legacy", name="map diff", account_name="a", cwd="/tmp",
                         origin="pm-onboard", origin_ref="viberpit:map-diff:offline-mode",
                         repo=_OBS_REPO, status="archived", archived_by="user",
                         archived_at=datetime.now(timezone.utc)))
    orch = _observe_orch("/no-clone")
    assert await orch._pm_triage_propose_map_diff("offline-mode", (100, 101, 102), _OBS_REPO) is None
    async with db_session() as db:
        assert all(p.map_resolution["source"] == "human-archive" for p in
                   (await db.execute(select(TriageProposal))).scalars())


@pytest.mark.asyncio
async def test_citations_to_other_repository_do_not_resolve_local_work(db_env):
    await _seed(db_env)
    await _no_fit_cluster("offline-mode")
    with tempfile.TemporaryDirectory() as root:
        _clone_with_docs(root, {f"{_DOCS}/renamed.md": "other/repo#100 other/repo#101 other/repo#102\n"})
        assert not await _observe_orch(root)._pm_onboard_observe_map_diff_not_needed(_map_diff_rec())


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [None, "human-archive"])
async def test_changed_scope_is_not_disposed_by_unchanged_document_citations(db_env, source):
    from sqlalchemy import select
    await _seed(db_env)
    await _no_fit_cluster("offline-mode")
    with tempfile.TemporaryDirectory() as root:
        _clone_with_docs(root, {f"{_DOCS}/renamed.md": "#100 #101 #102\n"})
        orch = _observe_orch(root)
        assert await orch._pm_onboard_observe_map_diff_not_needed(
            _map_diff_rec(), disposition_source=source)
        async with db_session() as db:
            item = (await db.execute(select(WorkItem).where(WorkItem.number == 100))).scalars().one()
            item.body = "Completely new scope"
        assert not await orch._pm_onboard_observe_map_diff_not_needed(_map_diff_rec())
        assert (await orch._pm_triage_count_state()).no_fit_clusters[0].item_numbers == (100,)
        async def spawn_changed_work(*args, **kwargs):
            assert kwargs["item_numbers"] == (100,)
            return "new-scope-session"
        orch.pm_onboard_spawn_map = spawn_changed_work
        assert await orch._pm_triage_propose_map_diff(
            "offline-mode", (100,), _OBS_REPO) == "new-scope-session"
        assert (await orch._pm_triage_count_state()).no_fit_clusters[0].item_numbers == (100,)
        # A fresh operator decision is allowed to dispose of the changed scope.
        assert await orch._pm_onboard_observe_map_diff_not_needed(
            _map_diff_rec(), disposition_source="human-archive")
        assert (await orch._pm_triage_count_state()).no_fit_clusters == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("citation", [
    "[#100](https://github.com/other/repo/issues/100) [#101](https://github.com/other/repo/issues/101) [#102](https://github.com/other/repo/issues/102)",
    "[#100][foreign] [#101][foreign] [#102][foreign]\n[foreign]: https://github.com/other/repo/issues/100",
])
async def test_foreign_markdown_citations_do_not_resolve_local_work(db_env, citation):
    await _seed(db_env)
    await _no_fit_cluster("offline-mode")
    with tempfile.TemporaryDirectory() as root:
        _clone_with_docs(root, {f"{_DOCS}/renamed.md": citation})
        assert not await _observe_orch(root)._pm_onboard_observe_map_diff_not_needed(_map_diff_rec())


@pytest.mark.asyncio
@pytest.mark.parametrize("citation", [
    "steiner385/viberpit#100 steiner385/viberpit#101 steiner385/viberpit#102",
    "[#100](https://github.com/steiner385/viberpit/issues/100) [#101](https://github.com/steiner385/viberpit/issues/101) [#102](https://github.com/steiner385/viberpit/issues/102)",
])
async def test_same_repository_qualified_citations_resolve_local_work(db_env, citation):
    await _seed(db_env)
    await _no_fit_cluster("offline-mode")
    with tempfile.TemporaryDirectory() as root:
        _clone_with_docs(root, {f"{_DOCS}/renamed.md": citation})
        assert await _observe_orch(root)._pm_onboard_observe_map_diff_not_needed(_map_diff_rec())
