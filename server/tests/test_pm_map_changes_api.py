"""POST/DELETE /api/pm/projects/{slug}/map-changes and POST …/rebase — the
map-changes service behind the API (roadmap drill-down spec §8/§9, row 8b).

Every GitHub call goes through the orchestrator's injectable ``_gh_run`` seam,
so these tests assert the exact ``gh`` argv sequence one edit produces (the
branch-from-default-head, the content read, the atomic GraphQL commit, the PR)
without touching the network. ``_sdlc_default_branch`` resolves through
``gitrepos.list_remote_branches``, which does NOT use that seam — it is patched
per client.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import tempfile
from contextlib import contextmanager
from unittest.mock import patch

from server.tests.test_pm_map_changes_gh import FakeGh

IDENTITY_HEADERS = {"Tailscale-User-Login": "testuser@example.com"}

REPO = "o/r"
SLUG = "example-product"
BRANCH = "pm/map-changes-example-product"
DOCS = "docs/product/features"


# -- fake `gh` answers --------------------------------------------------------

_404 = ("", 1, "HTTP 404: Not Found")
_OK = ("", 0, "")
_STALE = (json.dumps(
    {"errors": [{"message": 'Expected branch to point to "old" but it did not'}]}), 0, "")


def _ref(sha: str):
    return (json.dumps({"object": {"sha": sha}}), 0, "")


def _created(sha: str):
    return (json.dumps({"ref": f"refs/heads/{BRANCH}", "object": {"sha": sha}}), 0, "")


def _commit(oid: str):
    return (json.dumps({"data": {"createCommitOnBranch": {"commit": {"oid": oid}}}}), 0, "")


def _pr(*, number: int = 12, mergeable_state: str = "clean"):
    return (json.dumps({
        "number": number, "html_url": f"https://github.com/{REPO}/pull/{number}",
        "state": "open", "merged": False, "merged_at": None,
        "head": {"sha": "c0"}, "mergeable_state": mergeable_state}), 0, "")


def _contents(text: str):
    return (json.dumps({"content": base64.b64encode(text.encode()).decode(),
                        "sha": "blob1"}), 0, "")


def _listing(*names: str):
    return (json.dumps([{"name": n, "path": f"{DOCS}/{n}", "type": "file"} for n in names]), 0, "")


def _doc(slug: str, title: str, *, parent: str | None = None) -> str:
    front = f"slug: {slug}\ntitle: {title}\nstate: proposed"
    if parent:
        front += f"\nparent: {parent}"
    return f"---\n{front}\n---\n# {title}\n"


# -- fixtures -----------------------------------------------------------------


def _seed(db_url: str) -> None:
    """A product with a primary repo and two indexed capabilities (``sessions``
    with the child ``dictation``), plus a second product with no repo at all."""
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
            await db.commit()

    asyncio.run(_go())


@contextmanager
def _client(db_url: str, gh):
    """``test_pm_map_api._client`` with the orchestrator's ``gh`` runner swapped
    for *gh* before ``create_app``, and the default-branch probe stubbed."""
    from fastapi.testclient import TestClient
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    _clear = [
        "CCSWITCH_SESSIOND_ENDPOINT", "CCSWITCH_SESSIOND_SOCKET",
        "CCSWITCH_SESSIOND_HOST", "CCSWITCH_SESSIOND_PORT",
        "CCSWITCH_SESSIOND_CLIENT_CERT", "CCSWITCH_SESSIOND_CLIENT_KEY",
        "CCSWITCH_SESSIOND_CA", "CCSWITCH_DAEMON_ROLE",
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
            app = create_app(orchestrator=orch)
            with TestClient(app, raise_server_exceptions=False) as c:
                yield c
        finally:
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
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import ProjectRepo

    async def _read():
        async with db_session() as db:
            rows = (await db.execute(select(ProjectRepo))).scalars().all()
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


# -- tests --------------------------------------------------------------------


def test_create_opens_the_branch_the_pr_and_the_pointer_row():
    gh = FakeGh([_ref("base1"), _404, _created("base1"), _404, _commit("c0"), _pr()])
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
        assert argv[0] == ["api", f"repos/{REPO}/git/ref/heads/master"]
        assert argv[1] == ["api", f"repos/{REPO}/git/ref/heads/{BRANCH}"]
        assert argv[2][:4] == ["api", "-X", "POST", f"repos/{REPO}/git/refs"]
        assert f"ref=refs/heads/{BRANCH}" in argv[2] and "sha=base1" in argv[2]
        assert argv[3] == ["api", f"repos/{REPO}/contents/{DOCS}/payments.md?ref={BRANCH}"]
        assert argv[4] == ["api", "graphql", "--input", "-"]
        assert argv[5][:4] == ["api", "-X", "POST", f"repos/{REPO}/pulls"]
        assert len(argv) == 6

        # The committed text is the canonical serialized doc, proposed on create.
        payload = json.loads(gh.calls[4][2])
        added = payload["variables"]["input"]["fileChanges"]["additions"][0]
        assert added["path"] == f"{DOCS}/payments.md"
        text = base64.b64decode(added["contents"]).decode()
        assert "slug: payments" in text and "state: proposed" in text
        assert "Take money." in text
        assert payload["variables"]["input"]["expectedHeadOid"] == "base1"

        assert _pointers(c) == [(REPO, BRANCH, 12, "open", "c0", False)]
        assert _sync_requested(c) is True
        assert _audits(c, "pm.map_changes.create") == [{
            "slug": SLUG, "op": "create", "capability": "payments",
            "path": f"{DOCS}/payments.md", "commitSha": "c0", "prNumber": 12}]


def test_second_edit_reuses_the_pointer_and_sees_the_branch_only_capability():
    """No second PR, one commit — and ``payments`` (created on the branch, not
    yet indexed) is a valid parent because ``context()`` reads the branch docs."""
    gh = FakeGh([
        # first edit: create payments
        _ref("base1"), _404, _created("base1"), _404, _commit("c0"), _pr(),
        # second edit: context lists the branch docs, then commits billing
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
        assert argv[0] == ["api", f"repos/{REPO}/contents/{DOCS}?ref={BRANCH}"]
        assert argv[1] == ["api", f"repos/{REPO}/contents/{DOCS}/payments.md?ref={BRANCH}"]
        assert [a for a in argv if a[:1] == ["api"] and "graphql" in a] == [
            ["api", "graphql", "--input", "-"]]
        assert not [a for a in argv if a[:4] == ["api", "-X", "POST", f"repos/{REPO}/pulls"]]
        assert json.loads(gh.calls[-1][2])["variables"]["input"]["expectedHeadOid"] == "c0"

        assert _pointers(c) == [(REPO, BRANCH, 12, "open", "c1", False)]


def test_unknown_slug_bad_op_and_missing_primary_repo_are_422():
    gh = FakeGh([])
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

        assert gh.calls == []
        assert _pointers(c) == []


def test_create_of_an_existing_branch_doc_is_slug_taken():
    gh = FakeGh([
        _ref("base1"), _404, _created("base1"), _404, _commit("c0"), _pr(),
        _listing("payments.md"), _contents(_doc("payments", "Payments")),
    ])
    with _seeded(gh) as c:
        assert _edit(c, op="create", slug="payments", title="Payments").status_code == 200
        r = _edit(c, op="create", slug="payments", title="Payments again")
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["error"] == "slug_taken"


def test_a_moved_head_is_re_read_once_and_retried():
    gh = FakeGh([
        _ref("base1"), _404, _created("base1"), _404,
        _STALE,            # someone else committed between the read and the commit
        _ref("moved"),     # one re-read of the branch head
        _commit("c0"),
        _pr(),
    ])
    with _seeded(gh) as c:
        r = _edit(c, op="create", slug="payments", title="Payments")
        assert r.status_code == 200, r.text
        argv = _argv(gh)
        assert argv[5] == ["api", f"repos/{REPO}/git/ref/heads/{BRANCH}"]
        assert json.loads(gh.calls[6][2])["variables"]["input"]["expectedHeadOid"] == "moved"


def test_a_head_that_moves_twice_is_409_conflict():
    gh = FakeGh([
        _ref("base1"), _404, _created("base1"), _404,
        _STALE, _ref("moved"), _STALE,
    ])
    with _seeded(gh) as c:
        r = _edit(c, op="create", slug="payments", title="Payments")
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "conflict"
        assert r.json()["detail"]["step"] == "commit"
        assert _pointers(c) == []


def test_a_github_failure_is_502_with_the_step():
    gh = FakeGh([("", 1, "HTTP 502: Bad Gateway")])
    with _seeded(gh) as c:
        r = _edit(c, op="create", slug="payments", title="Payments")
        assert r.status_code == 502, r.text
        assert r.json()["detail"]["error"] == "github"
        assert r.json()["detail"]["step"] == "head"


def test_delete_without_a_pointer_is_404():
    gh = FakeGh([])
    with _seeded(gh) as c:
        r = c.delete(f"/api/pm/projects/{SLUG}/map-changes", headers=IDENTITY_HEADERS)
        assert r.status_code == 404, r.text
        assert r.json()["detail"]["error"] == "no_open_map_changes"


def test_delete_closes_the_pr_deletes_the_branch_and_abandons_the_row():
    gh = FakeGh([
        _ref("base1"), _404, _created("base1"), _404, _commit("c0"), _pr(),
        _OK,  # PATCH pulls/12 state=closed
        _OK,  # DELETE git/refs/heads/<branch>
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
        _ref("base1"), _404, _created("base1"), _404, _commit("c0"), _pr(),
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
