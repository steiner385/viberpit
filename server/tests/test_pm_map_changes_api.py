"""POST/DELETE /api/pm/projects/{slug}/map-changes and POST …/rebase — the
map-changes service behind the API (roadmap drill-down spec §8/§9, row 8b).

Every GitHub call goes through the orchestrator's injectable ``_gh_run`` seam,
so these tests assert the exact ``gh`` argv sequence one edit produces (the
branch-doc probe, the branch-from-default-head, the content read, the atomic
GraphQL commit, the PR) without touching the network. ``_sdlc_default_branch``
resolves through ``gitrepos.list_remote_branches``, which does NOT use that
seam — it is patched per client.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch

from server.tests.test_pm_map_changes_gh import FakeGh

IDENTITY_HEADERS = {"Tailscale-User-Login": "testuser@example.com"}

REPO = "o/r"
SLUG = "example-product"
BRANCH = "pm/map-changes-example-product"
DOCS = "docs/product/features"
# The slug of a product whose name never survived slugification — underscores
# are not branch-safe, so every map-changes edit on it is refused.
UNSAFE_SLUG = "bad_slug"
# A product row with no ``name`` at all — the PR title has to fall back to the
# slug rather than titling the PR "… : None".
NAMELESS_SLUG = "nameless-product"
NAMELESS_BRANCH = f"pm/map-changes-{NAMELESS_SLUG}"


# -- fake `gh` answers --------------------------------------------------------

_404 = ("", 1, "HTTP 404: Not Found")
_OK = ("", 0, "")
_EMPTY_LIST = ("[]", 0, "")
_NO_PR = _EMPTY_LIST
_STALE = (json.dumps(
    {"errors": [{"message": 'Expected branch to point to "old" but it did not'}]}), 0, "")


def _ref(sha: str):
    return (json.dumps({"object": {"sha": sha}}), 0, "")


def _created(sha: str):
    return (json.dumps({"ref": f"refs/heads/{BRANCH}", "object": {"sha": sha}}), 0, "")


def _commit(oid: str):
    return (json.dumps({"data": {"createCommitOnBranch": {"commit": {"oid": oid}}}}), 0, "")


def _pr_json(*, number: int = 12, mergeable_state: str = "clean") -> dict:
    return {
        "number": number, "html_url": f"https://github.com/{REPO}/pull/{number}",
        "state": "open", "merged": False, "merged_at": None,
        "head": {"sha": "c0"}, "mergeable_state": mergeable_state}


def _pr(*, number: int = 12, mergeable_state: str = "clean"):
    return (json.dumps(_pr_json(number=number, mergeable_state=mergeable_state)), 0, "")


def _pr_closed(*, merged: bool, number: int = 12):
    """What ``GET pulls/{n}`` answers once the PR is closed — merged or not."""
    d = _pr_json(number=number)
    d.update(state="closed", merged=merged,
             merged_at="2026-09-11T10:00:00Z" if merged else None,
             mergeable_state=None)
    return (json.dumps(d), 0, "")


def _pr_list(*, number: int = 12):
    """What ``find_open_pr`` sees: the LIST endpoint, which carries neither
    ``mergeable_state`` nor ``merged`` — GitHub computes those only for a
    single-PR read. Modelling them here would hide the bug the adoption path's
    ``read_pr`` fixes."""
    d = _pr_json(number=number)
    d.pop("mergeable_state", None)
    d.pop("merged", None)
    return (json.dumps([d]), 0, "")


def _contents(text: str):
    return (json.dumps({"content": base64.b64encode(text.encode()).decode(),
                        "sha": "blob1"}), 0, "")


def _listing(*names: str):
    return (json.dumps([{"name": n, "path": f"{DOCS}/{n}", "type": "file"} for n in names]), 0, "")


def _doc(slug: str, title: str, *, parent: str | None = None, body: str | None = None) -> str:
    front = f"slug: {slug}\ntitle: {title}\nstate: proposed"
    if parent:
        front += f"\nparent: {parent}"
    return f"---\n{front}\n---\n{body if body is not None else f'# {title}'}\n"


# The scripted answers one first edit produces: the branch-doc probe, the base
# head, the branch create, the (absent) doc, the commit, the PR lookup, the PR.
def _first_create_answers() -> list:
    return [_404, _ref("base1"), _404, _created("base1"), _404, _commit("c0"), _NO_PR, _pr()]


# -- fixtures -----------------------------------------------------------------


def _seed(db_url: str) -> None:
    """A product with a primary repo and two indexed capabilities (``sessions``
    with the child ``dictation``), a second product with no repo at all, a third
    whose slug is not branch-safe, and a fourth with a repo but no name."""
    from server.db import db_session, init_db
    from server.db.models import Feature, Project, ProjectRepo

    async def _go():
        await init_db(db_url)
        async with db_session() as db:
            p = Project(slug=SLUG, name="Example product")
            db.add(p)
            await db.flush()
            db.add(ProjectRepo(project_id=p.id, repo=REPO, is_primary=True))
            root = Feature(project_id=p.id, slug="sessions", title="Sessions", state="live")
            db.add(root)
            await db.flush()
            db.add(Feature(project_id=p.id, slug="dictation", title="Dictation",
                           state="delivering", parent_feature_id=root.id))
            db.add(Project(slug="bare-product", name="Bare product"))
            db.add(Project(slug=UNSAFE_SLUG, name="Bad slug product"))
            nameless = Project(slug=NAMELESS_SLUG)
            db.add(nameless)
            await db.flush()
            db.add(ProjectRepo(project_id=nameless.id, repo=REPO, is_primary=True))
            await db.commit()

    asyncio.run(_go())


@contextmanager
def _client(db_url: str, gh):
    """``test_pm_map_api._client`` with the orchestrator's ``gh`` runner swapped
    for *gh* before ``create_app``, and the default-branch probe stubbed.

    All THREE GitHub seams are swapped, not just the runner: ``GhWrite`` binds
    ``app_token=gitrepos.app_token_for_repo`` and ``env=os.environ`` as DEFAULT
    ARGUMENTS, so patching the module afterwards cannot reach them — and
    ``create_app``'s lifespan registers the real provider. On a host that has
    ``CCSWITCH_GITHUB_APP_ID`` + a private key (this repo's own runner does),
    every edit here would otherwise read the App's private key, sign a JWT and
    call api.github.com, and capture the host's real project PAT into
    ``GhWrite._pat``."""
    from ccswitch import gitrepos
    from fastapi.testclient import TestClient
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    _clear = [
        "CCSWITCH_SESSIOND_ENDPOINT", "CCSWITCH_SESSIOND_SOCKET",
        "CCSWITCH_SESSIOND_HOST", "CCSWITCH_SESSIOND_PORT",
        "CCSWITCH_SESSIOND_CLIENT_CERT", "CCSWITCH_SESSIOND_CLIENT_KEY",
        "CCSWITCH_SESSIOND_CA", "CCSWITCH_DAEMON_ROLE",
        "CCSWITCH_GH_PROJECT_TOKEN", "VIBERPIT_GH_PROJECT_TOKEN",
    ]
    with contextlib.ExitStack() as stack:
        saved = {k: os.environ.pop(k, None) for k in _clear}
        try:
            stack.enter_context(patch.dict(os.environ, {
                "CCSWITCH_DB_URL": db_url, "CCSWITCH_DEV": "1",
                "CCSWITCH_TAILNET_ALLOWLIST": ""}))
            stack.enter_context(patch(
                "ccswitch.gitrepos.list_remote_branches",
                return_value={"default": "master", "branches": ["master"]}))
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            orch._gh_run = gh  # type: ignore[assignment]
            orch._gh_token_provider = lambda repo: None  # type: ignore[assignment]
            orch._gh_env = {}
            app = create_app(orchestrator=orch)
            with TestClient(app, raise_server_exceptions=False) as c:
                # Belt and braces: the lifespan has just registered the real
                # owner-token provider on the process-global `gitrepos` seam.
                gitrepos.set_token_provider(None)
                yield c
        finally:
            gitrepos.set_token_provider(None)
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v


def _argv(gh) -> list[list[str]]:
    return [call[0] for call in gh.calls]


def _audits(c, type_: str) -> list[dict]:
    """Audit details of *type_*, oldest first (no GET /api/audit route exists)."""
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import AuditEvent

    async def _read():
        async with db_session() as db:
            return [dict(r.detail or {}) for r in (await db.execute(
                select(AuditEvent).where(AuditEvent.type == type_).order_by(
                    AuditEvent.at))).scalars().all()]

    return c.portal.call(_read)


def _pointers(c) -> list[tuple]:
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import FeatureMapChange

    async def _read():
        async with db_session() as db:
            return [(r.repo, r.branch, r.pr_number, r.status, r.head_sha, r.behind)
                    for r in (await db.execute(select(FeatureMapChange))).scalars().all()]

    return c.portal.call(_read)


def _sync_requested(c) -> bool:
    """Whether every repo of the EDITED product is stamped sync-requested (other
    products' repos are none of this edit's business)."""
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import Project, ProjectRepo

    async def _read():
        async with db_session() as db:
            p = (await db.execute(select(Project).where(
                Project.slug == SLUG))).scalars().one()
            rows = (await db.execute(select(ProjectRepo).where(
                ProjectRepo.project_id == p.id))).scalars().all()
            return all(r.sync_requested_at is not None for r in rows) and bool(rows)

    return c.portal.call(_read)


def _set_behind(c, value: bool) -> None:
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import FeatureMapChange

    async def _write():
        async with db_session() as db:
            row = (await db.execute(select(FeatureMapChange))).scalars().first()
            row.behind = value
            await db.commit()

    c.portal.call(_write)


def _pointer_row(c) -> dict:
    """The single pointer row's lifecycle columns."""
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import FeatureMapChange

    async def _read():
        async with db_session() as db:
            r = (await db.execute(select(FeatureMapChange))).scalars().one()
            return {"status": r.status, "merged_at": r.merged_at,
                    "closed_at": r.closed_at, "behind": r.behind,
                    "conflicting": r.conflicting, "last_error": r.last_error,
                    "head_sha": r.head_sha}

    return c.portal.call(_read)


def _features(c) -> dict[str, tuple]:
    """``slug -> (orphaned, pr_number, commit_sha)`` for the product."""
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import Feature

    async def _read():
        async with db_session() as db:
            return {f.slug: (bool(f.orphaned), f.pr_number, f.commit_sha)
                    for f in (await db.execute(select(Feature))).scalars().all()}

    return c.portal.call(_read)


def _seed_proposed_capabilities(c) -> None:
    """Two capabilities indexed from the branch head (pr_number set, no
    commit_sha): ``payments`` referenced by nothing, ``billing`` referenced by a
    classified work item. Plus a merged capability (``sessions``) that abandon
    must never touch, a merged CHILD of ``payments`` (whose
    ``parent_feature_id`` must be nulled, not left dangling) and a triage
    proposal pointing at ``payments`` (same for ``proposed_feature_id``)."""
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import (
        Feature, FeatureLink, Project, TriageProposal, WorkItem)

    async def _write():
        async with db_session() as db:
            p = (await db.execute(select(Project).where(
                Project.slug == SLUG))).scalars().one()
            sessions = (await db.execute(select(Feature).where(
                Feature.slug == "sessions"))).scalars().one()
            sessions.commit_sha = "merged-sha"
            payments = Feature(project_id=p.id, slug="payments", title="Payments",
                               state="proposed", pr_number=12)
            billing = Feature(project_id=p.id, slug="billing", title="Billing",
                              state="proposed", pr_number=12)
            db.add_all([payments, billing])
            await db.flush()
            # A link out of a deleted row must not dangle (the FK is real on
            # Postgres) — and an incoming one from a surviving row neither.
            db.add(FeatureLink(src_feature_id=payments.id,
                               dst_feature_id=sessions.id, kind="powered_by"))
            db.add(FeatureLink(src_feature_id=sessions.id,
                               dst_feature_id=payments.id, kind="integrates_with"))
            # A SURVIVING row parented to a doomed one: the FK is real on
            # Postgres, so the delete is illegal unless the parent is nulled.
            db.add(Feature(project_id=p.id, slug="payments-child",
                           title="Payments child", state="live",
                           commit_sha="merged-sha", parent_feature_id=payments.id))
            item = WorkItem(repo=REPO, number=7, kind="issue", state="open",
                            title="Billing bug", feature_id=billing.id,
                            triage_state="classified")
            db.add(item)
            await db.flush()
            # The triage proposal that proposed the doomed capability: it stays
            # as audit, so its FK has to be cleared rather than cascaded.
            db.add(TriageProposal(work_item_id=item.id,
                                  proposed_feature_id=payments.id,
                                  status="accepted"))
            await db.commit()

    c.portal.call(_write)


def _dangling_fks(c) -> dict:
    """The two FKs into a doomed capability that the cleanup has to clear."""
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import Feature, TriageProposal

    async def _read():
        async with db_session() as db:
            child = (await db.execute(select(Feature).where(
                Feature.slug == "payments-child"))).scalars().one()
            prop = (await db.execute(
                select(TriageProposal))).scalars().one()
            return {"parent_feature_id": child.parent_feature_id,
                    "proposed_feature_id": prop.proposed_feature_id}

    return c.portal.call(_read)


def _seed_pointer(c, *, pr_number: int = 12, head_sha: str = "c0",
                  opened_at: datetime | None = None, status: str = "open") -> None:
    """An open pointer row inserted behind the service's back — what a racing
    concurrent first edit leaves committed."""
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import FeatureMapChange, Project

    async def _write():
        async with db_session() as db:
            p = (await db.execute(select(Project).where(
                Project.slug == SLUG))).scalars().one()
            db.add(FeatureMapChange(
                project_id=p.id, repo=REPO, branch=BRANCH, pr_number=pr_number,
                pr_url=f"https://github.com/{REPO}/pull/{pr_number}", status=status,
                head_sha=head_sha, behind=False, conflicting=False,
                opened_at=opened_at or datetime.now(timezone.utc)))
            await db.commit()

    c.portal.call(_write)


def _refresh(c) -> None:
    """One PR-lifecycle refresh, driven exactly as the sync loop drives it."""
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import Project

    # `app.state.sessions` is the facade adapter, which forwards only the public
    # surface; the sync loop calls this seam on the orchestrator itself.
    orch = getattr(c.app.state.sessions, "_orch", c.app.state.sessions)

    async def _go():
        async with db_session() as db:
            pid = (await db.execute(select(Project).where(
                Project.slug == SLUG))).scalars().one().id
        await orch._pm_sync_refresh_map_changes(pid)

    c.portal.call(_go)


async def _loop_thread() -> threading.Thread:
    """The thread the app's event loop runs on (a TestClient portal thread)."""
    return threading.current_thread()


@contextmanager
def _seeded(gh):
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        _seed(db_url)
        with _client(db_url, gh) as c:
            yield c


def _edit(c, project: str = SLUG, **body):
    """POST one edit. The project slug is positional so ``slug=`` stays free for
    the body's capability slug."""
    return c.post(f"/api/pm/projects/{project}/map-changes", json=body,
                  headers=IDENTITY_HEADERS)


def _committed(gh, index: int = -1) -> str:
    """The text of the file added by the recorded GraphQL commit at *index*."""
    payload = json.loads(gh.calls[index][2])
    added = payload["variables"]["input"]["fileChanges"]["additions"][0]
    return base64.b64decode(added["contents"]).decode()


# -- tests --------------------------------------------------------------------


def test_create_opens_the_branch_the_pr_and_the_pointer_row():
    gh = FakeGh(_first_create_answers())
    with _seeded(gh) as c:
        r = _edit(c, op="create", slug="payments", title="Payments", summary="Take money.")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["path"] == f"{DOCS}/payments.md"
        assert body["commitSha"] == "c0"
        ptr = body["pointer"]
        assert ptr["branch"] == BRANCH
        assert ptr["prNumber"] == 12
        assert ptr["prUrl"] == f"https://github.com/{REPO}/pull/12"
        assert ptr["status"] == "open"
        assert ptr["headSha"] == "c0"
        assert ptr["behind"] is False and ptr["conflicting"] is False
        assert ptr["openedAt"]

        argv = _argv(gh)
        # The branch-doc probe comes first: docs can be stranded on the branch
        # with no pointer row, and validation must see them.
        assert argv[0] == ["api", f"repos/{REPO}/git/ref/heads/{BRANCH}"]
        assert argv[1] == ["api", f"repos/{REPO}/git/ref/heads/master"]
        assert argv[2] == ["api", f"repos/{REPO}/git/ref/heads/{BRANCH}"]
        assert argv[3][:4] == ["api", "-X", "POST", f"repos/{REPO}/git/refs"]
        assert f"ref=refs/heads/{BRANCH}" in argv[3] and "sha=base1" in argv[3]
        assert argv[4] == ["api", f"repos/{REPO}/contents/{DOCS}/payments.md?ref={BRANCH}"]
        assert argv[5] == ["api", "graphql", "--input", "-"]
        assert argv[6] == [
            "api", f"repos/{REPO}/pulls?head=o:{BRANCH}&base=master&state=open"]
        assert argv[7][:4] == ["api", "-X", "POST", f"repos/{REPO}/pulls"]
        assert "title=Capability map changes: Example product" in argv[7]
        assert len(argv) == 8

        # The committed text is the canonical serialized doc, proposed on create.
        text = _committed(gh, 5)
        assert "slug: payments" in text and "state: proposed" in text
        assert "Take money." in text
        assert json.loads(gh.calls[5][2])["variables"]["input"]["expectedHeadOid"] == "base1"

        assert _pointers(c) == [(REPO, BRANCH, 12, "open", "c0", False)]
        assert _sync_requested(c) is True
        assert _audits(c, "pm.map_changes.create") == [{
            "slug": SLUG, "op": "create", "capability": "payments",
            "path": f"{DOCS}/payments.md", "commitSha": "c0", "prNumber": 12}]


def test_second_edit_reuses_the_pointer_and_sees_the_branch_only_capability():
    """No second PR, one commit — and ``payments`` (created on the branch, not
    yet indexed) is a valid parent because the branch docs are read."""
    gh = FakeGh([
        *_first_create_answers(),
        # second edit: the branch docs are listed, then billing is committed
        _listing("payments.md"), _contents(_doc("payments", "Payments")),
        _ref("base1"), _ref("c0"), _404, _commit("c1"),
    ])
    with _seeded(gh) as c:
        assert _edit(c, op="create", slug="payments", title="Payments").status_code == 200
        first = len(gh.calls)
        r = _edit(c, op="create", slug="billing", title="Billing", parentSlug="payments")
        assert r.status_code == 200, r.text
        assert r.json()["commitSha"] == "c1"
        assert r.json()["pointer"]["prNumber"] == 12

        argv = _argv(gh)[first:]
        # An open pointer proves the branch exists, so no second existence probe.
        assert argv[0] == ["api", f"repos/{REPO}/contents/{DOCS}?ref={BRANCH}"]
        assert argv[1] == ["api", f"repos/{REPO}/contents/{DOCS}/payments.md?ref={BRANCH}"]
        assert [a for a in argv if a[:1] == ["api"] and "graphql" in a] == [
            ["api", "graphql", "--input", "-"]]
        assert not [a for a in argv if a[:4] == ["api", "-X", "POST", f"repos/{REPO}/pulls"]]
        assert json.loads(gh.calls[-1][2])["variables"]["input"]["expectedHeadOid"] == "c0"

        assert _pointers(c) == [(REPO, BRANCH, 12, "open", "c1", False)]


def test_a_branch_only_capability_is_a_valid_link_target():
    """R8b-10 as amended: a capability proposed on the branch can be linked to
    from the same PR, before any index pass has seen it."""
    gh = FakeGh([
        *_first_create_answers(),
        _listing("payments.md"), _contents(_doc("payments", "Payments")),
        _ref("base1"), _ref("c0"), _contents(_doc("sessions", "Sessions")), _commit("c1"),
    ])
    with _seeded(gh) as c:
        assert _edit(c, op="create", slug="payments", title="Payments").status_code == 200
        r = _edit(c, op="links", slug="sessions",
                  links=[{"kind": "powered_by", "project": SLUG, "feature": "payments"}])
        assert r.status_code == 200, r.text
        text = _committed(gh)
        assert "kind: powered_by" in text and "feature: payments" in text


def test_unknown_slug_bad_op_and_missing_primary_repo_are_422():
    gh = FakeGh([_404, *_first_create_answers()])
    with _seeded(gh) as c:
        r = _edit(c, op="rename", slug="nope", title="Nope")
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["error"] == "not_found"

        r = _edit(c, op="explode", slug="sessions")
        assert r.status_code == 422
        assert r.json()["detail"]["error"] == "bad_op"

        r = _edit(c, "bare-product", op="create", slug="anything", title="Anything")
        assert r.status_code == 422
        assert r.json()["detail"]["error"] == "no_primary_repo"

        # Only the branch probe for the `rename`: an op the shape check rejects
        # never reaches GitHub at all.
        assert _argv(gh) == [["api", f"repos/{REPO}/git/ref/heads/{BRANCH}"]]
        assert _pointers(c) == []

        # …and the same holds with a pointer open, where context() would
        # otherwise cost a directory listing plus one read per branch doc.
        assert _edit(c, op="create", slug="payments", title="Payments").status_code == 200
        after_create = len(gh.calls)
        assert _edit(c, op="explode", slug="payments").status_code == 422
        assert len(gh.calls) == after_create
        assert not [a for a in _argv(gh)[after_create:] if "contents" in "".join(a)]


def test_a_project_slug_that_is_not_branch_safe_is_422_before_any_github_call():
    gh = FakeGh([])
    with _seeded(gh) as c:
        r = _edit(c, UNSAFE_SLUG, op="create", slug="payments", title="Payments")
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["error"] == "bad_slug"
        assert gh.calls == []


def test_create_of_an_existing_branch_doc_is_slug_taken_and_names_the_branch():
    gh = FakeGh([
        *_first_create_answers(),
        _listing("payments.md"), _contents(_doc("payments", "Payments")),
    ])
    with _seeded(gh) as c:
        assert _edit(c, op="create", slug="payments", title="Payments").status_code == 200
        r = _edit(c, op="create", slug="payments", title="Payments again")
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["error"] == "slug_taken"
        assert BRANCH in r.json()["detail"]["message"]


def test_a_moved_head_re_reads_the_doc_and_re_applies_the_op():
    """The retry must not re-commit the text computed at the old head:
    ``fileChanges.additions`` replaces the file, so a concurrent writer's line
    would be silently dropped (the AI map-diff session shares this branch)."""
    v1 = _doc("payments", "Payments", body="# Payments\n\nOld summary.")
    v2 = _doc("payments", "Payments", body="# Payments\n\nOld summary.\n\nConcurrent line.")
    gh = FakeGh([
        *_first_create_answers(),
        _listing("payments.md"), _contents(v1),   # branch docs for validation
        _ref("base1"),                            # default-branch head
        _ref("c0"), _contents(v1),                # head + doc at the old head
        _STALE,                                   # someone else committed
        _ref("moved"), _contents(v2),             # re-read BOTH, re-apply
        _commit("c1"),
    ])
    with _seeded(gh) as c:
        assert _edit(c, op="create", slug="payments", title="Payments").status_code == 200
        first = len(gh.calls)
        r = _edit(c, op="summary", slug="payments", summary="New summary.")
        assert r.status_code == 200, r.text

        argv = _argv(gh)[first:]
        assert argv[-3] == ["api", f"repos/{REPO}/git/ref/heads/{BRANCH}"]
        assert argv[-2] == ["api", f"repos/{REPO}/contents/{DOCS}/payments.md?ref={BRANCH}"]
        assert argv[-1] == ["api", "graphql", "--input", "-"]
        assert json.loads(gh.calls[-1][2])["variables"]["input"]["expectedHeadOid"] == "moved"

        text = _committed(gh)
        assert "New summary." in text and "Old summary." not in text
        assert "Concurrent line." in text        # the concurrent write survived


def test_a_head_that_moves_twice_is_409_conflict():
    gh = FakeGh([
        _404, _ref("base1"), _404, _created("base1"), _404,
        _STALE, _ref("moved"), _404, _STALE,
    ])
    with _seeded(gh) as c:
        r = _edit(c, op="create", slug="payments", title="Payments")
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "conflict"
        assert r.json()["detail"]["step"] == "commit"
        assert _pointers(c) == []


def test_a_github_failure_is_502_with_the_step():
    gh = FakeGh([_404, ("", 1, "HTTP 502: Bad Gateway")])
    with _seeded(gh) as c:
        r = _edit(c, op="create", slug="payments", title="Payments")
        assert r.status_code == 502, r.text
        assert r.json()["detail"]["error"] == "github"
        assert r.json()["detail"]["step"] == "head"


def test_a_gh_timeout_is_502_not_a_500():
    class _TimeoutGh:
        def __init__(self) -> None:
            self.calls: list = []

        def __call__(self, args, token=None, *, input=None, timeout=None):
            self.calls.append((list(args), token, input))
            raise subprocess.TimeoutExpired(["gh", *args], timeout or 60)

    gh = _TimeoutGh()
    with _seeded(gh) as c:
        r = _edit(c, op="create", slug="payments", title="Payments")
        assert r.status_code == 502, r.text
        assert r.json()["detail"] == {
            "error": "github", "step": "timeout",
            "message": r.json()["detail"]["message"]}
        assert "timed out" in r.json()["detail"]["message"]
        assert gh.calls


def test_these_tests_never_mint_a_real_github_token():
    """The whole file is offline by construction: no `gh` call carries a token,
    and the App-token provider is the injected one, not ``app_token_for_repo``
    (which reads the App's private key, signs a JWT and calls api.github.com)."""
    gh = FakeGh(_first_create_answers())
    with _seeded(gh) as c:
        orch = getattr(c.app.state.sessions, "_orch", c.app.state.sessions)
        assert orch._gh_token_provider("o/r") is None
        assert orch._gh_env == {}
        svc = orch.pm_map_changes()
        built = svc._gh_factory(REPO)
        assert built._app is None and built._pat is None and built._token() is None

        assert _edit(c, op="create", slug="payments", title="Payments").status_code == 200
        assert [call[1] for call in gh.calls] == [None] * len(gh.calls)


def test_the_github_client_is_built_off_the_event_loop():
    """Minting the App token reads a PEM, takes a process-global lock and may
    POST — all blocking, so the factory has to run in a worker thread."""
    import server.pm.map_changes_gh as gh_mod

    gh = FakeGh(_first_create_answers())
    built: list[threading.Thread] = []
    real = gh_mod.GhWrite

    def _record(*a, **k):
        built.append(threading.current_thread())
        return real(*a, **k)

    with _seeded(gh) as c:
        with patch.object(gh_mod, "GhWrite", _record):
            assert _edit(c, op="create", slug="payments", title="Payments").status_code == 200
        loop_thread = c.portal.call(_loop_thread)
        assert built and all(t is not loop_thread for t in built)


def test_a_pr_that_failed_to_record_is_adopted_not_reopened():
    """The GitHub commit and the PR land before the DB row: a failure between
    them strands a doc (and possibly a PR) with no pointer. ``create`` still
    refuses the slug, but every other op works and adopts the open PR."""
    doc = _doc("payments", "Payments")
    gh = FakeGh([
        # 1) create: the commit lands, opening the PR 502s → the row rolls back
        _404, _ref("base1"), _404, _created("base1"), _404, _commit("c0"),
        _NO_PR, ("", 1, "HTTP 502: Bad Gateway"),
        # 2) the same create again: the doc is visible on the branch now
        _ref("c0"), _listing("payments.md"), _contents(doc),
        # 3) any other op on that slug works, and adopts the PR that IS open —
        #    finding it on the list endpoint, then READING it for its state
        _ref("c0"), _listing("payments.md"), _contents(doc),
        _ref("base1"), _ref("c0"), _contents(doc), _commit("c1"),
        _pr_list(), _pr(mergeable_state="behind"),
    ])
    with _seeded(gh) as c:
        r = _edit(c, op="create", slug="payments", title="Payments")
        assert r.status_code == 502, r.text
        assert r.json()["detail"]["step"] == "pr"
        assert _pointers(c) == []
        after_failure = len(gh.calls)

        r = _edit(c, op="create", slug="payments", title="Payments")
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["error"] == "slug_taken"
        assert BRANCH in r.json()["detail"]["message"]

        r = _edit(c, op="summary", slug="payments", summary="Take money.")
        assert r.status_code == 200, r.text
        assert r.json()["pointer"]["prNumber"] == 12
        # …with the state from the single-PR READ, not the list endpoint (which
        # carries no `mergeable_state`, so `behind` would be a false negative).
        assert r.json()["pointer"]["behind"] is True
        assert _pointers(c) == [(REPO, BRANCH, 12, "open", "c1", True)]
        # The PR was adopted, never re-opened: a second `POST pulls` would
        # answer "A pull request already exists" forever.
        assert not [a for a in _argv(gh)[after_failure:]
                    if a[:4] == ["api", "-X", "POST", f"repos/{REPO}/pulls"]]


def test_an_open_pr_without_a_row_is_adopted_and_read_for_its_state():
    """Adoption builds the pointer row from ``GET pulls/{n}``, not from the list
    endpoint: only the single-PR read carries ``mergeable_state``, so a PR that
    is behind (or conflicting) would otherwise be recorded clean and the UI would
    offer neither Rebase nor the conflict banner."""
    gh = FakeGh([
        _ref("c0"), _EMPTY_LIST,   # the branch exists, with no docs on it yet
        _ref("base1"), _ref("c0"), _404, _commit("c1"),
        _pr_list(number=12), _pr(number=12, mergeable_state="behind"),
    ])
    with _seeded(gh) as c:
        r = _edit(c, op="create", slug="payments", title="Payments")
        assert r.status_code == 200, r.text
        assert r.json()["pointer"]["prNumber"] == 12
        assert r.json()["pointer"]["behind"] is True
        assert _pointers(c) == [(REPO, BRANCH, 12, "open", "c1", True)]
        argv = _argv(gh)
        # `base` is part of the lookup: a stacked PR off this branch targeting
        # something else is not the product's map-changes PR.
        assert argv[-2] == [
            "api", f"repos/{REPO}/pulls?head=o:{BRANCH}&base=master&state=open"]
        assert argv[-1] == ["api", f"repos/{REPO}/pulls/12"]
        assert not [a for a in argv if a[:4] == ["api", "-X", "POST", f"repos/{REPO}/pulls"]]


def test_the_newest_open_row_is_the_products_pointer():
    """``open_pointer_rows`` is what enforces "at most one open map-changes PR
    per product" (R8b-5) — there is no partial unique index, so when two open
    rows do exist every reader has to agree on the SAME one: the newest."""
    from server.pm.map_changes import open_pointer_row, open_pointer_rows

    gh = FakeGh([])
    with _seeded(gh) as c:
        old = datetime(2026, 9, 1, tzinfo=timezone.utc)
        new = datetime(2026, 9, 10, tzinfo=timezone.utc)
        _seed_pointer(c, pr_number=11, head_sha="old", opened_at=old)
        _seed_pointer(c, pr_number=12, head_sha="new", opened_at=new)
        # …plus an abandoned row newer than both: status gates the lookup first.
        _seed_pointer(c, pr_number=13, head_sha="dead",
                      opened_at=datetime(2026, 9, 11, tzinfo=timezone.utc),
                      status="abandoned")

        from sqlalchemy import select

        from server.db import db_session
        from server.db.models import Project

        async def _read():
            async with db_session() as db:
                pid = (await db.execute(select(Project).where(
                    Project.slug == SLUG))).scalars().one().id
                one = await open_pointer_row(db, pid)
                batched = await open_pointer_rows(db, [pid])
                return one.pr_number, batched[pid].pr_number

        assert c.portal.call(_read) == (12, 12)


def test_an_unparseable_github_timestamp_never_raises():
    """A polling pass must not die on a timestamp GitHub changed the shape of;
    the caller falls back to its own clock."""
    from server.pm.map_changes import _parse_gh_time

    assert _parse_gh_time("2026-09-11T10:00:00Z") == datetime(
        2026, 9, 11, 10, 0, tzinfo=timezone.utc)
    assert _parse_gh_time(None) is None
    assert _parse_gh_time("") is None
    assert _parse_gh_time("yesterday") is None
    assert _parse_gh_time("2026-13-45T99:99:99Z") is None


def test_a_nameless_product_titles_the_pr_with_its_slug():
    """``project.name`` is nullable, and an f-string over it titles the PR
    "Capability map changes: None"."""
    gh = FakeGh(_first_create_answers())
    with _seeded(gh) as c:
        r = _edit(c, NAMELESS_SLUG, op="create", slug="payments", title="Payments")
        assert r.status_code == 200, r.text
        assert r.json()["pointer"]["branch"] == NAMELESS_BRANCH
        post = [a for a in _argv(gh) if a[:4] == ["api", "-X", "POST", f"repos/{REPO}/pulls"]]
        assert post and f"title=Capability map changes: {NAMELESS_SLUG}" in post[0]


def test_a_pointer_that_appeared_mid_edit_is_reused_not_duplicated():
    """Two concurrent first edits (R8b-5 is enforced by lookup, not by a partial
    unique index, which SQLite's reconcile path cannot add): the lookup is re-run
    immediately before the insert, so the loser reuses the winner's row instead
    of opening a second "one open pointer per product"."""
    import server.pm.map_changes as mc

    gh = FakeGh([
        _ref("c0"), _EMPTY_LIST,   # the branch exists, with no docs on it yet
        _ref("base1"), _ref("c0"), _404, _commit("c1"), _pr_list(), _pr(),
    ])
    with _seeded(gh) as c:
        _seed_pointer(c)                       # the racing writer's row
        real = mc.open_pointer_row
        seen = {"n": 0}

        async def _blind_first(db, project_id):
            """The racing writer had not committed when WE looked."""
            seen["n"] += 1
            return None if seen["n"] == 1 else await real(db, project_id)

        with patch.object(mc, "open_pointer_row", _blind_first):
            r = _edit(c, op="create", slug="payments", title="Payments")
        assert r.status_code == 200, r.text
        assert seen["n"] >= 2                  # the re-check really ran
        assert r.json()["pointer"]["prNumber"] == 12
        # ONE row, the racing writer's, with our commit on it.
        assert _pointers(c) == [(REPO, BRANCH, 12, "open", "c1", False)]


def test_delete_without_a_pointer_a_branch_or_a_pr_is_404():
    gh = FakeGh([
        _404,          # GET git/ref/heads/<branch> — no branch
        _EMPTY_LIST,   # GET pulls?head=… — no PR either
    ])
    with _seeded(gh) as c:
        r = c.delete(f"/api/pm/projects/{SLUG}/map-changes", headers=IDENTITY_HEADERS)
        assert r.status_code == 404, r.text
        assert r.json()["detail"]["error"] == "no_open_map_changes"


def test_delete_adopts_a_stranded_pr_and_abandons_it():
    """The commit and the PR land before the pointer row, so a crash in between
    strands them. Abandon must adopt and close, not 404 forever."""
    gh = FakeGh([
        _ref("c0"),                    # the branch is there
        _pr_list(), _pr(),             # …and so is an open PR → read it
        _OK,                           # PATCH pulls/12 state=closed
        _OK,                           # DELETE git/refs/heads/<branch>
    ])
    with _seeded(gh) as c:
        _seed_proposed_capabilities(c)
        r = c.delete(f"/api/pm/projects/{SLUG}/map-changes", headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        assert r.json()["pointer"]["status"] == "abandoned"
        assert r.json()["pointer"]["prNumber"] == 12
        # …and the adopted PR's proposals are cleaned up like any other abandon.
        assert r.json()["cleanup"] == {"deleted": 1, "orphaned": 1}

        argv = _argv(gh)
        assert argv[1] == [
            "api", f"repos/{REPO}/pulls?head=o:{BRANCH}&base=master&state=open"]
        assert argv[2] == ["api", f"repos/{REPO}/pulls/12"]
        assert argv[3][:4] == ["api", "-X", "PATCH", f"repos/{REPO}/pulls/12"]
        assert argv[4][:4] == ["api", "-X", "DELETE", f"repos/{REPO}/git/refs/heads/{BRANCH}"]
        assert [row[2:4] for row in _pointers(c)] == [(12, "abandoned")]


def test_delete_deletes_a_stranded_branch_that_never_got_a_pr():
    """The commit lands, opening the PR fails: a branch with docs on it and no
    PR at all. Abandon deletes it and records an abandoned pointer with no PR."""
    gh = FakeGh([
        _ref("c0"),     # the branch is there
        _EMPTY_LIST,    # …but no PR was ever opened
        _OK,            # DELETE git/refs/heads/<branch>
    ])
    with _seeded(gh) as c:
        r = c.delete(f"/api/pm/projects/{SLUG}/map-changes", headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        assert r.json()["pointer"]["status"] == "abandoned"
        assert r.json()["pointer"]["prNumber"] is None
        assert r.json()["cleanup"] == {"deleted": 0, "orphaned": 0}

        argv = _argv(gh)
        assert argv[-1][:4] == ["api", "-X", "DELETE", f"repos/{REPO}/git/refs/heads/{BRANCH}"]
        assert not [a for a in argv if a[:3] == ["api", "-X", "PATCH"]]
        assert [row[2:4] for row in _pointers(c)] == [(None, "abandoned")]
        assert _audits(c, "pm.map_changes.abandon") == [
            {"slug": SLUG, "branch": BRANCH, "prNumber": None}]


def test_delete_closes_the_pr_deletes_the_branch_and_abandons_the_row():
    gh = FakeGh([
        *_first_create_answers(),
        _OK,  # PATCH pulls/12 state=closed
        _OK,  # DELETE git/refs/heads/<branch>
        _404, _EMPTY_LIST,  # the second abandon: neither branch nor PR is left
    ])
    with _seeded(gh) as c:
        assert _edit(c, op="create", slug="payments", title="Payments").status_code == 200
        first = len(gh.calls)
        r = c.delete(f"/api/pm/projects/{SLUG}/map-changes", headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        assert r.json()["pointer"]["status"] == "abandoned"

        argv = _argv(gh)[first:]
        assert argv[0][:4] == ["api", "-X", "PATCH", f"repos/{REPO}/pulls/12"]
        assert "state=closed" in argv[0]
        assert argv[1][:4] == ["api", "-X", "DELETE", f"repos/{REPO}/git/refs/heads/{BRANCH}"]

        assert [row[3] for row in _pointers(c)] == ["abandoned"]
        assert _audits(c, "pm.map_changes.abandon") == [
            {"slug": SLUG, "branch": BRANCH, "prNumber": 12}]

        # …and the pointer is gone, so a second abandon 404s.
        assert c.delete(f"/api/pm/projects/{SLUG}/map-changes",
                        headers=IDENTITY_HEADERS).status_code == 404


def test_rebase_is_409_until_the_pr_is_behind_then_updates_the_branch():
    gh = FakeGh([
        *_first_create_answers(),
        _OK,  # PUT pulls/12/update-branch
    ])
    with _seeded(gh) as c:
        assert _edit(c, op="create", slug="payments", title="Payments").status_code == 200
        first = len(gh.calls)

        r = c.post(f"/api/pm/projects/{SLUG}/map-changes/rebase", headers=IDENTITY_HEADERS)
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "not_behind"
        assert len(gh.calls) == first

        _set_behind(c, True)
        r = c.post(f"/api/pm/projects/{SLUG}/map-changes/rebase", headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        assert r.json()["pointer"]["behind"] is False
        assert _argv(gh)[first][:4] == ["api", "-X", "PUT", f"repos/{REPO}/pulls/12/update-branch"]
        assert _audits(c, "pm.map_changes.rebase") == [
            {"slug": SLUG, "branch": BRANCH, "prNumber": 12}]


def test_rebase_without_a_pointer_is_404():
    gh = FakeGh([])
    with _seeded(gh) as c:
        r = c.post(f"/api/pm/projects/{SLUG}/map-changes/rebase", headers=IDENTITY_HEADERS)
        assert r.status_code == 404, r.text
        assert r.json()["detail"]["error"] == "no_open_map_changes"


# -- PR lifecycle refresh (R8b-6) and abandon cleanup (R8b-7) -----------------


def test_refresh_reads_the_pr_and_records_behind_and_conflicting():
    gh = FakeGh([*_first_create_answers(),
                 _pr(mergeable_state="behind"), _pr(mergeable_state="dirty")])
    with _seeded(gh) as c:
        assert _edit(c, op="create", slug="payments", title="Payments").status_code == 200
        first = len(gh.calls)

        _refresh(c)
        assert _argv(gh)[first] == ["api", f"repos/{REPO}/pulls/12"]
        row = _pointer_row(c)
        assert (row["status"], row["behind"], row["conflicting"]) == ("open", True, False)

        _refresh(c)
        row = _pointer_row(c)
        assert (row["status"], row["behind"], row["conflicting"]) == ("open", False, True)
        assert row["last_error"] is None


def test_refresh_marks_a_merged_pr_merged():
    gh = FakeGh([*_first_create_answers(), _pr_closed(merged=True)])
    with _seeded(gh) as c:
        assert _edit(c, op="create", slug="payments", title="Payments").status_code == 200
        _seed_proposed_capabilities(c)
        _refresh(c)

        row = _pointer_row(c)
        assert row["status"] == "merged"
        assert row["merged_at"] is not None
        # A merge is NOT an abandon: the docs are on the default branch now and
        # the next index pass stamps commit_sha over the PR columns.
        assert set(_features(c)) == {"sessions", "dictation", "payments", "billing",
                                     "payments-child"}

        # …and the pointer is closed, so the next pass reads nothing.
        before = len(gh.calls)
        _refresh(c)
        assert len(gh.calls) == before


def test_refresh_abandons_a_closed_unmerged_pr_and_cleans_up():
    """A PR closed on GitHub rather than through the UI reaches the same end
    state (R8b-7): the never-merged proposals must not linger forever."""
    gh = FakeGh([*_first_create_answers(), _pr_closed(merged=False)])
    with _seeded(gh) as c:
        assert _edit(c, op="create", slug="payments", title="Payments").status_code == 200
        _seed_proposed_capabilities(c)
        _refresh(c)

        row = _pointer_row(c)
        assert row["status"] == "abandoned"
        assert row["closed_at"] is not None and row["merged_at"] is None
        feats = _features(c)
        assert "payments" not in feats          # proposed-only, unreferenced
        assert feats["billing"][0] is True      # referenced ⇒ orphaned, not deleted
        assert feats["sessions"] == (False, None, "merged-sha")
        assert _dangling_fks(c) == {"parent_feature_id": None,
                                    "proposed_feature_id": None}


def test_a_failed_pr_read_records_last_error_and_leaves_the_status():
    gh = FakeGh([*_first_create_answers(), ("", 1, "HTTP 502: Bad Gateway")])
    with _seeded(gh) as c:
        assert _edit(c, op="create", slug="payments", title="Payments").status_code == 200
        _refresh(c)
        row = _pointer_row(c)
        assert row["status"] == "open"
        assert "502" in (row["last_error"] or "")

        # …and the next clean read clears it.
        gh.answers.append(_pr())
        _refresh(c)
        assert _pointer_row(c)["last_error"] is None


def test_a_hung_gh_during_a_pr_read_does_not_abandon_the_pr():
    """`subprocess.TimeoutExpired` escapes `_run_gh` rather than becoming a
    GhWriteError — it must land in ``last_error`` like any other failure, never
    take the PR out of ``open``."""
    class _HangAfter:
        def __init__(self, answers):
            self.inner = FakeGh(answers)
            self.calls = self.inner.calls

        def __call__(self, args, token=None, *, input=None, timeout=None):
            if self.inner.answers:
                return self.inner(args, token, input=input, timeout=timeout)
            self.calls.append((list(args), token, input))
            raise subprocess.TimeoutExpired(["gh", *args], timeout or 60)

    gh = _HangAfter(_first_create_answers())
    with _seeded(gh) as c:
        assert _edit(c, op="create", slug="payments", title="Payments").status_code == 200
        _refresh(c)
        row = _pointer_row(c)
        assert row["status"] == "open"
        assert "timed out" in (row["last_error"] or "")


def test_refresh_without_an_open_pointer_makes_no_github_call():
    gh = FakeGh([])
    with _seeded(gh) as c:
        _refresh(c)
        assert gh.calls == []


def test_delete_deletes_unreferenced_proposals_and_orphans_referenced_ones():
    """R8b-7: abandoning drops the branch, so a capability that only ever lived
    on it is deleted — unless something references it, which is orphaned instead
    so no FK (a work item's ``feature_id``) is left dangling."""
    gh = FakeGh([*_first_create_answers(), _OK, _OK])
    with _seeded(gh) as c:
        assert _edit(c, op="create", slug="payments", title="Payments").status_code == 200
        _seed_proposed_capabilities(c)

        r = c.delete(f"/api/pm/projects/{SLUG}/map-changes", headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        assert r.json()["cleanup"] == {"deleted": 1, "orphaned": 1}

        feats = _features(c)
        assert "payments" not in feats
        assert feats["billing"] == (True, 12, None)
        assert feats["sessions"] == (False, None, "merged-sha")   # merged: untouched
        assert feats["dictation"][0] is False
        # The surviving child and the triage proposal keep their rows, with the
        # FKs into the deleted capability cleared (real FKs on Postgres).
        assert "payments-child" in feats
        assert _dangling_fks(c) == {"parent_feature_id": None,
                                    "proposed_feature_id": None}
