"""PM deps/drift API tests — GET /api/pm/projects/{slug}/deps.

Mirrors test_pm_api.py fixture pattern: _client() context manager, async ORM seeding
via db_session(), VIBERPIT_DEV=1 + empty allowlist auth bypass.
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

    # Remote-sessiond vars are cleared globally in conftest.py
    # (_no_ambient_remote_daemon_config) so create_app() stays in-process.
    with contextlib.ExitStack() as stack:
        url = db_url
        if url is None:
            tmp = stack.enter_context(tempfile.TemporaryDirectory())
            url = f"sqlite+aiosqlite:///{tmp}/test.db"
        with patch.dict(os.environ, {"VIBERPIT_DB_URL": url,
                                     "VIBERPIT_DEV": "1",
                                     "VIBERPIT_TAILNET_ALLOWLIST": ""}):
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            app = create_app(orchestrator=orch)
            with TestClient(app, raise_server_exceptions=False) as c:
                yield c


def test_deps_endpoint_returns_edges_and_drift(tmp_path):  # noqa: ARG001
    with _client() as c:
        from server.db import db_session
        from server.db.models import (
            GhProject, GhProjectItem, Project, ProjectRepo, WorkItem, WorkItemDep)

        async def _seed_async() -> None:
            async with db_session() as db:
                proj = Project(slug="viberpit", name="viberpit")
                db.add(proj)
                await db.flush()

                db.add(ProjectRepo(project_id=proj.id,
                                   repo="steiner385/viberpit",
                                   is_primary=True))

                wi945 = WorkItem(repo="steiner385/viberpit", number=945,
                                 kind="issue", title="i945", state="open")
                wi949 = WorkItem(repo="steiner385/viberpit", number=949,
                                 kind="issue", title="i949", state="open")
                wi950 = WorkItem(repo="steiner385/viberpit", number=950,
                                 kind="issue", title="i950", state="open")
                wi960 = WorkItem(repo="steiner385/viberpit", number=960,
                                 kind="issue", title="i960", state="open")
                # Cross-product mirrored target: a different onboarded product's
                # issue, mirrored (kind issue, state open) but NOT in ViberPit's repos.
                wi_kd12 = WorkItem(repo="steiner385/KinDash", number=12,
                                   kind="issue", title="kd12", state="open")
                db.add_all([wi945, wi949, wi950, wi960, wi_kd12])
                await db.flush()

                ghp = GhProject(project_id=proj.id, gh_project_number=7,
                                title="viberpit")
                db.add(ghp)
                await db.flush()

                # Only 950 on the board — 960 is open but NOT on the board
                db.add(GhProjectItem(gh_project_id=ghp.id, work_item_id=wi950.id))

                # native dep: 950 → KinDash#12 (cross-product but MIRRORED →
                # resolves to dstState "open", NOT unresolved)
                db.add(WorkItemDep(
                    src_ref="steiner385/viberpit#950",
                    dst_ref="steiner385/KinDash#12",
                    source="native",
                    active=True,
                ))
                # native dep: 950 → KinDash#99 (truly unmirrored → unresolvedTargets)
                db.add(WorkItemDep(
                    src_ref="steiner385/viberpit#950",
                    dst_ref="steiner385/KinDash#99",
                    source="native",
                    active=True,
                ))
                # prose dep: 950 → 949 (prose-only, no native counterpart)
                db.add(WorkItemDep(
                    src_ref="steiner385/viberpit#950",
                    dst_ref="steiner385/viberpit#949",
                    source="prose",
                    active=True,
                ))
                await db.commit()

        # Seed on the app's own loop: a bare asyncio.run shares the engine pool
        # across two loops and dies once startup saturates it (#1386).
        assert c.portal is not None
        c.portal.call(_seed_async)

        resp = c.get("/api/pm/projects/viberpit/deps", headers=IDENTITY_HEADERS)
        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert body["project"]["number"] == 7
        srcs = {e["src"] for e in body["edges"]}
        assert "steiner385/viberpit#950" in srcs

        assert body["drift"]["proseOnly"] == [
            {"src": "steiner385/viberpit#950",
             "dst": "steiner385/viberpit#949"}]

        edge_by_dst = {e["dst"]: e for e in body["edges"]}
        # Cross-product MIRRORED target resolves: dstState "open", not unresolved.
        assert edge_by_dst["steiner385/KinDash#12"]["dstState"] == "open"
        unresolved_dsts = {e["dst"] for e in body["drift"]["unresolvedTargets"]}
        assert "steiner385/KinDash#12" not in unresolved_dsts
        # Truly unmirrored target still IS unresolved.
        assert edge_by_dst["steiner385/KinDash#99"]["dstState"] is None
        assert "steiner385/KinDash#99" in unresolved_dsts

        assert "steiner385/viberpit#960" in body["drift"]["missingFromProject"]


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
                proj = Project(slug="viberpit", name="viberpit")
                db.add(proj)
                await db.flush()
                db.add(ProjectRepo(project_id=proj.id,
                                   repo="steiner385/viberpit",
                                   is_primary=True))
                wi950 = WorkItem(repo="steiner385/viberpit", number=950,
                                 kind="issue", title="i950", state="open")
                db.add(wi950)
                await db.flush()
                db.add(WorkItemDep(
                    src_ref="steiner385/viberpit#950",
                    dst_ref="steiner385/viberpit#949",
                    source="prose",
                    active=True,
                ))
                await db.commit()

        assert c.portal is not None
        c.portal.call(_seed_async)  # the app's own loop — see the note above

        resp = c.get("/api/pm/projects/viberpit/deps", headers=IDENTITY_HEADERS)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["project"] is None
        assert body["drift"]["missingFromProject"] == []


def _seed_filter_fixture(c, *, board: bool) -> None:
    """#101 -> #102 active native (both open); #103 -> #102 INACTIVE; closed #104 -> #102."""
    from server.db import db_session
    from server.db.models import (
        GhProject, Project, ProjectRepo, WorkItem, WorkItemDep)

    repo = "steiner385/viberpit"

    async def _seed_async() -> None:
        async with db_session() as db:
            proj = Project(slug="viberpit", name="viberpit")
            db.add(proj)
            await db.flush()
            db.add(ProjectRepo(project_id=proj.id, repo=repo, is_primary=True))
            for n, state in ((101, "open"), (102, "open"), (103, "open"), (104, "closed")):
                db.add(WorkItem(repo=repo, number=n, kind="issue", title=f"i{n}", state=state))
            if board:
                db.add(GhProject(project_id=proj.id, gh_project_number=7, title="viberpit"))
            for src, active in ((101, True), (103, False), (104, True)):
                db.add(WorkItemDep(src_ref=f"{repo}#{src}", dst_ref=f"{repo}#102",
                                   source="native", active=active))
            await db.commit()

    assert c.portal is not None
    c.portal.call(_seed_async)


def test_deps_endpoint_filters_and_summarises():
    with _client() as c:
        _seed_filter_fixture(c, board=True)
        full = c.get("/api/pm/projects/viberpit/deps", headers=IDENTITY_HEADERS).json()
        assert len(full["edges"]) == 3, "defaults preserve today's response"

        active = c.get("/api/pm/projects/viberpit/deps?active=true&open_only=true",
                       headers=IDENTITY_HEADERS).json()
        assert [e["src"] for e in active["edges"]] == ["steiner385/viberpit#101"]

        s = c.get("/api/pm/projects/viberpit/deps?summary=true", headers=IDENTITY_HEADERS).json()
        assert s["edges"] == {"total": 3, "active": 2, "bySource": {"native": 3}}
        assert set(s["drift"]) == {"proseOnly", "unresolvedTargets", "missingFromProject",
                                   "checklistEpics"}
        assert all(isinstance(v, int) for v in s["drift"].values())
        assert s["topBlockers"] == [{"ref": "steiner385/viberpit#102", "blocks": 1,
                                     "state": "open"}]
        assert "project" in s


def test_deps_summary_for_a_product_without_a_board_answers_null_project():
    with _client() as c:
        _seed_filter_fixture(c, board=False)
        s = c.get("/api/pm/projects/viberpit/deps?summary=true", headers=IDENTITY_HEADERS).json()
        assert s["project"] is None and s["drift"]["missingFromProject"] == 0


def test_deps_summary_names_only_blockers_that_still_block():
    """#2979: topBlockers listed CLOSED issues as blocking (KinDash #18028 "blocks 7",
    state closed), and a dep on a merged PR read as an unresolved target. A
    resolved blocker drops out of topBlockers; a PR target resolves to its state."""
    from server.db import db_session
    from server.db.models import Project, ProjectRepo, WorkItem, WorkItemDep

    repo = "steiner385/viberpit"

    async def _seed_async() -> None:
        async with db_session() as db:
            proj = Project(slug="viberpit", name="viberpit")
            db.add(proj)
            await db.flush()
            db.add(ProjectRepo(project_id=proj.id, repo=repo, is_primary=True))
            for n, state in ((1, "open"), (2, "open"), (3, "open"), (10, "closed"),
                             (11, "open")):
                db.add(WorkItem(repo=repo, number=n, kind="issue", title=f"i{n}", state=state))
            db.add(WorkItem(repo=repo, number=12, kind="pr", title="pr12", state="merged"))
            for src, dst, source in ((1, 10, "native"), (2, 10, "prose"), (3, 11, "native"),
                                     (3, 12, "prose")):
                db.add(WorkItemDep(src_ref=f"{repo}#{src}", dst_ref=f"{repo}#{dst}",
                                   source=source, active=True))
            await db.commit()

    with _client() as c:
        assert c.portal is not None
        c.portal.call(_seed_async)
        s = c.get("/api/pm/projects/viberpit/deps?summary=true", headers=IDENTITY_HEADERS).json()
        assert s["topBlockers"] == [{"ref": f"{repo}#11", "blocks": 1, "state": "open"}]
        assert s["drift"]["unresolvedTargets"] == 0
        full = c.get("/api/pm/projects/viberpit/deps", headers=IDENTITY_HEADERS).json()
        assert {e["dst"]: e["dstState"] for e in full["edges"]}[f"{repo}#12"] == "merged"


def _seed_stale_fixture(c) -> dict[str, str]:
    """viberpit product, open issues 1-6 + closed 10, merged PR 12:

    - #1 ← #10 native, blocker closed            -> native_resolved (prunable)
    - #2 ← #12 prose ("Depends on #12"), PR merged -> derived_resolved (listed, refused)
    - #3 ← #4 prose, text gone from #3's body     -> prose_vanished (prunable)
    - #5 ← #6 native, blocker open                -> not stale
    Returns {ref: work_item_id}."""
    from server.db import db_session
    from server.db.models import Project, ProjectRepo, WorkItem, WorkItemDep

    repo = "steiner385/viberpit"
    bodies = {2: "Depends on #12", 3: "Nothing to see here"}

    async def _seed_async() -> dict[str, str]:
        async with db_session() as db:
            proj = Project(slug="viberpit", name="viberpit")
            db.add(proj)
            await db.flush()
            db.add(ProjectRepo(project_id=proj.id, repo=repo, is_primary=True))
            ids: dict[str, str] = {}
            for n, state in ((1, "open"), (2, "open"), (3, "open"), (4, "open"), (5, "open"),
                             (6, "open"), (10, "closed")):
                w = WorkItem(repo=repo, number=n, kind="issue", title=f"i{n}", state=state,
                             body=bodies.get(n, ""), gh_node_id=f"N{n}")
                db.add(w)
                await db.flush()
                ids[f"{repo}#{n}"] = w.id
            db.add(WorkItem(repo=repo, number=12, kind="pr", title="pr12", state="merged"))
            for src, dst, source in ((1, 10, "native"), (2, 12, "prose"), (3, 4, "prose"),
                                     (5, 6, "native")):
                db.add(WorkItemDep(src_ref=f"{repo}#{src}", dst_ref=f"{repo}#{dst}",
                                   source=source, active=True))
            await db.commit()
            return ids

    assert c.portal is not None
    return c.portal.call(_seed_async)


def _active_pairs(c) -> set[tuple[str, str]]:
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import WorkItemDep

    async def _read():
        async with db_session() as db:
            return {(d.src_ref, d.dst_ref) for d in (await db.execute(
                select(WorkItemDep).where(WorkItemDep.active.is_(True)))).scalars().all()}
    return c.portal.call(_read)


def test_stale_deps_lists_resolved_and_vanished_edges_only():
    r = "steiner385/viberpit"
    with _client() as c:
        _seed_stale_fixture(c)
        body = c.get("/api/pm/projects/viberpit/deps/stale", headers=IDENTITY_HEADERS).json()
        got = {(e["src"], e["blocker"]): (e["kind"], e["prunable"], e["blockerState"])
               for e in body["edges"]}
        assert got == {
            (f"{r}#1", f"{r}#10"): ("native_resolved", True, "closed"),
            (f"{r}#2", f"{r}#12"): ("derived_resolved", False, "merged"),
            (f"{r}#3", f"{r}#4"): ("prose_vanished", True, "open"),
        }
        assert body["counts"] == {"native_resolved": 1, "prose_vanished": 1,
                                  "derived_resolved": 1}
        assert body["prunable"] == 2
        assert c.get("/api/pm/projects/nope/deps/stale",
                     headers=IDENTITY_HEADERS).status_code == 404


def test_prune_removes_native_via_seam_deactivates_prose_and_refuses_the_rest():
    r = "steiner385/viberpit"
    with _client() as c:
        ids = _seed_stale_fixture(c)
        calls: list[tuple[str, str]] = []

        async def fake(work_item_id: str, blocker_ref: str) -> dict:
            calls.append((work_item_id, blocker_ref))
            return {"ok": True, "src": f"{r}#1", "blocker": blocker_ref}
        c.app.state.sessions.pm_remove_dependency = fake
        resp = c.post("/api/pm/projects/viberpit/deps/prune", headers=IDENTITY_HEADERS, json={
            "edges": [{"src": f"{r}#1", "blocker": f"{r}#10"},
                      {"src": f"{r}#3", "blocker": f"{r}#4"},
                      {"src": f"{r}#2", "blocker": f"{r}#12"},
                      {"src": f"{r}#5", "blocker": f"{r}#6"}]})
        assert resp.status_code == 200, resp.text
        out = resp.json()
        assert [(x["pruned"], x["refused"]) for x in out["results"]] == [
            ("native", None), ("prose", None), (None, "rederived"), (None, "not_stale")]
        assert out["pruned"] == 2 and out["refused"] == 2
        assert calls == [(ids[f"{r}#1"], f"{r}#10")]
        active = _active_pairs(c)
        assert (f"{r}#3", f"{r}#4") not in active  # deactivated locally
        assert (f"{r}#5", f"{r}#6") in active and (f"{r}#2", f"{r}#12") in active
        audits = _audits(c, "pm.workitem.dep_prune")
        assert [(a["src"], a["ok"], a["refused"]) for a in audits] == [
            (f"{r}#1", True, None), (f"{r}#3", True, None),
            (f"{r}#2", False, "rederived"), (f"{r}#5", False, "not_stale")]


def test_prune_revalidates_an_edge_that_stopped_being_stale():
    """The preview said #1 ← #10 was stale; #10 reopened since. Prune refuses it and
    never touches GitHub."""
    from sqlalchemy import update

    from server.db import db_session
    from server.db.models import WorkItem

    r = "steiner385/viberpit"
    with _client() as c:
        _seed_stale_fixture(c)
        assert c.get("/api/pm/projects/viberpit/deps/stale",
                     headers=IDENTITY_HEADERS).json()["prunable"] == 2

        async def _reopen():
            async with db_session() as db:
                await db.execute(update(WorkItem).where(WorkItem.number == 10)
                                 .values(state="open"))
                await db.commit()
        c.portal.call(_reopen)

        async def boom(*_a) -> dict:
            raise AssertionError("must not reach GitHub for a no-longer-stale edge")
        c.app.state.sessions.pm_remove_dependency = boom
        out = c.post("/api/pm/projects/viberpit/deps/prune", headers=IDENTITY_HEADERS,
                     json={"edges": [{"src": f"{r}#1", "blocker": f"{r}#10"}]}).json()
        assert out["results"][0]["refused"] == "not_stale"
        assert (f"{r}#1", f"{r}#10") in _active_pairs(c)


def test_prune_reports_a_github_refusal_and_caps_the_batch():
    r = "steiner385/viberpit"
    with _client() as c:
        _seed_stale_fixture(c)

        async def refuse(*_a) -> dict:
            return {"ok": False, "step": "mutate", "error": "no such edge"}
        c.app.state.sessions.pm_remove_dependency = refuse
        out = c.post("/api/pm/projects/viberpit/deps/prune", headers=IDENTITY_HEADERS,
                     json={"edges": [{"src": f"{r}#1", "blocker": f"{r}#10"}]}).json()
        assert out["results"][0] == {"src": f"{r}#1", "blocker": f"{r}#10", "pruned": None,
                                     "refused": "github", "message": "no such edge"}
        assert _audits(c, "pm.workitem.dep_prune")[0]["step"] == "mutate"
        too_many = [{"src": f"{r}#1", "blocker": f"{r}#{n}"} for n in range(21)]
        assert c.post("/api/pm/projects/viberpit/deps/prune", headers=IDENTITY_HEADERS,
                      json={"edges": too_many}).status_code == 422
        assert c.post("/api/pm/projects/viberpit/deps/prune", headers=IDENTITY_HEADERS,
                      json={"edges": []}).status_code == 422
        assert c.post("/api/pm/projects/nope/deps/prune", headers=IDENTITY_HEADERS,
                      json={"edges": [{"src": f"{r}#1", "blocker": f"{r}#10"}]}
                      ).status_code == 404
