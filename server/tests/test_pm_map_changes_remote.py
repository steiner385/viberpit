"""``RemoteOrchestrator.pm_map_changes()`` must materialize capability docs
into the ``Feature`` table, not just hand back a service.

The edge process (``RemoteOrchestrator``) has NO local git clone, so it cannot
use ``SessionOrchestrator._pm_sync_read_feature_docs``'s git plumbing. Until
this was wired, ``pm_map_changes()`` passed only ``gh_factory`` — leaving all
three of ``_resync_now``'s seams ``None``, which makes ``_resync_now`` a silent
no-op: retire/merge/triage-inline-create committed a doc to GitHub and then the
API answered from a ``Feature`` table that still showed the old state, until the
next pm-sync tick (up to 300 s later) on the *other* process.

These tests therefore assert REAL POST-CALL DB STATE, never merely that a seam
is non-None: a seam that is wired but broken reads identically to one that is
not.
"""
from __future__ import annotations

import base64
import json

import pytest

from server.tests.test_pm_map_changes_gh import FakeGh

REPO = "o/r"
SLUG = "example-product"
DOCS = "docs/product/features"

ROOT_DOC = "---\nslug: sessions\ntitle: Sessions\nstate: live\n---\n# Sessions\n"
# The doc the edge is expected to discover: `dictation` is NOT seeded as a
# Feature row, so its appearance after the resync can only come from this read.
CHILD_DOC = ("---\nslug: dictation\ntitle: Dictation\nstate: sunset\nparent: sessions\n"
             "---\n# Dictation\n")


def _contents_list(*names: str):
    """The contents API's answer for a directory listing — stubs, no content."""
    return (json.dumps([
        {"path": f"{DOCS}/{n}", "name": n, "type": "file"} for n in names]), 0, "")


def _contents_file(text: str):
    """The contents API's answer for one file."""
    return (json.dumps({
        "content": base64.b64encode(text.encode()).decode(), "sha": "blob"}), 0, "")


def _ref(sha: str):
    return (json.dumps({"object": {"sha": sha}}), 0, "")


async def _seed(url: str) -> tuple[str, str]:
    """One product with a primary repo and ONE indexed capability. Returns
    ``(project_id, sessions_feature_id)``."""
    from server.db import db_session
    from server.db.models import Feature, Project, ProjectRepo

    async with db_session() as db:
        p = Project(slug=SLUG, name="Example product", onboarding_state="live")
        db.add(p)
        await db.flush()
        db.add(ProjectRepo(project_id=p.id, repo=REPO, is_primary=True))
        root = Feature(project_id=p.id, slug="sessions", title="Sessions", state="live")
        db.add(root)
        await db.flush()
        await db.commit()
        return p.id, root.id


def _stub_default_branch(monkeypatch):
    """Pin the repo's default branch without shelling out to `git ls-remote`.

    Patched at `server.pm.map_changes.resolve_default_branch` — the one
    definition `read_feature_docs_via_gh_api` reads — rather than at the git
    helper underneath it, so the test says nothing about which plumbing that
    resolution happens to use today."""
    async def _master(repo: str) -> str:
        return "master"
    monkeypatch.setattr("server.pm.map_changes.resolve_default_branch", _master)


def _remote(gh):
    """A ``RemoteOrchestrator`` whose three GitHub seams are the fake — the
    daemon client is never touched by the map-changes path (it is edge-local),
    so a bare object stands in for it."""
    from server.sessions.remote import RemoteOrchestrator

    orch = RemoteOrchestrator(object())
    orch._gh_run = gh
    orch._gh_token_provider = lambda repo: None
    orch._gh_env = {}
    return orch


@pytest.mark.asyncio
async def test_remote_resync_materializes_feature_rows_from_the_github_api(
    isolated_db, monkeypatch,
):
    """The whole point of Fix 2: driving ``_resync_now`` on the EDGE
    orchestrator upserts a Feature row that only the doc read could know about,
    and updates an existing one's state — proving all three seams are wired and
    that the docs really were read over the GitHub API."""
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import Feature, Project

    project_id, _root_id = await _seed(isolated_db)

    gh = FakeGh([
        _contents_list("sessions.md", "dictation.md"),   # list_dir
        _contents_file(ROOT_DOC),                        # read_file sessions.md
        _contents_file(CHILD_DOC),                       # read_file dictation.md
        _ref("head-sha"),                                # default_branch_head
    ])
    _stub_default_branch(monkeypatch)

    orch = _remote(gh)
    svc = orch.pm_map_changes()
    async with db_session() as db:
        project = (await db.execute(select(Project).where(
            Project.slug == SLUG))).scalars().one()
        await svc._resync_now(db, project)

    # --- real DB read, after the fact -------------------------------------
    async with db_session() as db:
        rows = {f.slug: f for f in (await db.execute(select(Feature).where(
            Feature.project_id == project_id))).scalars().all()}
    assert set(rows) == {"sessions", "dictation"}
    # `dictation` existed ONLY in the doc read — its row is proof the edge read
    # the docs and applied the plan.
    child = rows["dictation"]
    assert child.title == "Dictation"
    assert child.state == "sunset"
    assert child.parent_feature_id == rows["sessions"].id
    assert child.orphaned is not True
    # Both rows are stamped with the SHA the API read resolved, not a guess.
    assert rows["sessions"].commit_sha == "head-sha"
    assert child.commit_sha == "head-sha"

    # And the calls really were GitHub API reads on the resolved default branch.
    argv = [call[0] for call in gh.calls]
    assert argv[0] == ["api", f"repos/{REPO}/contents/{DOCS}?ref=master"]
    assert argv[1] == ["api", f"repos/{REPO}/contents/{DOCS}/sessions.md?ref=master"]
    assert argv[3] == ["api", f"repos/{REPO}/git/ref/heads/master"]


@pytest.mark.asyncio
async def test_remote_resync_orphans_a_row_whose_doc_is_gone(isolated_db, monkeypatch):
    """The other half of "the seams are really wired": a Feature row with no
    doc behind it is orphaned by the apply, exactly as on the daemon side."""
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import Feature, Project

    project_id, _root_id = await _seed(isolated_db)
    async with db_session() as db:
        db.add(Feature(project_id=project_id, slug="ghost", title="Ghost", state="live"))
        await db.commit()

    gh = FakeGh([
        _contents_list("sessions.md"),
        _contents_file(ROOT_DOC),
        _ref("head-sha"),
    ])
    _stub_default_branch(monkeypatch)

    orch = _remote(gh)
    async with db_session() as db:
        project = (await db.execute(select(Project).where(
            Project.slug == SLUG))).scalars().one()
        await orch.pm_map_changes()._resync_now(db, project)

    async with db_session() as db:
        ghost = (await db.execute(select(Feature).where(
            Feature.project_id == project_id, Feature.slug == "ghost"))).scalars().one()
    assert ghost.orphaned is True


@pytest.mark.asyncio
async def test_remote_pm_map_changes_wires_all_three_resync_seams():
    """A structural guard on top of the behavioural tests above: if a future
    refactor drops a seam, ``_resync_now`` degrades to a SILENT no-op (its own
    ``None`` guard), so name the omission here rather than letting a behaviour
    test fail obscurely."""
    svc = _remote(FakeGh([])).pm_map_changes()
    assert svc._read_feature_docs is not None
    assert svc._list_feature_rows is not None
    assert svc._apply_feature_plan is not None
