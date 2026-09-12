"""PM deps/drift API tests — GET /api/pm/projects/{slug}/deps.

Mirrors test_pm_api.py fixture pattern: _client() context manager, async ORM seeding
via db_session(), CCSWITCH_DEV=1 + empty allowlist auth bypass.
"""
from __future__ import annotations

import contextlib
import os
import tempfile
from contextlib import contextmanager
from unittest.mock import patch

IDENTITY_HEADERS = {"Tailscale-User-Login": "testuser@example.com"}


@contextmanager
def _client(db_url: str | None = None):
    """Same idiom as ``test_sdlc_api.py::_client`` / ``test_pm_map_api.py::_client``:
    accept a caller-prepared ``db_url`` so seeding can happen BEFORE the client (and
    its own event loop) is opened, or build its own temp dir when none is given."""
    from fastapi.testclient import TestClient
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    _clear = [
        "CCSWITCH_SESSIOND_ENDPOINT",
        "CCSWITCH_SESSIOND_SOCKET",
        "CCSWITCH_SESSIOND_HOST",
        "CCSWITCH_SESSIOND_PORT",
        "CCSWITCH_SESSIOND_CLIENT_CERT",
        "CCSWITCH_SESSIOND_CLIENT_KEY",
        "CCSWITCH_SESSIOND_CA",
    ]
    saved = {k: os.environ.pop(k, None) for k in _clear}
    try:
        with contextlib.ExitStack() as stack:
            url = db_url
            if url is None:
                tmp = stack.enter_context(tempfile.TemporaryDirectory())
                url = f"sqlite+aiosqlite:///{tmp}/test.db"
            with patch.dict(os.environ, {"CCSWITCH_DB_URL": url,
                                         "CCSWITCH_DEV": "1",
                                         "CCSWITCH_TAILNET_ALLOWLIST": ""}):
                orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
                app = create_app(orchestrator=orch)
                with TestClient(app, raise_server_exceptions=False) as c:
                    yield c
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def test_deps_endpoint_returns_edges_and_drift(tmp_path):  # noqa: ARG001
    with _client() as c:
        from server.db import db_session
        from server.db.models import (
            GhProject, GhProjectItem, Project, ProjectRepo, WorkItem, WorkItemDep)

        async def _seed_async() -> None:
            async with db_session() as db:
                proj = Project(slug="ccswitch", name="ccswitch")
                db.add(proj)
                await db.flush()

                db.add(ProjectRepo(project_id=proj.id,
                                   repo="steiner385/cc-account-switcher",
                                   is_primary=True))

                wi945 = WorkItem(repo="steiner385/cc-account-switcher", number=945,
                                 kind="issue", title="i945", state="open")
                wi949 = WorkItem(repo="steiner385/cc-account-switcher", number=949,
                                 kind="issue", title="i949", state="open")
                wi950 = WorkItem(repo="steiner385/cc-account-switcher", number=950,
                                 kind="issue", title="i950", state="open")
                wi960 = WorkItem(repo="steiner385/cc-account-switcher", number=960,
                                 kind="issue", title="i960", state="open")
                # Cross-product mirrored target: a different onboarded product's
                # issue, mirrored (kind issue, state open) but NOT in ccswitch's repos.
                wi_kd12 = WorkItem(repo="steiner385/KinDash", number=12,
                                   kind="issue", title="kd12", state="open")
                db.add_all([wi945, wi949, wi950, wi960, wi_kd12])
                await db.flush()

                ghp = GhProject(project_id=proj.id, gh_project_number=7,
                                title="cc-account-switcher")
                db.add(ghp)
                await db.flush()

                # Only 950 on the board — 960 is open but NOT on the board
                db.add(GhProjectItem(gh_project_id=ghp.id, work_item_id=wi950.id))

                # native dep: 950 → KinDash#12 (cross-product but MIRRORED →
                # resolves to dstState "open", NOT unresolved)
                db.add(WorkItemDep(
                    src_ref="steiner385/cc-account-switcher#950",
                    dst_ref="steiner385/KinDash#12",
                    source="native",
                    active=True,
                ))
                # native dep: 950 → KinDash#99 (truly unmirrored → unresolvedTargets)
                db.add(WorkItemDep(
                    src_ref="steiner385/cc-account-switcher#950",
                    dst_ref="steiner385/KinDash#99",
                    source="native",
                    active=True,
                ))
                # prose dep: 950 → 949 (prose-only, no native counterpart)
                db.add(WorkItemDep(
                    src_ref="steiner385/cc-account-switcher#950",
                    dst_ref="steiner385/cc-account-switcher#949",
                    source="prose",
                    active=True,
                ))
                await db.commit()

        # Seed on the app's own loop: a bare asyncio.run shares the engine pool
        # across two loops and dies once startup saturates it (#1386).
        assert c.portal is not None
        c.portal.call(_seed_async)

        resp = c.get("/api/pm/projects/ccswitch/deps", headers=IDENTITY_HEADERS)
        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert body["project"]["number"] == 7
        srcs = {e["src"] for e in body["edges"]}
        assert "steiner385/cc-account-switcher#950" in srcs

        assert body["drift"]["proseOnly"] == [
            {"src": "steiner385/cc-account-switcher#950",
             "dst": "steiner385/cc-account-switcher#949"}]

        edge_by_dst = {e["dst"]: e for e in body["edges"]}
        # Cross-product MIRRORED target resolves: dstState "open", not unresolved.
        assert edge_by_dst["steiner385/KinDash#12"]["dstState"] == "open"
        unresolved_dsts = {e["dst"] for e in body["drift"]["unresolvedTargets"]}
        assert "steiner385/KinDash#12" not in unresolved_dsts
        # Truly unmirrored target still IS unresolved.
        assert edge_by_dst["steiner385/KinDash#99"]["dstState"] is None
        assert "steiner385/KinDash#99" in unresolved_dsts

        assert "steiner385/cc-account-switcher#960" in body["drift"]["missingFromProject"]


def test_deps_endpoint_404_on_unknown_slug():
    with _client() as c:
        assert c.get(
            "/api/pm/projects/nope/deps", headers=IDENTITY_HEADERS
        ).status_code == 404


def _audits(c, type_: str) -> list[dict]:
    """Every audit row of *type_*, oldest first. No GET /api/audit route exists, so
    this reads the DB through the client's own loop — same idiom as
    ``test_sdlc_api.py::_audits``."""
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import AuditEvent

    async def _read():
        async with db_session() as db:
            return [dict(r.detail or {}) for r in (await db.execute(
                select(AuditEvent).where(AuditEvent.type == type_)
                .order_by(AuditEvent.at))).scalars().all()]

    return c.portal.call(_read)


def _seed_blocked_pair(db_url: str) -> str:
    """#5 blocked by #3, native; returns #5's work item id."""
    import asyncio

    from server.db import db_session, init_db
    from server.db.models import WorkItem, WorkItemDep

    async def _go() -> str:
        await init_db(db_url)
        async with db_session() as db:
            src = WorkItem(repo="o/r", number=5, kind="issue", state="open", gh_node_id="N5")
            dst = WorkItem(repo="o/r", number=3, kind="issue", state="open", gh_node_id="N3")
            db.add_all([src, dst])
            await db.flush()
            db.add(WorkItemDep(src_ref="o/r#5", dst_ref="o/r#3", source="native", active=True,
                               src_work_item_id=src.id, dst_work_item_id=dst.id))
            await db.commit()
            return src.id
    return asyncio.run(_go())


def test_delete_dep_calls_the_seam_and_audits():
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        wid = _seed_blocked_pair(db_url)
        with _client(db_url) as c:
            calls: list[tuple[str, str]] = []

            async def fake(work_item_id: str, blocker_ref: str) -> dict:
                calls.append((work_item_id, blocker_ref))
                return {"ok": True, "src": "o/r#5", "blocker": "o/r#3"}
            c.app.state.sessions.pm_remove_dependency = fake
            r = c.delete(f"/api/pm/work-items/{wid}/deps?blocker=o/r%233", headers=IDENTITY_HEADERS)
            assert r.status_code == 200, r.text
            assert r.json() == {"ok": True, "src": "o/r#5", "blocker": "o/r#3"}
            assert calls == [(wid, "o/r#3")]
            assert _audits(c, "pm.workitem.dep_remove") == [
                {"workItemId": wid, "blocker": "o/r#3", "ok": True, "step": None}]


def test_delete_dep_refusals():
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        wid = _seed_blocked_pair(db_url)
        with _client(db_url) as c:
            async def refuse(work_item_id: str, blocker_ref: str) -> dict:
                return {"ok": False, "step": "mutate", "error": "no such edge"}
            c.app.state.sessions.pm_remove_dependency = refuse
            assert c.delete("/api/pm/work-items/nope/deps?blocker=o/r%233",
                            headers=IDENTITY_HEADERS).status_code == 404
            r = c.delete(f"/api/pm/work-items/{wid}/deps?blocker=o/r%239",
                         headers=IDENTITY_HEADERS)
            assert r.status_code == 404 and r.json()["detail"]["error"] == "dep_not_found"
            r = c.delete(f"/api/pm/work-items/{wid}/deps?blocker=garbage", headers=IDENTITY_HEADERS)
            assert r.status_code == 422 and r.json()["detail"]["error"] == "bad_blocker"
            r = c.delete(f"/api/pm/work-items/{wid}/deps?blocker=o/r%233", headers=IDENTITY_HEADERS)
            assert r.status_code == 502, r.text
            assert r.json()["detail"] == {"error": "dep_remove_failed", "step": "mutate",
                                          "message": "no such edge"}
            # The failed attempt is still in the trail (committed before the raise).
            assert [a["ok"] for a in _audits(c, "pm.workitem.dep_remove")] == [False]
            assert c.delete(f"/api/pm/work-items/{wid}/deps?blocker=o/r%233").status_code == 401


def test_deps_endpoint_null_project_when_no_gh_project():
    """When no GhProject is linked, project is null and missingFromProject is []."""
    with _client() as c:
        from server.db import db_session
        from server.db.models import Project, ProjectRepo, WorkItem, WorkItemDep

        async def _seed_async() -> None:
            async with db_session() as db:
                proj = Project(slug="ccswitch", name="ccswitch")
                db.add(proj)
                await db.flush()
                db.add(ProjectRepo(project_id=proj.id,
                                   repo="steiner385/cc-account-switcher",
                                   is_primary=True))
                wi950 = WorkItem(repo="steiner385/cc-account-switcher", number=950,
                                 kind="issue", title="i950", state="open")
                db.add(wi950)
                await db.flush()
                db.add(WorkItemDep(
                    src_ref="steiner385/cc-account-switcher#950",
                    dst_ref="steiner385/cc-account-switcher#949",
                    source="prose",
                    active=True,
                ))
                await db.commit()

        assert c.portal is not None
        c.portal.call(_seed_async)  # the app's own loop — see the note above

        resp = c.get("/api/pm/projects/ccswitch/deps", headers=IDENTITY_HEADERS)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["project"] is None
        assert body["drift"]["missingFromProject"] == []
