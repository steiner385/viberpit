"""A new product starts with the fleet's trusted authors (#3449).

Before this, a product created through onboarding had a null policy, so its
``trusted_authors`` was empty and every trust gate — PR Doctor first — refused all of
its PRs. The seed is the set of entries every configured product agrees on.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from server.tests._trust_ids import STEINER_ENTRY, STEINER_ID, TONY_ENTRY
from server.tests.test_pm_api import IDENTITY_HEADERS, _client
from server.tests.test_pm_api import canonical_mutation_repositories  # noqa: F401 — autouse


async def _products(*policies) -> None:
    from server.db import db_session
    from server.db.models import Project, ProjectRepo
    async with db_session() as db:
        for i, policy in enumerate(policies):
            p = Project(slug=f"p{i}", name=f"p{i}", automation_policy=policy)
            db.add(p)
            await db.flush()
            db.add(ProjectRepo(project_id=p.id, repo=f"o/r{i}", is_primary=True))


def _seed() -> list[str]:
    from server.automations.product_policy import fleet_default_trusted_authors
    from server.db import db_session

    async def run():
        async with db_session() as db:
            return await fleet_default_trusted_authors(db)
    return asyncio.run(run())


def test_no_configured_product_seeds_nothing(isolated_db):
    asyncio.run(_products(None, {"dispatch": "auto"}, {"trusted_authors": []}))
    assert _seed() == []


def test_seed_is_what_every_configured_product_trusts_in_first_order(isolated_db):
    asyncio.run(_products(
        {"trusted_authors": [STEINER_ENTRY, "app/viberpit", TONY_ENTRY]},
        None,  # never configured: no say in the seed
        {"trusted_authors": ["APP/viberpit", STEINER_ENTRY]},
        {"trusted_authors": []},  # empty: no say either
    ))
    # TONY is trusted on one product only — a per-product grant, not a fleet default.
    assert _seed() == [STEINER_ENTRY, "app/viberpit"]


def test_seed_carries_only_entries_that_grant_trust(isolated_db):
    # An unbound login trusts nobody, and a malformed entry is skipped by readers.
    asyncio.run(_products({"trusted_authors": ["tony", "github-actions", "app/viberpit"]}))
    assert _seed() == ["app/viberpit"]


def test_a_created_product_is_seeded_and_still_has_dispatch_off(monkeypatch):
    async def resolve(login: str) -> int:
        return STEINER_ID
    monkeypatch.setattr("server.pm.github_identity.resolve_github_user_id", resolve)
    with _client() as c:
        assert c.post("/api/pm/projects", json={"name": "First", "repos": [{"repo": "o/a"}]},
                      headers=IDENTITY_HEADERS).status_code == 201
        assert c.patch("/api/pm/projects/first/policy", json={"policy": {
            "trusted_authors": ["steiner385", "app/viberpit"]}},
            headers=IDENTITY_HEADERS).status_code == 200

        resp = c.post("/api/pm/projects", json={"name": "Keel", "repos": [{"repo": "o/keel"}]},
                      headers=IDENTITY_HEADERS)
        assert resp.status_code == 201, resp.text
        assert resp.json()["automationPolicy"] == {
            "trusted_authors": [STEINER_ENTRY, "app/viberpit"]}
        detail = c.get("/api/pm/projects/keel", headers=IDENTITY_HEADERS).json()
        assert detail["effectivePolicy"]["dispatch"] == "off"
        # The seed carries a revision an editor can write against.
        resp = c.patch("/api/pm/projects/keel/policy", json={
            "policy": {"skip_labels": ["on-hold"]},
            "expectedUpdatedAt": detail["automationPolicyUpdatedAt"]}, headers=IDENTITY_HEADERS)
        assert resp.status_code == 200, resp.text
        assert resp.json()["automationPolicy"]["trusted_authors"] == [STEINER_ENTRY,
                                                                      "app/viberpit"]


def test_the_first_product_starts_with_a_null_policy():
    with _client() as c:
        resp = c.post("/api/pm/projects", json={"name": "Only", "repos": [{"repo": "o/a"}]},
                      headers=IDENTITY_HEADERS)
        assert resp.status_code == 201 and resp.json()["automationPolicy"] is None


def test_trust_gap_names_the_product_and_where_to_edit(isolated_db):
    from server.db import db_session
    from server.db.models import Project
    from server.pm.dispatch_trust import trust_gap
    asyncio.run(_products({"trusted_authors": [TONY_ENTRY]}, None, {"trusted_authors": ["tony"]}))

    async def run():
        async with db_session() as db:
            for p in (await db.execute(select(Project))).scalars():
                p.onboarding_state = "registered" if p.slug == "p1" else "live"
            await db.flush()
            return [await trust_gap(db, r) for r in ("o/r0", "o/r1", "o/r2", "x/unowned")]
    listed, empty, unbound, unowned = asyncio.run(run())
    assert listed.text == "the author is not on product p0's trusted authors list"
    assert (listed.product, listed.edit_path) == ("p0", "/roadmap/p0?tab=automation")
    assert empty.text.startswith(
        "product p1 has no automation policy yet (still onboarding: registered), so no author")
    assert empty.edit_path == "/roadmap/p1?tab=automation"
    # Only an unbound login: it trusts no one, so it reads as an empty list.
    assert unbound.text.startswith("product p2 trusts no authors yet, so no author is trusted")
    assert unowned.text == "no single product owns x/unowned, so no author is trusted there"
    assert unowned.product is None and unowned.edit_path is None
