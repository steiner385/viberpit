"""Product repo lifecycle API: add, set primary, retire, un-retire
(docs/superpowers/specs/2026-11-18-product-repo-lifecycle-design.md § API)."""
from __future__ import annotations

from server.tests.test_pm_api import IDENTITY_HEADERS, _client, canonical_mutation_repositories  # noqa: F401

H = IDENTITY_HEADERS


def _create(c, name: str, *repos: str) -> dict:
    resp = c.post("/api/pm/projects", json={
        "name": name, "repos": [{"repo": r, "isPrimary": i == 0} for i, r in enumerate(repos)],
    }, headers=H)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _repos(body: dict) -> dict[str, dict]:
    return {r["repo"]: r for r in body["repos"]}


def _primary(body: dict) -> str | None:
    primaries = [r["repo"] for r in body["repos"] if r["isPrimary"]]
    assert len(primaries) <= 1, primaries
    return primaries[0] if primaries else None


def _audit_types(c) -> list[str]:
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import AuditEvent

    async def go():
        async with db_session() as db:
            return list((await db.execute(
                select(AuditEvent.type).where(AuditEvent.type.like("pm.project.repo.%"))
                .order_by(AuditEvent.at))).scalars().all())
    return c.portal.call(go)


def _retired_set(c) -> frozenset[str]:
    from server.db import db_session
    from server.pm.product_repos import retired_repo_set

    async def go():
        async with db_session() as db:
            return await retired_repo_set(db)
    return c.portal.call(go)


def test_add_set_primary_retire_unretire_happy_path():
    with _client() as c:
        _create(c, "Ghost", "o/ghost-mcp")

        resp = c.post("/api/pm/projects/ghost/repos", json={"repo": "o/ghost"}, headers=H)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert set(_repos(body)) == {"o/ghost-mcp", "o/ghost"}
        assert _primary(body) == "o/ghost-mcp"
        added = _repos(body)["o/ghost"]
        assert added["retiredAt"] is None and added["retiredReason"] is None
        assert added["supersededBy"] is None

        # path params match case-insensitively
        resp = c.post("/api/pm/projects/ghost/repos/O/Ghost/primary", json={}, headers=H)
        assert resp.status_code == 200, resp.text
        assert _primary(resp.json()) == "o/ghost"
        assert resp.json()["repos"][0]["repo"] == "o/ghost"  # primary listed first

        resp = c.post("/api/pm/projects/ghost/repos/o/ghost-mcp/retire", json={
            "reason": "archived", "supersededBy": "o/ghost"}, headers=H)
        assert resp.status_code == 200, resp.text
        old = _repos(resp.json())["o/ghost-mcp"]
        assert old["retiredAt"] is not None
        assert old["retiredReason"] == "archived"
        assert old["supersededBy"] == "o/ghost"
        assert old["isPrimary"] is False
        assert _primary(resp.json()) == "o/ghost"
        assert resp.json()["repos"][-1]["repo"] == "o/ghost-mcp"  # retired listed last
        assert "o/ghost-mcp" in _retired_set(c)

        resp = c.post("/api/pm/projects/ghost/repos/o/ghost-mcp/unretire", headers=H)
        assert resp.status_code == 200, resp.text
        old = _repos(resp.json())["o/ghost-mcp"]
        assert old["retiredAt"] is None and old["retiredReason"] is None
        assert old["supersededBy"] is None
        assert _primary(resp.json()) == "o/ghost"
        assert "o/ghost-mcp" not in _retired_set(c)

        # un-retiring an active repo is a 200 no-op (no audit row)
        resp = c.post("/api/pm/projects/ghost/repos/o/ghost-mcp/unretire", headers=H)
        assert resp.status_code == 200, resp.text

        assert _audit_types(c) == [
            "pm.project.repo.add", "pm.project.repo.primary",
            "pm.project.repo.retire", "pm.project.repo.unretire"]


def test_add_with_make_primary_demotes_the_old_primary():
    with _client() as c:
        _create(c, "Ghost", "o/ghost-mcp")
        resp = c.post("/api/pm/projects/ghost/repos",
                      json={"repo": "o/ghost", "makePrimary": True}, headers=H)
        assert resp.status_code == 201, resp.text
        assert _primary(resp.json()) == "o/ghost"
        assert _repos(resp.json())["o/ghost-mcp"]["isPrimary"] is False


def test_add_validates_and_refuses_unknown_products():
    with _client() as c:
        _create(c, "Ghost", "o/ghost")
        resp = c.post("/api/pm/projects/ghost/repos", json={"repo": "no-slash"}, headers=H)
        assert resp.status_code == 422
        assert resp.json()["detail"]["error"] == "invalid_repo"
        resp = c.post("/api/pm/projects/nope/repos", json={"repo": "o/x"}, headers=H)
        assert resp.status_code == 404


def test_add_repo_owned_by_another_product_is_an_ownership_conflict():
    """Review Focus 2: never a silent second owner."""
    with _client() as c:
        _create(c, "Ghost", "o/ghost")
        _create(c, "Other", "o/other")
        resp = c.post("/api/pm/projects/ghost/repos", json={"repo": "o/other"}, headers=H)
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["error"] == "ownership_conflict"
        assert set(_repos(c.get("/api/pm/projects/ghost", headers=H).json())) == {"o/ghost"}
        assert _audit_types(c) == []


def test_adding_an_attached_retired_repo_unretires_it_instead_of_duplicating():
    with _client() as c:
        _create(c, "Ghost", "o/a", "o/b")
        assert c.post("/api/pm/projects/ghost/repos/o/b/retire", json={},
                      headers=H).status_code == 200
        resp = c.post("/api/pm/projects/ghost/repos", json={"repo": "O/B", "makePrimary": True},
                      headers=H)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert [r["repo"] for r in body["repos"]].count("o/b") == 1
        assert _repos(body)["o/b"]["retiredAt"] is None
        assert _primary(body) == "o/b"


def test_retiring_the_primary_without_a_new_primary_is_refused_and_changes_nothing():
    """Review Focus 1."""
    with _client() as c:
        before = _create(c, "Ghost", "o/a", "o/b")
        resp = c.post("/api/pm/projects/ghost/repos/o/a/retire", json={"reason": "x"}, headers=H)
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["error"] == "new_primary_required"
        after = c.get("/api/pm/projects/ghost", headers=H).json()
        assert _repos(after)["o/a"]["retiredAt"] is None
        assert _primary(after) == _primary(before) == "o/a"
        assert _audit_types(c) == []

        # naming a repo that is not an active repo of this product is refused too
        for bad in ("o/zzz", "o/a"):
            resp = c.post("/api/pm/projects/ghost/repos/o/a/retire",
                          json={"newPrimary": bad}, headers=H)
            assert resp.status_code == 422, resp.text
            assert resp.json()["detail"]["error"] == "invalid_new_primary"

        resp = c.post("/api/pm/projects/ghost/repos/o/a/retire",
                      json={"newPrimary": "o/b", "reason": "moved"}, headers=H)
        assert resp.status_code == 200, resp.text
        assert _primary(resp.json()) == "o/b"
        assert _repos(resp.json())["o/a"]["retiredAt"] is not None


def test_retiring_the_last_active_repo_is_refused():
    with _client() as c:
        _create(c, "Ghost", "o/a", "o/b")
        assert c.post("/api/pm/projects/ghost/repos/o/b/retire", json={},
                      headers=H).status_code == 200
        resp = c.post("/api/pm/projects/ghost/repos/o/a/retire",
                      json={"newPrimary": "o/b"}, headers=H)
        assert resp.status_code == 422, resp.text
        # o/b is retired, so it cannot be the new primary; but the last-active rule wins
        assert resp.json()["detail"]["error"] == "last_active_repo"


def test_retired_repo_can_never_be_primary_and_unretire_restores_it():
    """Review Focus 5."""
    with _client() as c:
        _create(c, "Ghost", "o/a", "o/b")
        assert c.post("/api/pm/projects/ghost/repos/o/b/retire", json={},
                      headers=H).status_code == 200
        resp = c.post("/api/pm/projects/ghost/repos/o/b/primary", json={}, headers=H)
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["error"] == "repo_retired"
        resp = c.post("/api/pm/projects/ghost/repos",
                      json={"repo": "o/c"}, headers=H)
        assert resp.status_code == 201
        resp = c.post("/api/pm/projects/ghost/repos/o/a/retire",
                      json={"newPrimary": "o/b"}, headers=H)
        assert resp.status_code == 422
        assert resp.json()["detail"]["error"] == "invalid_new_primary"

        assert c.post("/api/pm/projects/ghost/repos/o/b/unretire",
                      headers=H).status_code == 200
        assert "o/b" not in _retired_set(c)
        resp = c.post("/api/pm/projects/ghost/repos/o/b/primary", json={}, headers=H)
        assert resp.status_code == 200, resp.text
        assert _primary(resp.json()) == "o/b"


def test_expected_primary_mismatch_is_a_conflict():
    with _client() as c:
        _create(c, "Ghost", "o/a", "o/b")
        resp = c.post("/api/pm/projects/ghost/repos/o/b/primary",
                      json={"expectedPrimary": "o/b"}, headers=H)
        assert resp.status_code == 409
        assert resp.json()["detail"]["error"] == "primary_changed"
        resp = c.post("/api/pm/projects/ghost/repos/o/a/retire",
                      json={"newPrimary": "o/b", "expectedPrimary": "o/b"}, headers=H)
        assert resp.status_code == 409
        assert resp.json()["detail"]["error"] == "primary_changed"
        resp = c.post("/api/pm/projects/ghost/repos/o/b/primary",
                      json={"expectedPrimary": "O/A"}, headers=H)
        assert resp.status_code == 200, resp.text


def test_unknown_repo_is_404():
    with _client() as c:
        _create(c, "Ghost", "o/a")
        for action in ("primary", "retire", "unretire"):
            resp = c.post(f"/api/pm/projects/ghost/repos/o/nope/{action}", json={}, headers=H)
            assert resp.status_code == 404, (action, resp.text)


def test_primary_change_is_refused_while_map_changes_is_open():
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import FeatureMapChange, Project

    with _client() as c:
        _create(c, "Ghost", "o/a", "o/b")

        async def _open_pointer():
            async with db_session() as db:
                p = (await db.execute(select(Project).where(
                    Project.slug == "ghost"))).scalars().one()
                db.add(FeatureMapChange(
                    project_id=p.id, repo="o/a", branch="pm/map-changes-ghost",
                    pr_number=1, pr_url="https://github.com/o/a/pull/1", status="open"))
                await db.commit()
        c.portal.call(_open_pointer)

        for path, payload in (("repos/o/b/primary", {}),
                              ("repos/o/a/retire", {"newPrimary": "o/b"}),
                              ("repos", {"repo": "o/c", "makePrimary": True})):
            resp = c.post(f"/api/pm/projects/ghost/{path}", json=payload, headers=H)
            assert resp.status_code == 409, (path, resp.text)
            assert resp.json()["detail"]["error"] == "open_map_changes"

        # changes that keep the primary are unaffected
        assert c.post("/api/pm/projects/ghost/repos/o/b/retire", json={},
                      headers=H).status_code == 200
        assert c.post("/api/pm/projects/ghost/repos", json={"repo": "o/c"},
                      headers=H).status_code == 201


def test_patch_repo_retains_old_repo_as_retired():
    with _client() as c:
        _create(c, "Ghost", "o/ghost-mcp")
        resp = c.patch("/api/pm/projects/ghost", json={"repo": "o/ghost"}, headers=H)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert _primary(body) == "o/ghost"
        assert body["repos"][0]["repo"] == "o/ghost"
        old = _repos(body)["o/ghost-mcp"]
        assert old["retiredAt"] is not None
        assert old["retiredReason"] == "repointed"
        assert old["supersededBy"] == "o/ghost"
        assert old["isPrimary"] is False

        # repointing back re-activates the retained row rather than adding a duplicate
        resp = c.patch("/api/pm/projects/ghost", json={"repo": "o/ghost-mcp"}, headers=H)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["repos"]) == 2
        assert _primary(body) == "o/ghost-mcp"
        assert _repos(body)["o/ghost-mcp"]["retiredAt"] is None
        assert _repos(body)["o/ghost"]["retiredReason"] == "repointed"
        assert "pm.project.repo.retire" in _audit_types(c)
