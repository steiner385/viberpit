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


# The scripted answers retiring `sessions` (one live child, `dictation`)
# produces: the branch-doc probe, the base head, then one `_commit_and_track`
# per op. `dictation` is reparented FIRST (children before the capability
# itself) — its commit is also the branch's first, so it opens the branch and
# the PR too. The `retire` commit on `sessions` follows on the now-existing
# branch, so its `ensure_branch` finds the ref directly (no create) and no PR
# call is made (the pointer from the first commit is already tracked).
def _retire_answers() -> list:
    return [
        _404, _ref("base1"),
        # reparent dictation → sessions' parent (None, sessions is root)
        _404, _created("base1"), _contents(CHILD_DOC), _commit("c1"), _NO_PR, _pr(),
        # retire sessions itself
        _ref("c1"), _contents(ROOT_DOC), _commit("c2"),
    ]


# The scripted answers merging `dictation` into `sessions` produces when
# NEITHER has any FeatureLink to repoint: the branch-doc probe, the base
# head, `merge()`'s own explicit `ensure_branch` (ref absent, then created —
# it has to exist before `merge()` can read any doc off it, even though
# nothing here needs a `links` commit), then ONE `_commit_and_track` (retire
# dictation) — dictation has no children in the seed fixture, so unlike
# `_retire_answers()` there is no reparent op first. That commit's OWN
# internal `ensure_branch` now finds the ref `merge()` already created (a
# single GET, no second create), then the existing doc read, the commit, the
# PR lookup, the PR (this is still the branch's first REAL content commit).
def _merge_answers() -> list:
    return [_404, _ref("base1"), _404, _created("base1"),
            _ref("base1"), _contents(CHILD_DOC), _commit("c1"),
            _NO_PR, _pr()]


# The scripted answers merging `dictation` into `sessions` where `dictation`
# has TWO outgoing FeatureLink rows: one to `third-cap` (must repoint onto
# `sessions`' own doc) and one DIRECTLY to `sessions` (must be dropped, not
# turned into a self-link). `dictation` has no children. Sequence: the
# branch-doc probe, the base head, the explicit `ensure_branch` (has to exist
# before any doc can be read off it), `_current_doc(sessions)` (read the
# target's current links before merging in the repointed ones), the
# `op=links` commit for `sessions` (opens the branch's first REAL commit —
# the PR too), the `op=links` commit clearing `dictation`'s own list, and
# finally the `op=retire` commit for `dictation`.
def _merge_outgoing_link_answers() -> list:
    return [
        _404, _ref("base1"),
        _404, _created("base1"),
        _contents(ROOT_DOC),                                        # _current_doc(sessions)
        _ref("base1"), _contents(ROOT_DOC), _commit("c1"), _NO_PR, _pr(),   # op=links sessions
        _ref("c1"), _contents(CHILD_DOC), _commit("c2"),             # op=links dictation (clear)
        _ref("c2"), _contents(CHILD_DOC), _commit("c3"),             # op=retire dictation
    ]


# The scripted answers merging `dictation` into `sessions` where `fourth-cap`
# (same project) already links TO `dictation` — the incoming-link repoint
# case. `dictation` has no outgoing links and no children, so there is no
# `sessions`-doc commit at all here, only `fourth-cap`'s and the retire.
def _merge_incoming_link_answers() -> list:
    return [
        _404, _ref("base1"),
        _404, _created("base1"),
        _contents(FOURTH_DOC_LINKING_TO_DICTATION),                  # _current_doc(fourth-cap)
        _ref("base1"), _contents(FOURTH_DOC_LINKING_TO_DICTATION), _commit("c1"), _NO_PR, _pr(),
        _ref("c1"), _contents(CHILD_DOC), _commit("c2"),             # op=retire dictation
    ]


# The scripted answers merging `dictation` into `sessions` where `sessions`
# itself (== into_slug) already links DIRECTLY to `dictation` — the incoming
# SELF-link case: repointing it naively would write `sessions -> sessions`
# into sessions' own doc; it must be dropped instead. `dictation` has no
# outgoing links and no children.
def _merge_incoming_self_link_answers() -> list:
    return [
        _404, _ref("base1"),
        _404, _created("base1"),
        _contents(SESSIONS_DOC_LINKING_TO_DICTATION),                # _current_doc(sessions)
        _ref("base1"), _contents(SESSIONS_DOC_LINKING_TO_DICTATION), _commit("c1"), _NO_PR, _pr(),
        _ref("c1"), _contents(CHILD_DOC), _commit("c2"),             # op=retire dictation
    ]


# The scripted answers merging `dictation` into `sessions` where `sessions`
# (the merge TARGET, whose doc `merge()` rewrites to add `dictation`'s
# outgoing link to `third-cap`) already carries a STALE, unresolvable link —
# same shape as `_merge_outgoing_link_answers()`, just with
# `SESSIONS_DOC_WITH_STALE_REF` instead of `ROOT_DOC` at the two points that
# read/re-read `sessions`' doc. Proves the stale ref does not block the
# merge: `merge()`'s link commits skip `validate_op`'s full-list
# re-validation (see the comment in `map_changes.py`).
def _merge_stale_ref_answers() -> list:
    return [
        _404, _ref("base1"),
        _404, _created("base1"),
        _contents(SESSIONS_DOC_WITH_STALE_REF),                      # _current_doc(sessions)
        _ref("base1"), _contents(SESSIONS_DOC_WITH_STALE_REF), _commit("c1"), _NO_PR, _pr(),
        _ref("c1"), _contents(CHILD_DOC), _commit("c2"),             # op=links dictation (clear)
        _ref("c2"), _contents(CHILD_DOC), _commit("c3"),             # op=retire dictation
    ]


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

    # Remote-sessiond vars (and CCSWITCH_DAEMON_ROLE) are cleared globally in
    # conftest.py (_no_ambient_remote_daemon_config) so create_app() stays
    # in-process; only the GH token vars need this test's own save/clear/restore.
    _clear = ["CCSWITCH_GH_PROJECT_TOKEN", "VIBERPIT_GH_PROJECT_TOKEN"]
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
                # `db_url` and `orch` (the real SessionOrchestrator, not the
                # facade `app.state.sessions` may wrap) are yielded alongside
                # `c` for tests that need a synchronous verification session
                # or that stub an orchestrator seam directly (e.g. pm-sync's
                # doc-read seam ahead of an inline resync).
                yield c, db_url, orch
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
    """Yields ``(c, db_url, orch)`` — see ``_client``. Most call sites only
    need ``c``; unpack all three and ignore what you don't use (``F841`` is
    ignored for test modules)."""
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        _seed(db_url)
        with _client(db_url, gh) as (c, url, orch):
            yield c, url, orch


def _edit(c, project: str = SLUG, **body):
    """POST one edit. The project slug is positional so ``slug=`` stays free for
    the body's capability slug."""
    return c.post(f"/api/pm/projects/{project}/map-changes", json=body,
                  headers=IDENTITY_HEADERS)


def _retire(c, project: str = SLUG, *, slug: str):
    """POST one retire. Mirrors ``_edit``'s convention for the sibling route."""
    return c.post(f"/api/pm/projects/{project}/map-changes/retire", json={"slug": slug},
                  headers=IDENTITY_HEADERS)


def _merge(c, project: str = SLUG, *, from_slug: str, into_slug: str):
    """POST one merge. Mirrors ``_retire``'s convention for the sibling route."""
    return c.post(f"/api/pm/projects/{project}/map-changes/merge",
                  json={"fromSlug": from_slug, "intoSlug": into_slug},
                  headers=IDENTITY_HEADERS)


def _committed(gh, index: int = -1) -> str:
    """The text of the file added by the recorded GraphQL commit at *index*."""
    payload = json.loads(gh.calls[index][2])
    added = payload["variables"]["input"]["fileChanges"]["additions"][0]
    return base64.b64decode(added["contents"]).decode()


def _all_committed(gh) -> list[tuple[str, str]]:
    """(path, text) for every recorded GraphQL commit, in call order — the
    generalization of `_committed()` (above) over the whole call history
    instead of one index."""
    out: list[tuple[str, str]] = []
    for args, _token, stdin in gh.calls:
        if args[:2] != ["api", "graphql"] or not stdin:
            continue
        try:
            payload = json.loads(stdin)
            added = payload["variables"]["input"]["fileChanges"]["additions"][0]
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        out.append((added["path"], base64.b64decode(added["contents"]).decode()))
    return out


def _stub_pm_sync(orch, gh, main_docs: dict[str, str]) -> None:
    """Point the orchestrator's pm-sync doc-read seam at fixture text instead
    of a real git clone: `ref=None` (default branch) returns *main_docs*
    verbatim; any other ref (the map-changes branch) overlays every doc this
    FakeGh has actually committed so far on top of *main_docs*, keyed by path
    — so `_resync_now` sees exactly what the SUT under test wrote, not a
    hand-maintained guess."""
    from server.pm.feature_index import FeatureDocsRead

    async def _fake_read(repo: str, ref: str | None = None):
        docs = dict(main_docs)
        if ref is not None:
            docs.update(_all_committed(gh))
        return FeatureDocsRead(sha="fake-sha", docs=list(docs.items()))
    orch._pm_sync_read_feature_docs = _fake_read


ROOT_DOC = "---\nslug: sessions\ntitle: Sessions\nstate: live\n---\n# Sessions\n"
CHILD_DOC = ("---\nslug: dictation\ntitle: Dictation\nstate: live\nparent: sessions\n"
             "---\n# Dictation\n")
# A third, unrelated capability — the `merge()` FeatureLink-repoint tests'
# outgoing-link target. No links of its own.
THIRD_DOC = "---\nslug: third-cap\ntitle: Third\nstate: live\n---\n# Third\n"
# A fourth capability that already links TO `dictation` — the incoming-link
# repoint case.
FOURTH_DOC_LINKING_TO_DICTATION = (
    "---\nslug: fourth-cap\ntitle: Fourth\nstate: live\n"
    "links:\n  - kind: integrates_with\n    project: example-product\n    feature: dictation\n"
    "---\n# Fourth\n")
# `sessions` (the merge TARGET) already linking DIRECTLY to `dictation` (the
# merge SOURCE) — the incoming self-link case: repointing this naively would
# write `sessions -> sessions` into sessions' own doc.
SESSIONS_DOC_LINKING_TO_DICTATION = (
    "---\nslug: sessions\ntitle: Sessions\nstate: live\n"
    "links:\n  - kind: powered_by\n    project: example-product\n    feature: dictation\n"
    "---\n# Sessions\n")
# `sessions` carrying a STALE link — `gone-cap` is not (and never will be, for
# this test) an indexed capability. `_pm_sync_apply_feature_plan` never
# cleans up a ref it can't resolve (it just skips it with a warning), so this
# is a real, reachable doc state, not a contrived one.
SESSIONS_DOC_WITH_STALE_REF = (
    "---\nslug: sessions\ntitle: Sessions\nstate: live\n"
    "links:\n  - kind: integrates_with\n    project: example-product\n    feature: gone-cap\n"
    "---\n# Sessions\n")


# -- tests --------------------------------------------------------------------


def test_create_opens_the_branch_the_pr_and_the_pointer_row():
    gh = FakeGh(_first_create_answers())
    with _seeded(gh) as (c, db_url, orch):
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
    with _seeded(gh) as (c, db_url, orch):
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
    with _seeded(gh) as (c, db_url, orch):
        assert _edit(c, op="create", slug="payments", title="Payments").status_code == 200
        r = _edit(c, op="links", slug="sessions",
                  links=[{"kind": "powered_by", "project": SLUG, "feature": "payments"}])
        assert r.status_code == 200, r.text
        text = _committed(gh)
        assert "kind: powered_by" in text and "feature: payments" in text


def test_unknown_slug_bad_op_and_missing_primary_repo_are_422():
    gh = FakeGh([_404, *_first_create_answers()])
    with _seeded(gh) as (c, db_url, orch):
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
    with _seeded(gh) as (c, db_url, orch):
        r = _edit(c, UNSAFE_SLUG, op="create", slug="payments", title="Payments")
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["error"] == "bad_slug"
        assert gh.calls == []


def test_delete_on_a_project_slug_that_is_not_branch_safe_is_422_not_500():
    # `_adopt_stranded` derives the branch through `map_changes_branch_name`, so
    # the DELETE route must map MapOpError like the edit route does.
    gh = FakeGh([])
    with _seeded(gh) as (c, db_url, orch):
        r = c.delete(f"/api/pm/projects/{UNSAFE_SLUG}/map-changes", headers=IDENTITY_HEADERS)
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["error"] == "bad_slug"
        assert gh.calls == []


def test_create_of_an_existing_branch_doc_is_slug_taken_and_names_the_branch():
    gh = FakeGh([
        *_first_create_answers(),
        _listing("payments.md"), _contents(_doc("payments", "Payments")),
    ])
    with _seeded(gh) as (c, db_url, orch):
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
    with _seeded(gh) as (c, db_url, orch):
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
    with _seeded(gh) as (c, db_url, orch):
        r = _edit(c, op="create", slug="payments", title="Payments")
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "conflict"
        assert r.json()["detail"]["step"] == "commit"
        assert _pointers(c) == []


def test_a_github_failure_is_502_with_the_step():
    gh = FakeGh([_404, ("", 1, "HTTP 502: Bad Gateway")])
    with _seeded(gh) as (c, db_url, orch):
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
    with _seeded(gh) as (c, db_url, orch):
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
    with _seeded(gh) as (c, db_url, orch):
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

    with _seeded(gh) as (c, db_url, orch):
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
    with _seeded(gh) as (c, db_url, orch):
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
    with _seeded(gh) as (c, db_url, orch):
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
    with _seeded(gh) as (c, db_url, orch):
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
    with _seeded(gh) as (c, db_url, orch):
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
    with _seeded(gh) as (c, db_url, orch):
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
    with _seeded(gh) as (c, db_url, orch):
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
    with _seeded(gh) as (c, db_url, orch):
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
    with _seeded(gh) as (c, db_url, orch):
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
    with _seeded(gh) as (c, db_url, orch):
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
        (json.dumps({"data": {"r0": {"nameWithOwner": REPO, "item": {
            "__typename": "PullRequest", "id": "PR_map", "number": 12,
            "url": f"https://github.com/{REPO}/pull/12", "repository": {"nameWithOwner": REPO},
            "closingIssuesReferences": {"pageInfo": {"hasNextPage": False}, "nodes": []}}}}}), 0, ""),
        (json.dumps({**_pr_json(), "head": {"sha": "a" * 40},
                     "base": {"sha": "b" * 40, "repo": {"full_name": REPO}}}), 0, ""),
        _OK,  # PUT pulls/12/update-branch
        (json.dumps({"data": {"r0": {"nameWithOwner": REPO, "item": {
            "__typename": "PullRequest", "id": "PR_map", "number": 12,
            "url": f"https://github.com/{REPO}/pull/12", "repository": {"nameWithOwner": REPO},
            "closingIssuesReferences": {"pageInfo": {"hasNextPage": False}, "nodes": []}}}}}), 0, ""),
        (json.dumps({**_pr_json(), "head": {"sha": "c" * 40},
                     "base": {"sha": "b" * 40, "repo": {"full_name": REPO}}}), 0, ""),
        (json.dumps({"status": "ahead", "base_commit": {"sha": "a" * 40},
                     "merge_base_commit": {"sha": "a" * 40}}), 0, ""),
        (json.dumps({"status": "ahead", "base_commit": {"sha": "b" * 40},
                     "merge_base_commit": {"sha": "b" * 40}}), 0, ""),
    ])
    with _seeded(gh) as (c, db_url, orch):
        assert _edit(c, op="create", slug="payments", title="Payments").status_code == 200
        first = len(gh.calls)

        r = c.post(f"/api/pm/projects/{SLUG}/map-changes/rebase", headers=IDENTITY_HEADERS)
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "not_behind"
        assert len(gh.calls) == first

        _set_behind(c, True)
        r = c.post(f"/api/pm/projects/{SLUG}/map-changes/rebase", headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        assert r.json()["pointer"]["behind"] is True  # accepted, not yet completed
        assert _argv(gh)[first + 2][:6] == ["api", "--hostname", "github.com", "-X", "PUT", f"repos/{REPO}/pulls/12/update-branch"]
        assert "expected_head_sha=" + "a" * 40 in _argv(gh)[first + 2]
        assert _audits(c, "pm.map_changes.rebase") == [
            {"slug": SLUG, "branch": BRANCH, "prNumber": 12}]
        _set_behind(c, False)
        r = c.post(f"/api/pm/projects/{SLUG}/map-changes/rebase", headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text  # pending action reconciles despite cached clean
        assert sum("PUT" in argv for argv in _argv(gh)) == 1


def test_rebase_without_a_pointer_is_404():
    gh = FakeGh([])
    with _seeded(gh) as (c, db_url, orch):
        r = c.post(f"/api/pm/projects/{SLUG}/map-changes/rebase", headers=IDENTITY_HEADERS)
        assert r.status_code == 404, r.text
        assert r.json()["detail"]["error"] == "no_open_map_changes"


# -- PR lifecycle refresh (R8b-6) and abandon cleanup (R8b-7) -----------------


def test_refresh_reads_the_pr_and_records_behind_and_conflicting():
    gh = FakeGh([*_first_create_answers(),
                 _pr(mergeable_state="behind"), _pr(mergeable_state="dirty")])
    with _seeded(gh) as (c, db_url, orch):
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
    with _seeded(gh) as (c, db_url, orch):
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
    with _seeded(gh) as (c, db_url, orch):
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
    with _seeded(gh) as (c, db_url, orch):
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
    with _seeded(gh) as (c, db_url, orch):
        assert _edit(c, op="create", slug="payments", title="Payments").status_code == 200
        _refresh(c)
        row = _pointer_row(c)
        assert row["status"] == "open"
        assert "timed out" in (row["last_error"] or "")


def test_refresh_without_an_open_pointer_makes_no_github_call():
    gh = FakeGh([])
    with _seeded(gh) as (c, db_url, orch):
        _refresh(c)
        assert gh.calls == []


def test_delete_deletes_unreferenced_proposals_and_orphans_referenced_ones():
    """R8b-7: abandoning drops the branch, so a capability that only ever lived
    on it is deleted — unless something references it, which is orphaned instead
    so no FK (a work item's ``feature_id``) is left dangling."""
    gh = FakeGh([*_first_create_answers(), _OK, _OK])
    with _seeded(gh) as (c, db_url, orch):
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


# -- retire (capability-management spec) --------------------------------------


def test_retire_refuses_with_live_dependents():
    gh = FakeGh([])
    with _seeded(gh) as (c, db_url, orch):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from server.db.models import Feature, Initiative, InitiativeFeature, Project

        engine = create_engine(db_url.replace("+aiosqlite", ""))
        Session = sessionmaker(bind=engine)
        with Session() as s:
            proj = s.query(Project).filter_by(slug=SLUG).one()
            dictation = s.query(Feature).filter_by(project_id=proj.id, slug="dictation").one()
            init = Initiative(slug="init-a", title="Init A")
            s.add(init)
            s.flush()
            s.add(InitiativeFeature(initiative_id=init.id, feature_id=dictation.id, is_primary=True))
            s.commit()

        r = _retire(c, slug="dictation")
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["error"] == "has_dependents"
        # The guard is a pure DB check before any GitHub call.
        assert gh.calls == []


def test_retire_reparents_children_and_syncs_the_db_inline():
    gh = FakeGh(_retire_answers())
    with _seeded(gh) as (c, db_url, orch):
        _stub_pm_sync(orch, gh, {
            f"{DOCS}/sessions.md": ROOT_DOC, f"{DOCS}/dictation.md": CHILD_DOC})
        r = _retire(c, slug="sessions")
        assert r.status_code == 200, r.text
        assert r.json()["pointer"]["prNumber"] == 12

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from server.db.models import Feature, Project

        engine = create_engine(db_url.replace("+aiosqlite", ""))
        Session = sessionmaker(bind=engine)
        with Session() as s:
            proj = s.query(Project).filter_by(slug=SLUG).one()
            sessions_row = s.query(Feature).filter_by(project_id=proj.id, slug="sessions").one()
            dictation_row = s.query(Feature).filter_by(project_id=proj.id, slug="dictation").one()
            assert sessions_row.state == "sunset"
            assert dictation_row.parent_feature_id is None  # reparented to root (the grandparent)

        assert _audits(c, "pm.map_changes.retire") == [{
            "slug": SLUG, "capability": "sessions", "childrenReparented": 1,
            "proposalsCleared": 0, "commitSha": "c2", "prNumber": 12}]


# -- merge (capability-management spec) ---------------------------------------


def test_merge_refuses_a_cycle():
    gh = FakeGh([])
    with _seeded(gh) as (c, db_url, orch):
        # dictation is a CHILD of sessions — merging sessions into its own
        # child would cycle once sessions' other children reparent onto it.
        r = _merge(c, from_slug="sessions", into_slug="dictation")
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["error"] == "parent_cycle"
        # The guard is a pure DB check before any GitHub call.
        assert gh.calls == []


def test_merge_repoints_work_items_and_initiatives_and_retires_the_source():
    gh = FakeGh(_merge_answers())  # dictation (the merge source here) has no
                                    # children in the seed fixture, so this is
                                    # a single ensure_branch/read_file/commit
                                    # sequence — the branch's first commit, so
                                    # it also opens the branch and the PR.
    with _seeded(gh) as (c, db_url, orch):
        _stub_pm_sync(orch, gh, {
            f"{DOCS}/sessions.md": ROOT_DOC, f"{DOCS}/dictation.md": CHILD_DOC})

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from server.db.models import Feature, InitiativeFeature, Initiative, Project, WorkItem

        engine = create_engine(db_url.replace("+aiosqlite", ""))
        Session = sessionmaker(bind=engine)
        with Session() as s:
            proj = s.query(Project).filter_by(slug=SLUG).one()
            dictation = s.query(Feature).filter_by(project_id=proj.id, slug="dictation").one()
            init = Initiative(slug="init-a", title="Init A")
            s.add(init)
            s.flush()
            s.add(InitiativeFeature(initiative_id=init.id, feature_id=dictation.id, is_primary=True))
            s.add(WorkItem(repo="o/r", number=1, kind="issue", feature_id=dictation.id))
            s.commit()
            init_id = init.id  # captured before the session closes below —
                                # `init` itself is detached after that, and a
                                # later access to `init.id` raises

        r = _merge(c, from_slug="dictation", into_slug="sessions")
        assert r.status_code == 200, r.text
        body = r.json()["pointer"]
        assert body["status"] == "open"

        with Session() as s:
            proj = s.query(Project).filter_by(slug=SLUG).one()
            dictation_row = s.query(Feature).filter_by(project_id=proj.id, slug="dictation").one()
            assert dictation_row.state == "sunset"
            wi = s.query(WorkItem).filter_by(repo="o/r", number=1, kind="issue").one()
            sessions_cap = s.query(Feature).filter_by(project_id=proj.id, slug="sessions").one()
            assert wi.feature_id == sessions_cap.id
            link = s.query(InitiativeFeature).filter_by(initiative_id=init_id).one()
            assert link.feature_id == sessions_cap.id and link.is_primary is True

        assert _audits(c, "pm.map_changes.merge") == [{
            "slug": SLUG, "from": "dictation", "into": "sessions",
            "workItems": 1, "initiatives": 1, "linksRepointed": 0, "linksNotRepointed": 0,
            "childrenReparented": 0, "commitSha": "c1", "prNumber": 12}]


def test_merge_dedupes_an_initiative_classified_onto_both_ends():
    """The trivial move-only test above only exercises the `InitiativeFeature`
    "no conflict, just move the row" branch (`else: row.feature_id = dst.id`)
    — hand-traced against `uq_initiative_feature` in `server/db/models.py` but
    never run: an initiative already classified onto BOTH `dictation`
    (is_primary) and `sessions` (not primary) must dedupe to ONE surviving row
    on `sessions`, with `is_primary` OR'd true, not a `uq_initiative_feature`
    violation."""
    gh = FakeGh(_merge_answers())
    with _seeded(gh) as (c, db_url, orch):
        _stub_pm_sync(orch, gh, {
            f"{DOCS}/sessions.md": ROOT_DOC, f"{DOCS}/dictation.md": CHILD_DOC})

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from server.db.models import Feature, Initiative, InitiativeFeature, Project

        engine = create_engine(db_url.replace("+aiosqlite", ""))
        Session = sessionmaker(bind=engine)
        with Session() as s:
            proj = s.query(Project).filter_by(slug=SLUG).one()
            dictation = s.query(Feature).filter_by(project_id=proj.id, slug="dictation").one()
            sessions_cap = s.query(Feature).filter_by(project_id=proj.id, slug="sessions").one()

            dual = Initiative(slug="init-dual", title="Init Dual")
            s.add(dual)
            s.flush()
            s.add(InitiativeFeature(
                initiative_id=dual.id, feature_id=dictation.id, is_primary=True))
            s.add(InitiativeFeature(
                initiative_id=dual.id, feature_id=sessions_cap.id, is_primary=False))
            s.commit()
            dual_id, sessions_id = dual.id, sessions_cap.id

        r = _merge(c, from_slug="dictation", into_slug="sessions")
        assert r.status_code == 200, r.text

        with Session() as s:
            rows = s.query(InitiativeFeature).filter_by(initiative_id=dual_id).all()
            assert len(rows) == 1
            assert rows[0].feature_id == sessions_id
            assert rows[0].is_primary is True

        assert _audits(c, "pm.map_changes.merge") == [{
            "slug": SLUG, "from": "dictation", "into": "sessions",
            "workItems": 0, "initiatives": 1, "linksRepointed": 0, "linksNotRepointed": 0,
            "childrenReparented": 0, "commitSha": "c1", "prNumber": 12}]


def test_merge_repoints_outgoing_links_and_drops_a_direct_link_to_the_target():
    """`FeatureLink` is entirely doc-derived: `_pm_sync_apply_feature_plan`
    (which `merge()`'s own `_resync_now` call triggers, inside the SAME
    request, before the response returns) DELETES AND REBUILDS every outgoing
    edge of every project Feature from doc frontmatter on every resync. A
    merge that only repointed the `FeatureLink` TABLE would have its repoint
    silently reverted by that very resync before the caller ever saw a
    response — this is a real bug a prior round of this task shipped and a
    deeper review caught. `merge()` now repoints via doc-level `op="links"`
    commits instead, which DO survive the resync (they're what the resync
    reads from).

    This goes through the REAL `_seeded`/HTTP path — not a bare service that
    skips `_resync_now` — specifically to prove the repoint survives it.

    Also covers the direct-link case: `dictation` links straight to
    `sessions` (the merge target) in addition to `third-cap` — repointing
    that naively would write a self-link (`sessions -> sessions`); it must be
    dropped instead."""
    gh = FakeGh(_merge_outgoing_link_answers())
    with _seeded(gh) as (c, db_url, orch):
        _stub_pm_sync(orch, gh, {
            f"{DOCS}/sessions.md": ROOT_DOC, f"{DOCS}/dictation.md": CHILD_DOC,
            f"{DOCS}/third-cap.md": THIRD_DOC})

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from server.db.models import Feature, FeatureLink, Project

        engine = create_engine(db_url.replace("+aiosqlite", ""))
        Session = sessionmaker(bind=engine)
        with Session() as s:
            proj = s.query(Project).filter_by(slug=SLUG).one()
            dictation = s.query(Feature).filter_by(project_id=proj.id, slug="dictation").one()
            sessions_cap = s.query(Feature).filter_by(project_id=proj.id, slug="sessions").one()
            third = Feature(project_id=proj.id, slug="third-cap", title="Third", state="live")
            s.add(third)
            s.flush()
            s.add(FeatureLink(src_feature_id=dictation.id, dst_feature_id=third.id,
                              kind="integrates_with"))
            s.add(FeatureLink(src_feature_id=dictation.id, dst_feature_id=sessions_cap.id,
                              kind="powered_by"))
            s.commit()
            dictation_id, sessions_id, third_id = dictation.id, sessions_cap.id, third.id

        r = _merge(c, from_slug="dictation", into_slug="sessions")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["pointer"]["status"] == "open"
        assert body["linksRepointed"] == 2
        assert body["linksNotRepointed"] == 0

        with Session() as s:
            # The repoint survived `_resync_now`, which already ran inside
            # this same request — the exact bug this test guards against.
            survivors = s.query(FeatureLink).filter_by(
                src_feature_id=sessions_id, dst_feature_id=third_id,
                kind="integrates_with").all()
            assert len(survivors) == 1
            assert s.query(FeatureLink).filter_by(src_feature_id=dictation_id).all() == []
            # Never turned into a self-link.
            assert s.query(FeatureLink).filter_by(
                src_feature_id=sessions_id, dst_feature_id=sessions_id).all() == []


def test_merge_repoints_a_same_project_incoming_link():
    """The incoming-link half of the same bug: `fourth-cap` (same project)
    already links TO `dictation` — after merging `dictation` into `sessions`,
    `fourth-cap` must link to `sessions` instead, and that repoint (a doc-level
    `op="links"` commit on `fourth-cap`'s own doc) must survive `_resync_now`
    the same way the outgoing case does. Real `_seeded`/HTTP path, not a bare
    service."""
    gh = FakeGh(_merge_incoming_link_answers())
    with _seeded(gh) as (c, db_url, orch):
        _stub_pm_sync(orch, gh, {
            f"{DOCS}/sessions.md": ROOT_DOC, f"{DOCS}/dictation.md": CHILD_DOC,
            f"{DOCS}/fourth-cap.md": FOURTH_DOC_LINKING_TO_DICTATION})

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from server.db.models import Feature, FeatureLink, Project

        engine = create_engine(db_url.replace("+aiosqlite", ""))
        Session = sessionmaker(bind=engine)
        with Session() as s:
            proj = s.query(Project).filter_by(slug=SLUG).one()
            dictation = s.query(Feature).filter_by(project_id=proj.id, slug="dictation").one()
            fourth = Feature(project_id=proj.id, slug="fourth-cap", title="Fourth", state="live")
            s.add(fourth)
            s.flush()
            s.add(FeatureLink(src_feature_id=fourth.id, dst_feature_id=dictation.id,
                              kind="integrates_with"))
            s.commit()
            fourth_id, dictation_id = fourth.id, dictation.id

        r = _merge(c, from_slug="dictation", into_slug="sessions")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["linksRepointed"] == 1
        assert body["linksNotRepointed"] == 0

        with Session() as s:
            proj = s.query(Project).filter_by(slug=SLUG).one()
            sessions_cap = s.query(Feature).filter_by(project_id=proj.id, slug="sessions").one()
            survivors = s.query(FeatureLink).filter_by(
                src_feature_id=fourth_id, dst_feature_id=sessions_cap.id,
                kind="integrates_with").all()
            assert len(survivors) == 1
            assert s.query(FeatureLink).filter_by(dst_feature_id=dictation_id).all() == []


def test_merge_drops_a_direct_incoming_link_from_the_target_instead_of_self_linking():
    """The incoming SELF-link case: `sessions` (the merge TARGET) already
    links DIRECTLY to `dictation` (the merge SOURCE) — repointing that ref
    naively would write `sessions -> sessions` into sessions' own doc. It
    must be dropped instead, and — same guard as the other two merge-link
    tests — that has to be verified through the real resync, not a bare
    service."""
    gh = FakeGh(_merge_incoming_self_link_answers())
    with _seeded(gh) as (c, db_url, orch):
        _stub_pm_sync(orch, gh, {
            f"{DOCS}/sessions.md": SESSIONS_DOC_LINKING_TO_DICTATION,
            f"{DOCS}/dictation.md": CHILD_DOC})

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from server.db.models import Feature, FeatureLink, Project

        engine = create_engine(db_url.replace("+aiosqlite", ""))
        Session = sessionmaker(bind=engine)
        with Session() as s:
            proj = s.query(Project).filter_by(slug=SLUG).one()
            dictation = s.query(Feature).filter_by(project_id=proj.id, slug="dictation").one()
            sessions_cap = s.query(Feature).filter_by(project_id=proj.id, slug="sessions").one()
            s.add(FeatureLink(src_feature_id=sessions_cap.id, dst_feature_id=dictation.id,
                              kind="powered_by"))
            s.commit()
            sessions_id = sessions_cap.id

        r = _merge(c, from_slug="dictation", into_slug="sessions")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["linksRepointed"] == 1
        assert body["linksNotRepointed"] == 0

        with Session() as s:
            # Dropped, not turned into a self-link.
            assert s.query(FeatureLink).filter_by(
                src_feature_id=sessions_id, dst_feature_id=sessions_id).all() == []
            assert s.query(FeatureLink).filter_by(kind="powered_by").all() == []


def test_merge_succeeds_despite_a_stale_link_on_the_target_doc_it_rewrites():
    """`merge()`'s link-commit call sites deliberately skip `validate_op`
    (see the comment above each commit in `map_changes.py`) because that
    call would re-validate EVERY ref in the full replacement list, including
    refs `merge()` never touched. `sessions` (the merge target here, whose
    doc gets rewritten to add `dictation`'s outgoing link to `third-cap`)
    already carries a STALE `integrates_with example-product/gone-cap` ref —
    a real, reachable state: `_pm_sync_apply_feature_plan` never cleans up a
    ref it can't resolve (its target may have been renamed or removed in
    another product), it just skips it with a warning. Before this fix, ANY
    merge that rewrote `sessions.md` would 422 with `bad_link: unknown link
    target example-product/gone-cap` — even though that stale ref has
    nothing to do with the merge being attempted. This asserts the merge now
    SUCCEEDS."""
    gh = FakeGh(_merge_stale_ref_answers())
    with _seeded(gh) as (c, db_url, orch):
        _stub_pm_sync(orch, gh, {
            f"{DOCS}/sessions.md": SESSIONS_DOC_WITH_STALE_REF, f"{DOCS}/dictation.md": CHILD_DOC,
            f"{DOCS}/third-cap.md": THIRD_DOC})

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from server.db.models import Feature, FeatureLink, Project

        engine = create_engine(db_url.replace("+aiosqlite", ""))
        Session = sessionmaker(bind=engine)
        with Session() as s:
            proj = s.query(Project).filter_by(slug=SLUG).one()
            dictation = s.query(Feature).filter_by(project_id=proj.id, slug="dictation").one()
            third = Feature(project_id=proj.id, slug="third-cap", title="Third", state="live")
            s.add(third)
            s.flush()
            s.add(FeatureLink(src_feature_id=dictation.id, dst_feature_id=third.id,
                              kind="integrates_with"))
            s.commit()

        r = _merge(c, from_slug="dictation", into_slug="sessions")
        # Previously would have 422'd with `bad_link: unknown link target
        # example-product/gone-cap` — a ref this merge never touched.
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["linksRepointed"] == 1


# -- retire clears pending triage suggestions ---------------------------------


def _retire_leaf_answers() -> list:
    """Retiring `dictation` (a leaf — no children to reparent first): the
    branch-doc probe, the base head, then ONE `_commit_and_track`, which is
    also the branch's first commit (so it creates the ref and opens the PR)."""
    return [_404, _ref("base1"),
            _404, _created("base1"), _contents(CHILD_DOC), _commit("c1"), _NO_PR, _pr()]


def _seed_triage_proposals(c) -> tuple[str, str, str]:
    """A pending and an accepted triage proposal, both pointing at
    ``dictation``, plus a pending one pointing at ``sessions`` (a control that
    retiring ``dictation`` must not touch). Returns their ids in that order."""
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import Feature, Project, TriageProposal, WorkItem

    async def _go():
        async with db_session() as db:
            proj = (await db.execute(select(Project).where(
                Project.slug == SLUG))).scalars().one()
            feats = {f.slug: f for f in (await db.execute(select(Feature).where(
                Feature.project_id == proj.id))).scalars().all()}
            wi = WorkItem(repo=REPO, number=1, kind="issue", title="An issue")
            db.add(wi)
            await db.flush()
            pending = TriageProposal(
                work_item_id=wi.id, proposed_feature_id=feats["dictation"].id,
                status="pending")
            accepted = TriageProposal(
                work_item_id=wi.id, proposed_feature_id=feats["dictation"].id,
                status="accepted")
            control = TriageProposal(
                work_item_id=wi.id, proposed_feature_id=feats["sessions"].id,
                status="pending")
            db.add_all([pending, accepted, control])
            await db.flush()
            ids = (pending.id, accepted.id, control.id)
            await db.commit()
            return ids

    return c.portal.call(_go)


def test_retire_clears_pending_triage_suggestions_for_the_capability():
    """A pending AI suggestion is not a dependent (it must not BLOCK the
    retire — a stale guess would hold a human's cleanup hostage), but it must
    not survive as a live suggestion either: accepting it later would classify
    a work item onto a sunset capability. So `retire()` NULLs
    `proposed_feature_id` on the pending ones and leaves resolved proposals —
    which are audit records of what was actually decided — untouched."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from server.db.models import TriageProposal

    gh = FakeGh(_retire_leaf_answers())
    with _seeded(gh) as (c, db_url, orch):
        pending_id, accepted_id, control_id = _seed_triage_proposals(c)
        _stub_pm_sync(orch, gh, {
            f"{DOCS}/sessions.md": ROOT_DOC, f"{DOCS}/dictation.md": CHILD_DOC})

        r = _retire(c, slug="dictation")
        assert r.status_code == 200, r.text

        engine = create_engine(db_url.replace("+aiosqlite", ""))
        Session = sessionmaker(bind=engine)
        with Session() as s:
            by_id = {p.id: p for p in s.query(TriageProposal).all()}
            # Cleared: a later accept now comes back as "no fit" rather than
            # classifying onto a sunset capability.
            assert by_id[pending_id].proposed_feature_id is None
            # Untouched: already decided, so it is history, not a suggestion.
            assert by_id[accepted_id].proposed_feature_id is not None
            # Untouched: a pending suggestion for a DIFFERENT capability.
            assert by_id[control_id].proposed_feature_id is not None

        assert _audits(c, "pm.map_changes.retire")[0]["proposalsCleared"] == 1


# -- triage inline capability creation, against the REAL MapChangesService ----


def _triage_product_answers() -> list:
    """One `create` on a product whose map-changes branch does not exist yet —
    the same shape as `_first_create_answers()`, spelled out here because the
    triage product has its own branch name."""
    return [_404, _ref("base1"), _404, _created("base1"), _404, _commit("c0"),
            _NO_PR, _pr()]


TRIAGE_SLUG = "triage-product"
TRIAGE_REPO = "o/triage"


def _seed_triage_product(c) -> str:
    """A SECOND product with its own repo (so repo→product ownership resolves
    uniquely — ``o/r`` is registered to two products in this module's seed),
    one indexed capability, and one pending proposal on a work item of that
    repo. Returns the proposal id."""
    from server.db import db_session
    from server.db.models import (
        Feature, Project, ProjectRepo, TriageProposal, WorkItem)

    async def _go():
        async with db_session() as db:
            p = Project(slug=TRIAGE_SLUG, name="Triage product",
                        onboarding_state="live")
            db.add(p)
            await db.flush()
            db.add(ProjectRepo(project_id=p.id, repo=TRIAGE_REPO, is_primary=True))
            db.add(Feature(project_id=p.id, slug="sessions", title="Sessions",
                           state="live"))
            wi = WorkItem(repo=TRIAGE_REPO, number=4, kind="issue",
                          title="Take money")
            db.add(wi)
            await db.flush()
            prop = TriageProposal(work_item_id=wi.id, proposed_feature_id=None,
                                  no_fit_suggestion="payments", status="pending",
                                  reach=3, impact=2, effort=1, confidence=3)
            db.add(prop)
            await db.flush()
            ids = (prop.id, wi.id)
            await db.commit()
            return ids

    return c.portal.call(_go)


def test_triage_accept_with_new_capability_end_to_end_through_the_real_service(
    monkeypatch,
):
    """The inline-create path driven through the REAL ``MapChangesService``
    and its REAL ``_resync_now``, not a fake whose resync is a bare
    ``db.add(Feature(...))``.

    That distinction is the whole point: a fake resync materializes a Feature
    row no matter how the service is wired, so every one of the existing
    ``newCapability`` tests would still pass against an orchestrator whose
    resync seams were never wired at all. This one reads the doc, plans the
    index and applies it for real, then asserts the resulting DB state."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from server.automations import target_identity
    from server.db.models import Feature, Project, TriageProposal, WorkItem
    from server.tests.pm_ownership_fixtures import identity_repositories

    monkeypatch.setattr(target_identity, "resolve_repositories", identity_repositories)

    gh = FakeGh(_triage_product_answers())
    with _seeded(gh) as (c, db_url, orch):
        proposal_id, work_item_id = _seed_triage_product(c)
        # The triage product's own docs — `payments.md` is NOT among them; the
        # route's `svc.edit()` commit is what puts it on the branch, and the
        # real `_resync_now` is what has to find it there.
        _stub_pm_sync(orch, gh, {f"{DOCS}/sessions.md": ROOT_DOC})

        r = c.post(f"/api/pm/triage/{proposal_id}/accept",
                   json={"edits": {"newCapability": {"title": "Payments",
                                                     "slug": "payments"}}},
                   headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        assert r.json()["triageState"] == "classified"

        engine = create_engine(db_url.replace("+aiosqlite", ""))
        Session = sessionmaker(bind=engine)
        with Session() as s:
            proj = s.query(Project).filter_by(slug=TRIAGE_SLUG).one()
            feat = s.query(Feature).filter_by(
                project_id=proj.id, slug="payments").one()
            assert feat.title == "Payments"
            # Indexed off the OPEN map-changes branch, so it carries the PR
            # columns rather than a default-branch commit_sha — proof the real
            # `_resync_now` (not a stub) produced this row.
            assert feat.pr_number == 12
            assert feat.commit_sha is None
            # …and the work item is classified onto that very row.
            wi = s.query(WorkItem).filter_by(id=work_item_id).one()
            assert wi.feature_id == feat.id
            assert s.query(TriageProposal).filter_by(
                id=proposal_id).one().status == "accepted"

        # The doc really was committed to the product's own map-changes branch.
        assert (f"{DOCS}/payments.md", ) == tuple(
            path for path, _text in _all_committed(gh))
