"""An unmarked map-diff turn finishes only on independently verified evidence."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from server.tests.test_completion_releases_driver import _build_orch, _drain, _FakeDriver


async def _map_orch(tmp_path):
    orch = await _build_orch(
        tmp_path, origin="pm-onboard", origin_ref="viberpit:map-diff:offline-mode",
        repo="steiner385/viberpit")
    orch._drivers["s1"] = _FakeDriver()
    orch._completion_open_question = AsyncMock(return_value=None)
    orch._pm_onboard_observe_map_diff_not_needed = AsyncMock(return_value=True)
    orch._read_auto_archive_enabled = AsyncMock(return_value=False)
    orch._maybe_apply_auto_name = AsyncMock()
    return orch


@pytest.mark.asyncio
async def test_unmarked_map_diff_turn_is_verified_and_releases_driver(tmp_path):
    orch = await _map_orch(tmp_path)
    driver = orch._drivers["s1"]
    await orch._persist_status("s1", "done")
    await _drain(orch)
    await _drain(orch)
    rec = orch.record("s1")
    assert rec.status == "complete"
    assert rec.pr_resolution_source == "observed"
    assert driver.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("hold", ["waiting_question", "waiting_permission", "waiting_plan"])
async def test_unmarked_map_diff_preserves_open_holds(tmp_path, hold):
    orch = await _map_orch(tmp_path)
    orch._completion_open_question.return_value = hold
    await orch._persist_status("s1", "done")
    await _drain(orch)
    orch._pm_onboard_observe_map_diff_not_needed.assert_not_awaited()
    assert orch.record("s1").pr_resolution is None
    assert not orch._drivers["s1"].closed


@pytest.mark.asyncio
async def test_unmarked_map_diff_unverified_keeps_driver(tmp_path):
    orch = await _map_orch(tmp_path)
    orch._pm_onboard_observe_map_diff_not_needed.return_value = False
    await orch._persist_status("s1", "done")
    await _drain(orch)
    assert orch.record("s1").status == "done"
    assert orch.record("s1").pr_resolution is None
    assert not orch._drivers["s1"].closed


@pytest.mark.asyncio
async def test_unmarked_map_diff_respects_manual_opt_out(tmp_path):
    orch = await _map_orch(tmp_path)
    orch.record("s1").auto_archive_opt_out_reason = "manual-unarchive"
    await orch._persist_status("s1", "done")
    await _drain(orch)
    orch._pm_onboard_observe_map_diff_not_needed.assert_not_awaited()
    assert orch.record("s1").status == "done"


@pytest.mark.asyncio
async def test_new_turn_during_repository_observation_is_not_completed(tmp_path):
    orch = await _map_orch(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    async def observe(rec):
        entered.set()
        await release.wait()
        return True

    orch._pm_onboard_observe_map_diff_not_needed.side_effect = observe
    await orch._persist_status("s1", "done")
    await asyncio.wait_for(entered.wait(), timeout=2)
    await orch._persist_status("s1", "in_progress")
    release.set()
    await _drain(orch)
    assert orch.record("s1").status == "in_progress"
    assert orch.record("s1").pr_resolution is None
    assert not orch._drivers["s1"].closed


@pytest.mark.asyncio
async def test_question_check_failure_leaves_unmarked_turn_alone(tmp_path):
    orch = await _map_orch(tmp_path)
    orch._completion_open_question.side_effect = RuntimeError("unavailable")
    await orch._persist_status("s1", "done")
    await _drain(orch)
    orch._pm_onboard_observe_map_diff_not_needed.assert_not_awaited()
    assert orch.record("s1").status == "done"


@pytest.mark.asyncio
async def test_user_typing_during_verification_preserves_unmarked_turn(tmp_path):
    orch = await _map_orch(tmp_path)

    async def observe(rec):
        orch.note_user_interaction("s1")
        return True

    orch._pm_onboard_observe_map_diff_not_needed.side_effect = observe
    await orch._persist_status("s1", "done")
    await _drain(orch)
    assert orch.record("s1").status == "done"
    assert orch.record("s1").pr_resolution is None


@pytest.mark.asyncio
async def test_human_archive_persists_disposition_before_teardown(tmp_path):
    orch = await _map_orch(tmp_path)
    driver = orch._drivers["s1"]

    async def observe(rec, *, disposition_source=None):
        assert orch._drivers["s1"] is driver
        assert disposition_source == "human-archive"
        return True

    orch._pm_onboard_observe_map_diff_not_needed.side_effect = observe
    await orch.archive_session("s1")
    await _drain(orch)
    orch._pm_onboard_observe_map_diff_not_needed.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_human_disposition_write_prevents_loss_of_session(tmp_path):
    orch = await _map_orch(tmp_path)
    orch._pm_onboard_observe_map_diff_not_needed.side_effect = RuntimeError("DB unavailable")
    with pytest.raises(RuntimeError, match="DB unavailable"):
        await orch.archive_session("s1")
    assert orch.record("s1") is not None
    assert not orch._drivers["s1"].closed


@pytest.mark.asyncio
async def test_real_archive_delete_and_fresh_triage_preserve_disposition(tmp_path):
    from server.db import db_session
    from server.db.models import Project, ProjectRepo, Session, TriageProposal, WorkItem
    from server.tests.test_pm_triage_seams import _triage_orch

    orch = await _map_orch(tmp_path)
    # Exercise the real PM persistence and session cleanup, stubbing only filesystem
    # cleanup and unrelated background retention scheduling.
    del orch._pm_onboard_observe_map_diff_not_needed
    orch._enforce_archive_limit = AsyncMock()
    orch._delete_legacy_archive_dir = AsyncMock(return_value=True)
    async with db_session() as db:
        project = Project(slug="viberpit", name="ViberPit")
        db.add(project)
        await db.flush()
        db.add(ProjectRepo(project_id=project.id, repo="steiner385/viberpit", is_primary=True))
        item = WorkItem(repo="steiner385/viberpit", kind="issue", number=100,
                        title="Offline", state="open", triage_state="classified")
        db.add(item)
        await db.flush()
        proposal = TriageProposal(work_item_id=item.id, status="accepted",
                                  no_fit_suggestion="offline-mode")
        db.add(proposal)
        await db.flush()
        proposal_id = proposal.id
    await orch.archive_session("s1")
    await _drain(orch)
    assert await orch.permanent_delete("s1")
    async with db_session() as db:
        assert await db.get(Session, "s1") is None
        row = await db.get(TriageProposal, proposal_id)
        assert row.map_resolution["source"] == "human-archive"
    fresh = _triage_orch()
    assert (await fresh._pm_triage_count_state()).no_fit_clusters == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("late_change", [None, "proposal", "body", "acceptance"])
@pytest.mark.parametrize("archive_via", ["completion", "autopilot", "autopilot-without-record"])
async def test_classifier_map_disposition_survives_cleanup_and_respects_dispatch_scope(
        tmp_path, late_change, archive_via):
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import Project, ProjectRepo, Session, TriageProposal, WorkItem
    from server.pm.map_resolution import map_resolution_inputs
    from server.tests.test_pm_triage_seams import _triage_orch

    orch = await _map_orch(tmp_path)
    del orch._pm_onboard_observe_map_diff_not_needed
    orch._read_auto_archive_enabled = AsyncMock(return_value=True)
    orch._enforce_archive_limit = AsyncMock()
    orch._delete_legacy_archive_dir = AsyncMock(return_value=True)
    before = datetime.now(timezone.utc) - timedelta(minutes=5)
    dispatch = before + timedelta(minutes=2)
    async with db_session() as db:
        project = Project(slug="viberpit", name="ViberPit")
        db.add(project)
        await db.flush()
        db.add(ProjectRepo(project_id=project.id, repo="steiner385/viberpit", is_primary=True))
        item = WorkItem(repo="steiner385/viberpit", kind="issue", number=100,
                        title="Offline", body="old scope", state="open", triage_state="classified",
                        gh_updated_at=before)
        db.add(item)
        await db.flush()
        proposal = TriageProposal(work_item_id=item.id, status="accepted",
                                  no_fit_suggestion="offline-mode", created_at=before,
                                  resolved_at=before)
        db.add(proposal)
        await db.flush()
        proposal.map_resolution = {"source": "observed", "inputs": map_resolution_inputs(proposal, item)}
        item.body = "Expanded scope evaluated by the new session"
        if late_change == "body":
            item.gh_updated_at = dispatch + timedelta(minutes=1)
        if late_change == "acceptance":
            proposal.resolved_at = dispatch + timedelta(minutes=1)
        if late_change == "proposal":
            db.add(TriageProposal(work_item_id=item.id, status="accepted",
                                  no_fit_suggestion="offline-mode",
                                  created_at=dispatch + timedelta(minutes=1)))
        rec = orch.record("s1")
        rec.created_at = dispatch
        rec.pr_resolution = "not-needed"
        rec.pr_resolution_source = "classifier"
        rec.status = "complete"
        row = await db.get(Session, "s1")
        row.created_at = dispatch
        row.pr_resolution = "not-needed"
        row.pr_resolution_source = "classifier"
    if archive_via == "autopilot-without-record":
        orch._records.pop("s1")
    if archive_via == "completion":
        await orch._auto_archive_completed("s1")
    else:
        assert await orch._autopilot_archive("s1")
    await _drain(orch)
    assert await orch.permanent_delete("s1")
    async with db_session() as db:
        assert await db.get(Session, "s1") is None
        proposals = (await db.execute(select(TriageProposal))).scalars().all()
        classifier_count = sum(p.map_resolution is not None and
                               p.map_resolution.get("source") == "classifier" for p in proposals)
        assert classifier_count == (0 if late_change in ("body", "acceptance") else 1)
    state = await _triage_orch()._pm_triage_count_state()
    assert bool(state.no_fit_clusters) == (late_change is not None)


@pytest.mark.asyncio
async def test_classifier_disposition_requires_known_dispatch_time_and_verified_source(tmp_path):
    orch = await _map_orch(tmp_path)
    del orch._pm_onboard_observe_map_diff_not_needed
    rec = orch.record("s1")
    rec.pr_resolution = "not-needed"
    from datetime import datetime, timezone
    from unittest.mock import patch
    rec.pr_resolution_source = "classifier"
    rec.created_at = None
    with patch("server.sessions.orchestrator.db_session", side_effect=AssertionError("must not query")):
        assert not await orch._pm_onboard_observe_map_diff_not_needed(rec, disposition_source="classifier")
        rec.created_at = datetime.now(timezone.utc)
        for source in ("tool", "marker"):
            rec.pr_resolution_source = source
            assert not await orch._pm_onboard_observe_map_diff_not_needed(rec, disposition_source="classifier")


@pytest.mark.asyncio
@pytest.mark.parametrize("archive_via", ["completion", "autopilot"])
async def test_failed_classifier_disposition_write_keeps_session(tmp_path, archive_via):
    orch = await _map_orch(tmp_path)
    orch._read_auto_archive_enabled = AsyncMock(return_value=True)
    rec = orch.record("s1")
    rec.pr_resolution = "not-needed"
    rec.pr_resolution_source = "classifier"
    rec.status = "complete"
    orch._pm_onboard_observe_map_diff_not_needed.side_effect = RuntimeError("DB unavailable")
    if archive_via == "completion":
        await orch._auto_archive_completed("s1")
    else:
        with pytest.raises(RuntimeError, match="DB unavailable"):
            await orch._autopilot_archive("s1")
    assert orch.record("s1") is rec
    assert not orch._drivers["s1"].closed
    orch._pm_onboard_observe_map_diff_not_needed.assert_awaited_once_with(
        rec, disposition_source="classifier")


@pytest.mark.asyncio
@pytest.mark.parametrize("opt_out", [False, True])
@pytest.mark.parametrize("indexed", [False, True])
async def test_triage_recovers_retained_done_map_diff_without_a_new_turn(tmp_path, opt_out, indexed):
    from server.db import db_session
    from server.db.models import Feature, Project, ProjectRepo, TriageProposal, WorkItem
    from server.tests.test_pm_orchestrator_seams import _clone_with_docs

    orch = await _map_orch(tmp_path)
    del orch._pm_onboard_observe_map_diff_not_needed
    root = str(tmp_path / "repos")
    _clone_with_docs(root, {"docs/product/features/renamed.md": "#100\n"})
    orch._effective_repo_root = AsyncMock(return_value=root)
    orch._sdlc_default_branch = AsyncMock(return_value="main")
    async with db_session() as db:
        project = Project(slug="viberpit", name="ViberPit")
        db.add(project)
        await db.flush()
        db.add(ProjectRepo(project_id=project.id, repo="steiner385/viberpit", is_primary=True))
        if indexed:
            db.add(Feature(project_id=project.id, slug="offline-mode", title="Offline"))
        item = WorkItem(repo="steiner385/viberpit", number=100, kind="issue", title="Offline",
                        state="open", triage_state="classified")
        db.add(item)
        await db.flush()
        db.add(TriageProposal(work_item_id=item.id, status="accepted", no_fit_suggestion="offline-mode"))
    # Rehydrated historical state: no new _persist_status(done) callback occurs.
    rec = orch.record("s1")
    rec.status = "done"
    if opt_out:
        rec.auto_archive_opt_out_reason = "manual-unarchive"
    await orch._pm_triage_count_state()
    await _drain(orch)
    assert rec.status == ("done" if opt_out else "complete")
    assert rec.pr_resolution_source == (None if opt_out else "observed")
    assert bool((await orch._pm_triage_count_state()).no_fit_clusters) == opt_out


@pytest.mark.asyncio
async def test_unmarked_map_verifier_deduplicates_overlapping_recovery(tmp_path):
    orch = await _map_orch(tmp_path)
    rec = orch.record("s1")
    rec.status = "done"
    entered, release = asyncio.Event(), asyncio.Event()
    async def observe(_rec):
        entered.set()
        await release.wait()
        return False
    orch._pm_onboard_observe_map_diff_not_needed.side_effect = observe
    first = asyncio.create_task(orch._certify_unmarked_map_diff_turn(
        rec, rec.updated_at, rec.last_user_interaction_at))
    await entered.wait()
    second = asyncio.create_task(orch._certify_unmarked_map_diff_turn(
        rec, rec.updated_at, rec.last_user_interaction_at))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)
    orch._pm_onboard_observe_map_diff_not_needed.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("hold", ["remote", "waiting_question", "waiting_permission", "parked"])
async def test_triage_recovery_preserves_remote_and_waiting_sessions(tmp_path, hold):
    from server.db import db_session
    from server.db.models import Project, ProjectRepo
    from server.pm.engine import NoFitCluster

    orch = await _map_orch(tmp_path)
    async with db_session() as db:
        project = Project(slug="viberpit", name="ViberPit")
        db.add(project)
        await db.flush()
        db.add(ProjectRepo(project_id=project.id, repo="steiner385/viberpit", is_primary=True))
    rec = orch.record("s1")
    rec.status = "done"
    if hold == "remote":
        rec.host = "other-live-host"
        orch._drivers.pop("s1")
    elif hold == "parked":
        orch._session_is_parked = AsyncMock(return_value=True)
    else:
        rec.status = hold
    assert await orch._pm_triage_open_map_diffs((
        NoFitCluster("offline-mode", (100,), "steiner385/viberpit"),)) == ("offline-mode",)
    orch._pm_onboard_observe_map_diff_not_needed.assert_not_awaited()
    assert rec.pr_resolution is None
