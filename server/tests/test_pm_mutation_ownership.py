"""Mutation authorization keeps verified identity evidence through the DB boundary."""
from unittest.mock import AsyncMock

import pytest

from server.db import db_session, init_db
from server.db.models import Project, ProjectRepo
from server.pm import repo_project
from server.automations import target_identity


@pytest.mark.asyncio
async def test_snapshot_refuses_changed_registration_and_failed_alias(tmp_path, monkeypatch):
    await init_db(f"sqlite+aiosqlite:///{tmp_path}/ownership.db")
    async with db_session() as db:
        product = Project(slug="one", name="One")
        db.add(product)
        await db.flush()
        db.add(ProjectRepo(project_id=product.id, repo="old/name"))
    monkeypatch.setattr(target_identity, "resolve_repositories", AsyncMock(return_value={
        "old/name": "new/name", "NEW/NAME": "new/name"}))
    snapshot = await repo_project.gather_repo_ownership_snapshot(["NEW/NAME"])
    async with db_session() as db:
        owner = await repo_project.require_mutation_owner(db, "NEW/NAME", snapshot=snapshot)
        assert owner.slug == "one"
        product = Project(slug="two", name="Two")
        db.add(product)
        await db.flush()
        db.add(ProjectRepo(project_id=product.id, repo="new/name"))
    async with db_session() as db:
        with pytest.raises(repo_project.OwnershipConflict, match="changed"):
            await repo_project.require_mutation_owner(db, "NEW/NAME", snapshot=snapshot)
    monkeypatch.setattr(target_identity, "resolve_repositories", AsyncMock(return_value={}))
    snapshot = await repo_project.gather_repo_ownership_snapshot(["NEW/NAME"])
    async with db_session() as db:
        with pytest.raises(repo_project.OwnershipConflict, match="unresolved"):
            await repo_project.require_mutation_owner(db, "NEW/NAME", snapshot=snapshot)


@pytest.mark.asyncio
@pytest.mark.parametrize("family", ["triage-human", "triage-auto", "issue", "dependency", "materialize"])
@pytest.mark.parametrize("evidence", ["ambiguous", "unresolved", "null-slug", "omitted", "changed"])
async def test_mutations_refuse_before_any_effect(tmp_path, monkeypatch, family, evidence):
    from datetime import datetime, timezone
    from server.db.models import (
        GhWriteJournal, Initiative, MaterializationProposal, TriageProposal, WorkItem,
    )
    from server.pm import dep_remove, issue_disposition, materialize_apply, triage_accept
    from sqlalchemy import select

    await init_db(f"sqlite+aiosqlite:///{tmp_path}/mutation.db")
    async with db_session() as db:
        product = Project(slug=None if evidence == "null-slug" else "one", name="One")
        db.add(product)
        await db.flush()
        db.add(ProjectRepo(project_id=product.id, repo="old/name"))
        if evidence == "ambiguous":
            second = Project(slug="two", name="Two")
            db.add(second)
            await db.flush()
            db.add(ProjectRepo(project_id=second.id, repo="NEW/NAME"))
        item = WorkItem(repo="NEW/NAME", number=1, kind="issue", state="open",
                        title="Title", body="Body", gh_node_id="I1", triage_state="proposed")
        ini = Initiative(slug="initiative", repo="NEW/NAME", title="Initiative",
                         project_id=product.id, stage="plan", status="awaiting_human")
        db.add_all([item, ini])
        await db.flush()
        proposal = TriageProposal(work_item_id=item.id, status="pending", no_fit_suggestion="new")
        materialization = MaterializationProposal(initiative_id=ini.id, status="pending", payload={})
        db.add_all([proposal, materialization])
        await db.flush()
        ids = item.id, proposal.id, materialization.id, ini.id
    mapping = {"old/name": "new/name", "NEW/NAME": "new/name"}
    if evidence == "unresolved":
        mapping["old/name"] = None
    fetch_identities = AsyncMock(return_value=mapping)
    monkeypatch.setattr(target_identity, "resolve_repositories", fetch_identities)
    snapshot = await repo_project.gather_repo_ownership_snapshot(["NEW/NAME"])
    if evidence == "changed":
        async with db_session() as db:
            row = await db.get(Project, product.id)
            row.slug = "renamed"
    if evidence == "omitted":
        snapshot = None
    mutate = AsyncMock()
    fetch = AsyncMock()
    async with db_session() as db:
        item = await db.get(WorkItem, ids[0])
        proposal = await db.get(TriageProposal, ids[1])
        if family.startswith("triage"):
            with pytest.raises(repo_project.OwnershipConflict):
                await triage_accept.apply_accept(
                    db, proposal, decided_by=family.partition("-")[2], audit_identity="test",
                    ownership=snapshot)
        elif family == "issue":
            result = await issue_disposition.apply(
                db, src=item, decision=issue_disposition.IssueDecision(
                    action="resolve", version=issue_disposition.detail(item)["version"]),
                fetch=fetch, mutate=mutate, now=datetime.now(timezone.utc), ownership=snapshot)
            assert result["step"] == "ownership"
        elif family == "dependency":
            result = await dep_remove.remove(db, src=item, blocker_ref="new/name#2", mutate=mutate,
                                             now=datetime.now(timezone.utc), ownership=snapshot)
            assert result["step"] == "ownership"
        else:
            result = await materialize_apply.apply(
                db, await db.get(MaterializationProposal, ids[2]), mutate=mutate,
                now=datetime.now(timezone.utc), ownership=snapshot)
            assert result["step"] == "ownership"
        assert item.triage_state == "proposed"
        assert item.state == "open"
        assert proposal.status == "pending" and proposal.undo_snapshot is None
        assert (await db.get(Initiative, ids[3])).status == "awaiting_human"
        assert not (await db.scalars(select(GhWriteJournal))).all()
    fetch.assert_not_awaited()
    mutate.assert_not_awaited()
    fetch_identities.assert_awaited_once()  # no network at the mutation boundary


def test_repo_project_imports_without_loading_automation_registry():
    import subprocess
    import sys

    result = subprocess.run([sys.executable, "-c", "import server.pm.repo_project"],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_unique_alias_accept_and_explicit_standalone_semantics(tmp_path, monkeypatch):
    from server.db.models import Feature, TriageProposal, WorkItem
    from server.pm.triage_accept import apply_accept

    await init_db(f"sqlite+aiosqlite:///{tmp_path}/accept.db")
    async with db_session() as db:
        product = Project(slug="one", name="One")
        db.add(product)
        await db.flush()
        db.add(ProjectRepo(project_id=product.id, repo="old/name"))
        feature = Feature(project_id=product.id, slug="feature", title="Feature")
        item = WorkItem(repo="NEW/NAME", number=1, kind="issue", triage_state="proposed")
        standalone = WorkItem(repo="stand/alone", number=2, kind="issue", triage_state="proposed")
        db.add_all([feature, item, standalone])
        await db.flush()
        proposal = TriageProposal(work_item_id=item.id, status="pending", no_fit_suggestion="feature")
        other = TriageProposal(work_item_id=standalone.id, status="pending")
        db.add_all([proposal, other])
        await db.flush()
        ids = proposal.id, other.id, item.id, standalone.id, feature.id
    monkeypatch.setattr(target_identity, "resolve_repositories", AsyncMock(return_value={
        "old/name": "new/name", "NEW/NAME": "new/name", "stand/alone": "stand/alone"}))
    snapshot = await repo_project.gather_repo_ownership_snapshot(["NEW/NAME", "stand/alone"])
    async with db_session() as db:
        proposal = await db.get(TriageProposal, ids[0])
        await apply_accept(db, proposal, decided_by="auto", audit_identity="test", ownership=snapshot)
        assert (await db.get(WorkItem, ids[2])).feature_id == ids[4]
        other = await db.get(TriageProposal, ids[1])
        with pytest.raises(repo_project.OwnershipConflict, match="missing"):
            await apply_accept(db, other, decided_by="auto", audit_identity="test", ownership=snapshot)
        await apply_accept(db, other, decided_by="human", audit_identity="test", ownership=snapshot)
        assert (await db.get(WorkItem, ids[3])).triage_state == "classified"
