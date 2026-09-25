"""Capability evidence links (#2162): feature-doc `code:`/`tests:`/`specs:`/`adrs:`
frontmatter → `feature_evidence` rows resolved against the default branch →
roadmap-health findings."""
from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from server.pm.evidence import PathResolution, matches
from server.pm.feature_index import EvidenceUpsert, plan_feature_index
from server.pm.featuredoc import FeatureEvidenceRef, parse_feature_doc, serialize_feature_doc
from server.pm.health import roadmap_health

NOW = datetime(2026, 9, 24, tzinfo=timezone.utc)
IDENTITY_HEADERS = {"Tailscale-User-Login": "testuser@example.com"}

DOC = """---
slug: task-views
title: Task views
state: live
code:  [server/services/TaskViewService.ts, ./frontend/src/features/tasks/]
tests: server/__tests__/task-views.test.ts
specs: [docs/sdlc/epic-tasks-*/plan.md]
adrs:  [/docs/adrs/ADR-024.md]
links:
  - kind: powered_by
    project: kindash
    feature: tasks
---
# Task views
"""


# --- parse / serialize -------------------------------------------------------

def test_parse_reads_top_level_evidence_keys_and_normalises_paths():
    doc = parse_feature_doc(DOC)
    assert doc.evidence == (
        FeatureEvidenceRef("code", "server/services/TaskViewService.ts"),
        FeatureEvidenceRef("code", "frontend/src/features/tasks/"),
        FeatureEvidenceRef("tests", "server/__tests__/task-views.test.ts"),  # lone string
        FeatureEvidenceRef("specs", "docs/sdlc/epic-tasks-*/plan.md"),
        FeatureEvidenceRef("adrs", "docs/adrs/ADR-024.md"),
    )
    assert [link.feature for link in doc.links] == ["tasks"]   # links keep their meaning


def test_parse_harvests_evidence_from_an_archaeology_links_dict():
    """The dict shape used to be skipped whole; its path lists are now indexed,
    while `prs` (no repo path) and the feature-link list stay empty."""
    doc = parse_feature_doc(
        "---\nslug: a\ntitle: A\nlinks:\n  code: [server/a.py, server/a.py]\n"
        "  specs: [specs/001/spec.md]\n  prs: ['#1']\n---\n")
    assert doc.links == ()
    assert doc.evidence == (FeatureEvidenceRef("code", "server/a.py"),
                            FeatureEvidenceRef("specs", "specs/001/spec.md"))


def test_parse_skips_malformed_evidence_items_without_dropping_the_doc():
    doc = parse_feature_doc("---\nslug: a\ntitle: A\ncode: [{x: 1}, '', null, ok.py]\ntests: {a: b}\n---\n")
    assert doc.evidence == (FeatureEvidenceRef("code", "ok.py"),)


def test_serialize_round_trips_evidence():
    doc = parse_feature_doc(DOC)
    again = parse_feature_doc(serialize_feature_doc(doc))
    assert again.evidence == doc.evidence
    assert again.links == doc.links


# --- path matching -----------------------------------------------------------

TREE = ["server/a.py", "frontend/src/features/tasks/List.tsx",
        "docs/sdlc/epic-tasks-1/plan.md", "docs/sdlc/other/plan.md"]


@pytest.mark.parametrize("pattern,expected", [
    ("server/a.py", ["server/a.py"]),
    ("server/b.py", []),
    ("frontend/src/features/tasks/", ["frontend/src/features/tasks/List.tsx"]),
    ("frontend/src/features/tasks", ["frontend/src/features/tasks/List.tsx"]),
    ("frontend/src/feat", []),                       # a prefix is not a directory
    ("docs/sdlc/epic-tasks-*/plan.md", ["docs/sdlc/epic-tasks-1/plan.md"]),
    ("docs/sdlc/epic-gone-*/plan.md", []),
])
def test_matches(pattern, expected):
    assert matches(pattern, TREE) == expected


# --- planner -----------------------------------------------------------------

def test_plan_stamps_resolution_and_leaves_unresolved_paths_unknown():
    doc = parse_feature_doc(DOC)
    when = NOW - timedelta(days=2)
    res = {"server/services/TaskViewService.ts": PathResolution(False),
           "docs/sdlc/epic-tasks-*/plan.md": PathResolution(True, when),
           "docs/product/features/task-views.md": PathResolution(True, NOW)}
    plan = plan_feature_index([], [("docs/product/features/task-views.md", doc)], resolution=res)
    up = plan.upserts[0]
    by_path = {e.path: e for e in up.evidence}
    assert by_path["server/services/TaskViewService.ts"].missing is True
    assert by_path["docs/sdlc/epic-tasks-*/plan.md"] == EvidenceUpsert(
        "specs", "docs/sdlc/epic-tasks-*/plan.md", missing=False, last_commit_at=when)
    assert by_path["docs/adrs/ADR-024.md"].missing is None       # not resolved ≠ present
    assert up.doc_committed_at == NOW


# --- health ------------------------------------------------------------------

def test_health_flags_dangling_untested_unspecced_and_state_behind_spec():
    caps = [("live-bare", None, "live", NOW), ("deliv-ok", None, "delivering", NOW),
            ("proposed-code", None, "proposed", NOW), ("sunset-code", None, "sunset", NOW),
            ("behind", None, "proposed", NOW)]
    old, new = NOW - timedelta(days=10), NOW - timedelta(days=1)
    evidence = [
        ("deliv-ok", "tests", "t.py", False, None),
        ("deliv-ok", "code", "gone.py", True, None),
        ("deliv-ok", "specs", "s.md", False, old),
        ("proposed-code", "code", "c.py", False, None),
        ("sunset-code", "code", "c.py", None, None),
        ("behind", "specs", "s1.md", False, old),
        ("behind", "specs", "s2.md", False, new),
        ("orphaned", "code", "x.py", True, None),       # not a live capability row
    ]
    h = roadmap_health(capabilities=caps, initiatives=[], proposals=[], now=NOW,
                       evidence=evidence,
                       doc_committed={"deliv-ok": NOW, "behind": old + timedelta(days=1)})
    assert h.dangling_evidence == (("deliv-ok", "code", "gone.py"),)
    assert h.untested_capabilities == ("live-bare",)
    assert h.code_without_specs == ("proposed-code",)     # sunset is exempt
    assert h.state_behind_spec == ("behind",)             # newest spec (s2) wins


def test_health_without_evidence_input_keeps_old_findings_empty():
    h = roadmap_health(capabilities=[], initiatives=[], proposals=[], now=NOW)
    assert (h.dangling_evidence, h.untested_capabilities,
            h.code_without_specs, h.state_behind_spec) == ((), (), (), ())


# --- pm-sync loop ------------------------------------------------------------

def _loop(resolve):
    from server.automations.pm_sync import PmSyncLoop
    from server.pm.feature_index import FeatureDocsRead

    applied = []

    class Ctx:
        async def emit(self, *a, **kw):
            pass

    async def read_docs(repo, ref=None):
        return FeatureDocsRead(sha="sha1", docs=[("docs/product/features/task-views.md", DOC)])

    async def list_rows(_pid):
        return []

    async def apply_plan(_pid, plan):
        applied.append(plan)

    async def noop(*a, **kw):
        return None

    loop = PmSyncLoop(Ctx(), read_config=noop, list_repos=noop, fetch=noop,
                      upsert_items=noop, save_sync_state=noop,
                      read_feature_docs=read_docs, list_feature_rows=list_rows,
                      apply_feature_plan=apply_plan, now_utc=lambda: NOW,
                      resolve_evidence_paths=resolve)
    return loop, applied


def _view():
    from server.automations.pm_sync import RepoSyncView
    return RepoSyncView(project_id="p1", project_slug="proj", repo="o/r", is_primary=True,
                        backfill_done=True, backfill_phase=None, backfill_cursor=None,
                        watermark=None, last_synced_at=None, sync_requested_at=None)


@pytest.mark.asyncio
async def test_loop_resolves_every_cited_path_and_times_only_specs_and_docs():
    calls = []

    async def resolve(repo, sha, paths, timed):
        calls.append((repo, sha, paths, timed))
        return {p: PathResolution(True) for p in paths}

    loop, applied = _loop(resolve)
    await loop._feature_index(_view())
    (repo, sha, paths, timed), = calls
    assert (repo, sha) == ("o/r", "sha1")
    assert set(paths) == {"docs/product/features/task-views.md",
                          "server/services/TaskViewService.ts", "frontend/src/features/tasks/",
                          "server/__tests__/task-views.test.ts", "docs/sdlc/epic-tasks-*/plan.md",
                          "docs/adrs/ADR-024.md"}
    assert timed == {"docs/product/features/task-views.md", "docs/sdlc/epic-tasks-*/plan.md"}
    assert all(e.missing is False for e in applied[0].upserts[0].evidence)


@pytest.mark.asyncio
async def test_loop_resolver_failure_still_applies_the_index_unresolved():
    async def resolve(*_a):
        raise RuntimeError("ls-tree failed")

    loop, applied = _loop(resolve)
    await loop._feature_index(_view())
    assert applied and all(e.missing is None for e in applied[0].upserts[0].evidence)


# --- apply (DB) --------------------------------------------------------------

@contextmanager
def _db():
    from server.db import init_db
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/t.db"
        with patch.dict(os.environ, {"VIBERPIT_DB_URL": db_url, "VIBERPIT_DEV": "1",
                                     "VIBERPIT_TAILNET_ALLOWLIST": ""}):
            asyncio.run(init_db(db_url))
            yield db_url


def test_apply_rewrites_evidence_and_keeps_last_resolution_when_unresolved():
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import Feature, FeatureEvidence, Project
    from server.pm.feature_index import FeatureIndexPlan, FeatureUpsert
    from server.pm.feature_sync_seams import apply_feature_plan

    when = NOW - timedelta(days=3)

    def _plan(evidence, doc_at):
        return FeatureIndexPlan(upserts=(FeatureUpsert(
            slug="a", title="A", state="live", summary="", repo_path="d/a.md",
            parent_slug=None, links=(), evidence=tuple(evidence), doc_committed_at=doc_at),),
            orphan_slugs=(), revive_slugs=(), warnings=())

    async def _run():
        async with db_session() as db:
            p = Project(slug="prod", name="Prod")
            db.add(p)
            await db.flush()
            pid = p.id
            await db.commit()
        await apply_feature_plan(pid, _plan(
            [EvidenceUpsert("code", "x.py", missing=True),
             EvidenceUpsert("specs", "s.md", missing=False, last_commit_at=when),
             EvidenceUpsert("tests", "old.py", missing=False)], when))
        # Edge resync: nothing resolved, and `tests: old.py` left the doc.
        await apply_feature_plan(pid, _plan(
            [EvidenceUpsert("code", "x.py"), EvidenceUpsert("specs", "s.md"),
             EvidenceUpsert("adrs", "new.md")], None))
        async with db_session() as db:
            feat = (await db.execute(select(Feature))).scalar_one()
            rows = {(e.kind, e.path): e for e in (await db.execute(
                select(FeatureEvidence))).scalars().all()}
            return feat, rows

    with _db():
        feat, rows = asyncio.run(_run())
    assert set(rows) == {("code", "x.py"), ("specs", "s.md"), ("adrs", "new.md")}
    assert rows[("code", "x.py")].missing is True                   # carried over
    assert rows[("specs", "s.md")].last_commit_at.replace(tzinfo=timezone.utc) == when
    assert rows[("adrs", "new.md")].missing is None
    assert feat.doc_committed_at.replace(tzinfo=timezone.utc) == when   # not blanked


def test_health_endpoint_reports_evidence_findings():
    from server.db import db_session
    from server.db.models import Feature, FeatureEvidence, Project

    async def _seed():
        async with db_session() as db:
            p = Project(slug="kindash", name="KinDash", onboarding_state="live")
            db.add(p)
            await db.flush()
            f = Feature(project_id=p.id, slug="tasks", title="Tasks", state="live",
                        doc_committed_at=NOW - timedelta(days=5))
            db.add(f)
            await db.flush()
            db.add_all([
                FeatureEvidence(feature_id=f.id, kind="code", path="gone.py", missing=True),
                FeatureEvidence(feature_id=f.id, kind="specs", path="s.md", missing=False,
                                last_commit_at=NOW - timedelta(days=1)),
            ])

    with _db():
        asyncio.run(_seed())
        from fastapi.testclient import TestClient

        from server.app import create_app
        from server.sessions.orchestrator import SessionOrchestrator
        from server.tests.fakes.fake_factory import fake_sdk_factory
        app = create_app(orchestrator=SessionOrchestrator(sdk_factory=fake_sdk_factory()))
        with TestClient(app, raise_server_exceptions=False) as c:
            h = c.get("/api/pm/projects/kindash/health", headers=IDENTITY_HEADERS).json()
    assert h["danglingEvidence"] == [{"capability": "tasks", "kind": "code", "path": "gone.py"}]
    assert h["untestedCapabilities"] == ["tasks"]
    assert h["codeWithoutSpecs"] == []
    assert h["stateBehindSpec"] == ["tasks"]


def test_health_endpoint_reports_artifact_frontmatter_findings():
    """Issue #2161: a committed initiative artifact's frontmatter — read by
    pm-sync and stamped onto InitiativeArtifact — surfaces through the same
    health endpoint as a closed-epic-not-done initiative, an unknown-capability
    reference, and a missing-frontmatter flag."""
    from server.db import db_session
    from server.db.models import Feature, Initiative, InitiativeArtifact, Project, WorkItem

    async def _seed():
        async with db_session() as db:
            p = Project(slug="kindash", name="KinDash", onboarding_state="live")
            db.add(p)
            await db.flush()
            db.add(Feature(project_id=p.id, slug="tasks", title="Tasks", state="live"))
            ini_a = Initiative(project_id=p.id, repo="o/r", slug="init-closed-epic", status="active")
            ini_b = Initiative(project_id=p.id, repo="o/r", slug="init-no-frontmatter", status="active")
            db.add_all([ini_a, ini_b])
            await db.flush()
            db.add_all([
                InitiativeArtifact(initiative_id=ini_a.id, kind="spec",
                                   repo_path="docs/sdlc/init-closed-epic/spec.md", commit_sha="sha1",
                                   frontmatter_present=True, frontmatter_epic="o/r#9",
                                   frontmatter_capabilities=["ghost-capability"]),
                InitiativeArtifact(initiative_id=ini_b.id, kind="intent",
                                   repo_path="docs/sdlc/init-no-frontmatter/intent.md", commit_sha="sha2",
                                   frontmatter_present=False),
                WorkItem(repo="o/r", number=9, kind="issue", state="closed"),
            ])

    with _db():
        asyncio.run(_seed())
        from fastapi.testclient import TestClient

        from server.app import create_app
        from server.sessions.orchestrator import SessionOrchestrator
        from server.tests.fakes.fake_factory import fake_sdk_factory
        app = create_app(orchestrator=SessionOrchestrator(sdk_factory=fake_sdk_factory()))
        with TestClient(app, raise_server_exceptions=False) as c:
            h = c.get("/api/pm/projects/kindash/health", headers=IDENTITY_HEADERS).json()
    assert h["artifactsEpicClosedNotDone"] == ["init-closed-epic"]
    assert h["artifactsUnknownCapability"] == [
        {"initiative": "init-closed-epic", "capability": "ghost-capability"}]
    assert h["artifactsMissingFrontmatter"] == ["init-no-frontmatter"]


# --- daemon resolver (real git) ---------------------------------------------

def test_daemon_resolver_reads_the_tree_and_last_commit_times(tmp_path):
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    def git(*args, env=None):
        return subprocess.run(["git", "-C", str(tmp_path), *args], check=True,
                              capture_output=True, text=True,
                              env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                                   **(env or {})}).stdout.strip()

    git("init", "-q")
    (tmp_path / "specs").mkdir()
    (tmp_path / "specs" / "a.md").write_text("a")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "x.py").write_text("x")
    git("add", "-A")
    git("commit", "-qm", "one", env={"GIT_COMMITTER_DATE": "@1700000000 +0000"})
    sha = git("rev-parse", "HEAD")

    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())

    async def _root():
        return str(tmp_path.parent)

    with patch.object(orch, "_effective_repo_root", _root), \
            patch("server.sessions.orchestrator.gitrepos.ensure_clone",
                  lambda *_a: str(tmp_path)):
        res = asyncio.run(orch._pm_sync_resolve_evidence_paths(
            "o/r", sha, ["specs/a.md", "src/", "gone.py", "spec*/a.md"], {"specs/a.md", "gone.py"}))
        # A second call with the same inputs is served from the cache.
        with patch("server.sessions.orchestrator.gitrepos.ensure_clone",
                   side_effect=AssertionError("recomputed")):
            again = asyncio.run(orch._pm_sync_resolve_evidence_paths(
                "o/r", sha, ["specs/a.md", "src/", "gone.py", "spec*/a.md"],
                {"specs/a.md", "gone.py"}))
    assert res == again
    assert res["specs/a.md"] == PathResolution(
        True, datetime.fromtimestamp(1700000000, tz=timezone.utc))
    assert res["src/"] == PathResolution(True, None)          # untimed
    assert res["gone.py"] == PathResolution(False, None)
    assert res["spec*/a.md"].exists is True


# --- prompts -----------------------------------------------------------------

def test_every_evidence_kind_is_in_the_shared_prompt_help():
    from server.db.models import FEATURE_EVIDENCE_KINDS
    from server.pm.classification import EVIDENCE_FRONTMATTER_HELP
    for kind in FEATURE_EVIDENCE_KINDS:
        assert f"  {kind}:" in EVIDENCE_FRONTMATTER_HELP


@pytest.mark.parametrize("scope", ["initial", "diff"])
def test_map_prompts_ask_for_evidence_keys_not_prose_paths(scope):
    from server.pm.classification import EVIDENCE_FRONTMATTER_HELP, build_map_synthesis_prompt
    prompt = build_map_synthesis_prompt(
        slug="p", repo="o/r", default_branch="main", open_count=1, closed_count=1,
        scope=scope, suggestion="task-views", item_numbers=(1,))
    assert EVIDENCE_FRONTMATTER_HELP in prompt
    assert "links/code/tests/specs/adrs" in prompt
