"""Journaled, resumable materialization apply (spec 003 §7b, #992, PR C4 Task 15).

Every GitHub write goes through a fake ``mutate`` seam keyed by document text —
nothing here may ever reach GitHub. What the tests pin is the contract the apply
path owes the journal: a row BEFORE the call, in creation order, a recorded node
id as the resume key, and no blind retry of anything already recorded.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import select

from server.db import db_session, init_db
from server.db.models import (
    GhWriteJournal,
    Initiative,
    MaterializationProposal,
    Project,
    ProjectRepo,
    WorkItem,
)

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)

PAYLOAD = {
    "epic_title": "Dark mode",
    "epic_body": "Ship dark mode.",
    "tasks": [
        {"title": "Tokens", "body": "b0", "depends_on": []},
        {"title": "Toggle", "body": "b1", "depends_on": [0]},
        {"title": "Docs", "body": "b2", "depends_on": []},
    ],
    "source_session": "sess-9",
}


@pytest.fixture
def db_env():
    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite+aiosqlite:///{tmp}/t.db"
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": url, "CCSWITCH_DEV": "1",
                                     "CCSWITCH_TAILNET_ALLOWLIST": ""}):
            yield url


async def _seed(db_url: str, *, payload: dict | None = None,
                with_product: bool = True) -> tuple[str, str]:
    """(initiative_id, proposal_id) for a plan-stage initiative awaiting the gate."""
    await init_db(db_url)
    async with db_session() as db:
        if with_product:
            p = Project(slug="ccswitch", name="ccswitch", onboarding_state="map_merged")
            db.add(p)
            await db.flush()
            db.add(ProjectRepo(project_id=p.id, repo="o/r", is_primary=True))
        ini = Initiative(slug="dark-mode", repo="o/r", title="Dark mode",
                         stage="plan", status="awaiting_human")
        db.add(ini)
        await db.flush()
        prop = MaterializationProposal(
            initiative_id=ini.id, status="pending", session_id="sess-9",
            payload=dict(payload or PAYLOAD))
        db.add(prop)
        await db.flush()
        return ini.id, prop.id


def _title_of(doc: str) -> str:
    m = re.search(r'title: ("(?:[^"\\]|\\.)*")', doc)
    assert m, doc
    return json.loads(m.group(1))


class FakeGh:
    """Canned GraphQL payloads keyed by document text; records every call.

    Modelled on ``_pm_reconcile_mutate``: it returns the payload even when some
    aliases carry errors (``partial_ok``), so the apply path has to read the
    per-alias verdicts rather than trusting a raised exception."""

    def __init__(self, *, fail_titles: tuple[str, ...] = (),
                 fail_edges: tuple[str, ...] = (),
                 fail_pairs: tuple[tuple[str, str], ...] = (),
                 fail_document: str = "",
                 repo_id: str | None = "R_repo"):
        #: A document-level GraphQL error (502, abuse limit): no `path`, so it
        #: judges no single alias — parse_batch copies it onto all of them.
        self.fail_document = fail_document
        self.fail_titles = fail_titles
        #: Aliases to fail, for a single-shot batch. ``fail_pairs`` names the edge
        #: by its (issue, blocker) node ids instead — the alias of a given edge
        #: shifts between runs once applied edges are suppressed.
        self.fail_edges = fail_edges
        self.fail_pairs = set(fail_pairs)
        self.repo_id = repo_id
        self.docs: list[str] = []
        self.owners: list[str] = []
        self._n = 0

    async def __call__(self, owner: str, doc: str) -> dict:
        self.owners.append(owner)
        self.docs.append(doc)
        if "repository(" in doc:
            if self.repo_id is None:
                return {"data": {"repository": None},
                        "errors": [{"message": "Could not resolve to a Repository"}]}
            return {"data": {"repository": {"id": self.repo_id}}}
        if "createIssue" in doc:
            title = _title_of(doc)
            if title in self.fail_titles:
                return {"data": {"createIssue": None},
                        "errors": [{"message": f"cannot create {title}"}]}
            self._n += 1
            n = 100 + self._n
            return {"data": {"createIssue": {"issue": {
                "id": f"I_{self._n}", "number": n,
                "url": f"https://github.com/o/r/issues/{n}"}}}}
        if "addBlockedBy" in doc:
            if self.fail_document:
                return {"data": None, "errors": [{"message": self.fail_document}]}
            entries = re.findall(
                r'(m\d+): addBlockedBy\(input: \{issueId: "([^"]+)", '
                r'blockingIssueId: "([^"]+)"\}', doc)
            bad = {a for a, issue, blocker in entries
                   if a in self.fail_edges or (issue, blocker) in self.fail_pairs}
            errors = [{"path": [a], "message": "already blocked"} for a in bad]
            return {"data": {a: (None if a in bad else {"clientMutationId": None})
                             for a, _i, _b in entries},
                    "errors": errors}
        raise AssertionError(f"unexpected document: {doc}")

    @property
    def creates(self) -> list[str]:
        return [_title_of(d) for d in self.docs if "createIssue" in d]

    @property
    def edge_docs(self) -> list[str]:
        return [d for d in self.docs if "addBlockedBy" in d]


async def _apply(proposal_id: str, gh: FakeGh, *, now: datetime = NOW) -> dict:
    from server.pm.materialize_apply import apply
    async with db_session() as db:
        prop = (await db.execute(select(MaterializationProposal).where(
            MaterializationProposal.id == proposal_id))).scalar_one()
        return await apply(db, prop, mutate=gh, now=now)


async def _journal() -> list[GhWriteJournal]:
    async with db_session() as db:
        return list((await db.execute(select(GhWriteJournal))).scalars().all())


def _states(rows: list[GhWriteJournal]) -> list[tuple[str, object, str]]:
    from server.pm.journal import state_of
    return [(r.field or "", (r.new_value or {}).get("value"), state_of(r)) for r in rows]


@pytest.mark.asyncio
async def test_apply_creates_epic_then_tasks_then_the_chain(db_env):
    ini_id, prop_id = await _seed(db_env)
    gh = FakeGh()
    out = await _apply(prop_id, gh)

    assert out["ok"] is True
    assert out["tasks"] == 3
    assert out["edges"] == 2
    assert out["epic"]["number"] == 101

    # One createIssue per issue, epic first, tasks in plan order, then ONE
    # batched edge document.
    assert gh.creates == ["Dark mode", "Tokens", "Toggle", "Docs"]
    assert gh.owners[0] == "o"
    assert len(gh.edge_docs) == 1
    assert "parentIssueId" not in gh.docs[1]  # the epic itself has no parent
    for doc in gh.docs[2:5]:
        assert '"I_1"' in doc and "parentIssueId" in doc

    rows = await _journal()
    assert _states(rows) == [
        ("create_issue", "Dark mode", "applied"),
        ("create_issue", "Tokens", "applied"),
        ("create_issue", "Toggle", "applied"),
        ("create_issue", "Docs", "applied"),
        ("add_blocked_by", "I_2", "applied"),   # Toggle blocked by Tokens
        ("add_blocked_by", "I_3", "applied"),   # Docs blocked by Toggle
    ]
    assert {r.batch_id for r in rows} == {f"materialize:{prop_id}"}
    assert {r.initiated_by for r in rows} == {"materialize:dark-mode"}
    assert rows[0].target_node_id == "R_repo"
    assert rows[1].target_node_id == "I_1"


@pytest.mark.asyncio
async def test_edge_rows_carry_the_reconcilers_own_key(db_env):
    """The journal key must equal the key ``compile_edges`` would compute, or the
    reconciler re-issues every edge on its next pass."""
    from server.pm.materialize import blocked_by_mutation
    _, prop_id = await _seed(db_env)
    await _apply(prop_id, FakeGh())
    rows = [r for r in await _journal() if r.field == "add_blocked_by"]
    expected = {blocked_by_mutation(b, br, initiated_by="materialize:dark-mode").key()
                for b, br in (("I_3", "I_2"), ("I_4", "I_3"))}
    assert {r.mutation_id for r in rows} == expected


@pytest.mark.asyncio
async def test_apply_overlays_the_initiative_and_the_work_items(db_env):
    ini_id, prop_id = await _seed(db_env)
    await _apply(prop_id, FakeGh())
    async with db_session() as db:
        ini = (await db.execute(select(Initiative).where(
            Initiative.id == ini_id))).scalar_one()
        assert (ini.epic_repo, ini.epic_number) == ("o/r", 101)
        assert ini.status == "active"
        items = list((await db.execute(select(WorkItem).order_by(
            WorkItem.number))).scalars().all())
        assert [w.number for w in items] == [102, 103, 104]
        assert [w.title for w in items] == ["Tokens", "Toggle", "Docs"]
        assert {w.kind for w in items} == {"issue"}
        assert {w.state for w in items} == {"open"}
        assert {w.triage_state for w in items} == {"classified"}
        assert {w.classified_by for w in items} == {"materialize"}
        assert all(w.gh_node_id and w.url and w.body for w in items)
        prop = (await db.execute(select(MaterializationProposal).where(
            MaterializationProposal.id == prop_id))).scalar_one()
        assert prop.status == "accepted"
        assert prop.resolved_at is not None
        assert prop.payload["created"]["epic"]["node_id"] == "I_1"
        assert prop.payload["created"]["tasks"]["2"]["number"] == 104
        repo_row = (await db.execute(select(ProjectRepo))).scalar_one()
        assert repo_row.sync_requested_at is not None


@pytest.mark.asyncio
async def test_apply_without_an_owning_product_still_succeeds(db_env):
    """`Initiative.project_id` is nullable and the repo may belong to no product;
    the sync request is skipped rather than the whole apply failing."""
    _, prop_id = await _seed(db_env, with_product=False)
    out = await _apply(prop_id, FakeGh())
    assert out["ok"] is True
    assert out["syncRequested"] is False


@pytest.mark.asyncio
async def test_a_failed_task_stops_the_apply_and_blocks_the_initiative(db_env):
    ini_id, prop_id = await _seed(db_env)
    gh = FakeGh(fail_titles=("Toggle",))
    out = await _apply(prop_id, gh)

    assert out["ok"] is False
    assert out["step"] == "task:1"
    assert "Toggle" in out["error"]
    assert gh.creates == ["Dark mode", "Tokens", "Toggle"]  # stops at the failure
    assert gh.edge_docs == []

    assert _states(await _journal()) == [
        ("create_issue", "Dark mode", "applied"),
        ("create_issue", "Tokens", "applied"),
        ("create_issue", "Toggle", "failed"),
    ]
    async with db_session() as db:
        ini = (await db.execute(select(Initiative).where(
            Initiative.id == ini_id))).scalar_one()
        assert ini.status == "blocked"
        assert ini.epic_repo is None
        prop = (await db.execute(select(MaterializationProposal).where(
            MaterializationProposal.id == prop_id))).scalar_one()
        assert prop.status == "pending"
        created = prop.payload["created"]
        assert created["epic"]["node_id"] == "I_1"
        assert list(created["tasks"]) == ["0"]
        assert (await db.execute(select(WorkItem))).scalars().all() == []


@pytest.mark.asyncio
async def test_a_rerun_resumes_from_the_recorded_node_ids(db_env):
    """The resume key is the recorded node id: an index already created is never
    journaled or sent again (no blind retry, no duplicate issue)."""
    _, prop_id = await _seed(db_env)
    assert (await _apply(prop_id, FakeGh(fail_titles=("Toggle",))))["ok"] is False

    gh2 = FakeGh()
    gh2._n = 2  # the fake keeps numbering where the first run left off
    out = await _apply(prop_id, gh2)
    assert out["ok"] is True
    assert gh2.creates == ["Toggle", "Docs"]  # epic + task 0 skipped entirely
    # No repository lookup either: the epic node id is already recorded.
    assert not any("repository(" in d for d in gh2.docs)

    assert _states(await _journal()) == [
        ("create_issue", "Dark mode", "applied"),
        ("create_issue", "Tokens", "applied"),
        ("create_issue", "Toggle", "failed"),
        ("create_issue", "Toggle", "applied"),
        ("create_issue", "Docs", "applied"),
        ("add_blocked_by", "I_2", "applied"),
        ("add_blocked_by", "I_3", "applied"),
    ]
    async with db_session() as db:
        assert len((await db.execute(select(WorkItem))).scalars().all()) == 3


@pytest.mark.asyncio
async def test_a_failed_edge_alias_fails_only_that_row(db_env):
    ini_id, prop_id = await _seed(db_env)
    out = await _apply(prop_id, FakeGh(fail_edges=("m1",)))
    assert out["ok"] is False
    assert out["step"] == "edges"
    assert _states(await _journal())[-2:] == [
        ("add_blocked_by", "I_2", "applied"),
        ("add_blocked_by", "I_3", "failed"),
    ]
    async with db_session() as db:
        ini = (await db.execute(select(Initiative).where(
            Initiative.id == ini_id))).scalar_one()
        assert ini.status == "blocked"
        prop = (await db.execute(select(MaterializationProposal).where(
            MaterializationProposal.id == prop_id))).scalar_one()
        assert prop.status == "pending"


@pytest.mark.asyncio
async def test_a_rerun_does_not_re_issue_an_applied_edge(db_env):
    """An edge already applied is suppressed by the journal's ``applied_keys`` —
    the same suppression the reconciler uses, so a resume cannot double-write."""
    _, prop_id = await _seed(db_env)
    assert (await _apply(prop_id, FakeGh(fail_edges=("m1",))))["ok"] is False
    gh2 = FakeGh()
    gh2._n = 4
    out = await _apply(prop_id, gh2)
    assert out["ok"] is True
    assert gh2.creates == []
    assert len(gh2.edge_docs) == 1
    assert gh2.edge_docs[0].count("addBlockedBy") == 1  # only the failed edge


@pytest.mark.asyncio
async def test_an_unresolvable_repository_fails_before_any_journal_row(db_env):
    ini_id, prop_id = await _seed(db_env)
    out = await _apply(prop_id, FakeGh(repo_id=None))
    assert out["ok"] is False
    assert out["step"] == "repository"
    assert await _journal() == []
    async with db_session() as db:
        ini = (await db.execute(select(Initiative).where(
            Initiative.id == ini_id))).scalar_one()
        assert ini.status == "blocked"


@pytest.mark.asyncio
async def test_a_transport_failure_marks_the_pending_row_failed(db_env):
    """The row is written BEFORE the call, so a raising transport still leaves a
    record of what we attempted."""
    _, prop_id = await _seed(db_env)

    class Boom(FakeGh):
        async def __call__(self, owner: str, doc: str) -> dict:
            if "createIssue" in doc:
                raise RuntimeError("gh graphql failed: 502")
            return await super().__call__(owner, doc)

    out = await _apply(prop_id, Boom())
    assert out["ok"] is False
    assert out["step"] == "epic"
    assert _states(await _journal()) == [("create_issue", "Dark mode", "failed")]


@pytest.mark.asyncio
async def test_more_than_twenty_edges_are_chunked(db_env):
    """``build_batch`` caps a document at 20 aliases; a 25-task plan must not raise."""
    payload = dict(PAYLOAD, tasks=[{"title": f"T{i}", "body": f"b{i}", "depends_on": []}
                                   for i in range(25)])
    _, prop_id = await _seed(db_env, payload=payload)
    gh = FakeGh()
    out = await _apply(prop_id, gh)
    assert out["ok"] is True
    assert out["edges"] == 24
    assert len(gh.edge_docs) == 2


@pytest.mark.asyncio
async def test_a_mirrored_work_item_is_overlaid_not_duplicated(db_env):
    """pm-sync can mirror the new issues before a resumed apply reaches the
    overlay; (repo, number, kind) is unique, so the write has to be an upsert —
    and the mirrored row still needs the classified overlay."""
    _, prop_id = await _seed(db_env)
    async with db_session() as db:
        db.add(WorkItem(repo="o/r", number=102, kind="issue", state="open",
                        title="Tokens", triage_state="unclassified"))
        await db.commit()
    assert (await _apply(prop_id, FakeGh()))["ok"] is True
    async with db_session() as db:
        items = list((await db.execute(select(WorkItem).order_by(
            WorkItem.number))).scalars().all())
    assert [w.number for w in items] == [102, 103, 104]
    assert items[0].triage_state == "classified"
    assert items[0].gh_node_id == "I_2"


@pytest.mark.asyncio
async def test_an_unresolved_create_row_stops_the_resume(db_env):
    """The crash window: a `createIssue` that succeeded but whose journal row was
    never marked leaves a pending row and no recorded id. `createIssue` is not
    idempotent, so the resume must NOT re-issue it — it stops and asks a human."""
    from server.pm.journal import state_of
    _, prop_id = await _seed(db_env)
    gh1 = FakeGh(fail_titles=("Toggle",))
    assert (await _apply(prop_id, gh1))["ok"] is False
    # Simulate the crash: the row for Toggle exists but carries no verdict.
    async with db_session() as db:
        row = next(r for r in (await db.execute(select(GhWriteJournal))).scalars().all()
                   if (r.new_value or {}).get("value") == "Toggle")
        row.error = None
        await db.commit()

    gh2 = FakeGh()
    gh2._n = 2
    out = await _apply(prop_id, gh2)
    assert out["ok"] is False
    assert out["step"] == "resume_ambiguous"
    assert "Toggle" in out["error"]
    assert gh2.creates == []  # nothing re-issued, and Docs is never reached
    rows = await _journal()
    assert [state_of(r) for r in rows] == ["applied", "applied", "pending"]
    async with db_session() as db:
        ini = (await db.execute(select(Initiative))).scalar_one()
        assert ini.status == "blocked"


@pytest.mark.asyncio
async def test_an_edge_failing_twice_is_parked_and_stops_blocking(db_env):
    """One relation GitHub keeps refusing must not strand the epic, its tasks and
    every other edge forever: park it on the second own-merits failure (the
    reconciler's rule) and let the overlay land."""
    from server.pm.journal import state_of
    ini_id, prop_id = await _seed(db_env)
    bad = (("I_4", "I_3"),)  # Docs blocked by Toggle
    assert (await _apply(prop_id, FakeGh(fail_pairs=bad)))["ok"] is False

    gh2 = FakeGh(fail_pairs=bad)
    gh2._n = 4
    out = await _apply(prop_id, gh2)
    assert out["ok"] is True
    assert out["parkedEdges"] == 1
    assert gh2.edge_docs[0].count("addBlockedBy") == 1  # only the disputed edge

    edges = [r for r in await _journal() if r.field == "add_blocked_by"]
    assert sorted(state_of(r) for r in edges) == ["applied", "failed", "parked"]
    async with db_session() as db:
        ini = (await db.execute(select(Initiative).where(
            Initiative.id == ini_id))).scalar_one()
        assert ini.status == "active"
        assert ini.epic_number == 101
        prop = (await db.execute(select(MaterializationProposal))).scalar_one()
        assert prop.status == "accepted"
        assert len((await db.execute(select(WorkItem))).scalars().all()) == 3


@pytest.mark.asyncio
async def test_a_parked_edge_is_not_re_sent(db_env):
    """A parked edge is a decided outcome — a later accept must not re-issue it."""
    _, prop_id = await _seed(db_env)
    bad = (("I_4", "I_3"),)
    assert (await _apply(prop_id, FakeGh(fail_pairs=bad)))["ok"] is False
    gh2 = FakeGh(fail_pairs=bad)
    gh2._n = 4
    assert (await _apply(prop_id, gh2))["ok"] is True
    # A third apply (the proposal is accepted, but the seam is re-entrant) sends
    # nothing at all: every edge is either applied or parked.
    gh3 = FakeGh(fail_pairs=bad)
    gh3._n = 4
    out = await _apply(prop_id, gh3)
    assert out["ok"] is True
    assert gh3.edge_docs == []
    assert out["parkedEdges"] == 1


@pytest.mark.asyncio
async def test_a_document_level_edge_failure_is_transient_not_parked(db_env):
    """A 502 / abuse limit fails every alias at once and judges none of them.
    Counting those as own-merits failures would PARK the whole chain on the second
    blip — real edges never created and suppressed on every future reconcile."""
    from server.pm.journal import TRANSIENT_PREFIX, state_of
    _, prop_id = await _seed(db_env)
    for _ in range(2):  # two consecutive blips
        out = await _apply(prop_id, FakeGh(fail_document="502 Bad Gateway"))
        assert out["ok"] is False
        assert out["step"] == "edges_transient"
        assert "502" in out["error"]

    edges = [r for r in await _journal() if r.field == "add_blocked_by"]
    assert len(edges) == 4  # both edges, twice
    assert {state_of(r) for r in edges} == {"failed"}  # nothing parked
    assert all((r.error or "").startswith(TRANSIENT_PREFIX) for r in edges)
    async with db_session() as db:
        from server.pm import journal as _j
        keys = {r.mutation_id for r in edges}
        parked = await db.run_sync(
            lambda s: {k for k in keys if _j.is_parked(s, k)})
        assert parked == set()
        # …and the blips left no failure "on the merits" to park against later.
        counts = await db.run_sync(
            lambda s: {_j.failure_count(s, k) for k in keys})
        assert counts == {0}

    gh = FakeGh()
    out = await _apply(prop_id, gh)
    assert out["ok"] is True
    assert out["parkedEdges"] == 0
    applied = [r for r in await _journal()
               if r.field == "add_blocked_by" and state_of(r) == "applied"]
    assert len(applied) == 2
