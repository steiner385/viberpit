"""PmSyncLoop Projects v2 + relations passes (003 Phase A).

The mirror pass is driven by new optional seams on ``PmSyncLoop`` (all default
to ``None`` / no-ops so every pre-existing ``test_pm_sync_loop.py`` test still
passes unmodified). Discovery is OWNER-AWARE: under the GitHub App token
``viewer`` resolves to the App bot, so project discovery iterates the distinct
owners of the products' repos and roots the query at
``repositoryOwner(login: owner)`` (resolves both Users and Organizations),
riding a dedicated ``fetch_gh_projects(owner, query)`` seam. Relations queries
are repo-scoped and keep using the plain repo-scoped ``fetch`` seam.
"""
from datetime import datetime, timedelta, timezone

import pytest

from server.automations.pm_sync import PmSyncLoop
from server.pm.gh_projects import (
    GhProjectItemView, GhProjectView, IssueRelView)
from server.pm.mirror_store import ProductSyncView


class Ctx:
    def __init__(self):
        self.events = []
        self.pm_sync_counts = {}

    async def emit(self, level, text, **kw):
        self.events.append((level, text, kw))


# ---- canned GraphQL payloads (repositoryOwner-rooted discovery) -------------


def _owner_projects_payload(title="cc-account-switcher"):
    return {"data": {"repositoryOwner": {"projectsV2": {
        "pageInfo": {"endCursor": None, "hasNextPage": False},
        "nodes": [
            {"id": "PVT_1", "number": 7, "title": title, "closed": False,
             "updatedAt": "2026-09-01T10:00:00Z"},
        ]}}}}


def _fields_payload():
    return {"data": {"node": {"fields": {"nodes": [
        {"id": "F_status", "name": "Status",
         "options": [{"id": "O_ready", "name": "Ready"}]},
        {"id": "F_score", "name": "Score"},
    ]}}}}


def _items_payload():
    return {"data": {"node": {"items": {
        "pageInfo": {"endCursor": None, "hasNextPage": False},
        "nodes": [
            {"id": "PVTI_1", "updatedAt": "2026-09-02T09:00:00Z",
             "content": {"__typename": "Issue", "number": 950,
                         "repository": {"nameWithOwner": "steiner385/cc-account-switcher"}},
             "fieldValues": {"nodes": [
                 {"name": "Ready", "field": {"name": "Status"}},
             ]}},
        ]}}}}


def _relations_payload():
    return {"data": {"repository": {"issues": {
        "pageInfo": {"endCursor": None, "hasNextPage": False},
        "nodes": [
            {"number": 950,
             "blockedBy": {"nodes": []},
             "parent": None,
             "subIssues": {"nodes": []}},
        ]}}}}


# ---- loop wiring (mirrors test_pm_sync_loop._loop, plus the new seams) --------


def _loop(ctx, *, products, owner_fetch, repo_fetch, now_fn=None, config=None):
    """Wire a loop with no repos to sync (so tick() goes straight to the mirror
    pass) and canned mirror seams. Every seam call is recorded in ``calls``."""
    calls = {"save_gh_project": [], "save_project_fields": [],
             "save_project_items": [], "save_relations": []}

    async def read_config():
        return "on", (config or {})

    async def list_repos():
        return []

    async def fetch(query):
        # repo-scoped seam (ambient credential): relations only.
        return repo_fetch(query)

    async def upsert(items):
        pass

    async def save(view, **updates):
        pass

    async def read_docs(repo):
        return []

    async def list_rows(project_id):
        return []

    async def apply_plan(project_id, plan, docs_by_slug):
        pass

    async def list_products():
        return list(products)

    async def fetch_gh_projects(owner, query):
        return owner_fetch(owner, query)

    async def save_gh_project(project_id, view):
        calls["save_gh_project"].append((project_id, view))
        return "ghdb_1"

    async def save_project_fields(gh_db_id, cache):
        calls["save_project_fields"].append((gh_db_id, cache))

    async def save_project_items(gh_db_id, items):
        calls["save_project_items"].append((gh_db_id, list(items)))

    async def save_relations(repo, rels):
        calls["save_relations"].append((repo, list(rels)))

    loop = PmSyncLoop(
        ctx, read_config=read_config, list_repos=list_repos, fetch=fetch,
        upsert_items=upsert, save_sync_state=save, read_feature_docs=read_docs,
        list_feature_rows=list_rows, apply_feature_plan=apply_plan,
        now_utc=now_fn or (lambda: datetime(2026, 2, 1, tzinfo=timezone.utc)),
        list_products=list_products, fetch_gh_projects=fetch_gh_projects,
        save_gh_project=save_gh_project, save_project_fields=save_project_fields,
        save_project_items=save_project_items, save_relations=save_relations)
    return loop, calls


def _product(**over):
    base = dict(project_id="p1", slug="ccswitch", name="cc-account-switcher",
                gh_project_db_id=None, gh_node_id=None,
                repos=["steiner385/cc-account-switcher"])
    base.update(over)
    return ProductSyncView(**base)


@pytest.mark.asyncio
async def test_projects_pass_discovers_by_title_and_saves_items():
    ctx = Ctx()

    def owner_fetch(owner, query):
        if "projectsV2" in query:
            return _owner_projects_payload()
        if "fields" in query:
            return _fields_payload()
        if "items" in query:
            return _items_payload()
        raise AssertionError(f"unexpected owner query: {query[:60]}")

    def repo_fetch(query):
        return _relations_payload()

    loop, calls = _loop(ctx, products=[_product()],
                        owner_fetch=owner_fetch, repo_fetch=repo_fetch)
    await loop.tick()

    # Discovery matched the project title against the product name/slug and
    # persisted it with the product's project_id.
    assert len(calls["save_gh_project"]) == 1
    project_id, view = calls["save_gh_project"][0]
    assert project_id == "p1"
    assert isinstance(view, GhProjectView) and view.node_id == "PVT_1"

    # Fields cache persisted against the returned db id.
    assert len(calls["save_project_fields"]) == 1
    gh_db_id, cache = calls["save_project_fields"][0]
    assert gh_db_id == "ghdb_1"
    assert cache["Status"]["field_id"] == "F_status"

    # Items persisted against the db id: one issue #950 with status "Ready".
    assert len(calls["save_project_items"]) == 1
    gh_db_id, items = calls["save_project_items"][0]
    assert gh_db_id == "ghdb_1"
    assert len(items) == 1
    item = items[0]
    assert isinstance(item, GhProjectItemView)
    assert item.number == 950 and item.status == "Ready"


@pytest.mark.asyncio
async def test_relations_pass_runs_per_repo_and_saves():
    ctx = Ctx()

    def owner_fetch(owner, query):
        if "projectsV2" in query:
            return _owner_projects_payload()
        if "fields" in query:
            return _fields_payload()
        return _items_payload()

    def repo_fetch(query):
        return _relations_payload()

    product = _product(repos=["steiner385/cc-account-switcher", "steiner385/KinDash"])
    loop, calls = _loop(ctx, products=[product],
                        owner_fetch=owner_fetch, repo_fetch=repo_fetch)
    await loop.tick()

    saved_repos = [repo for repo, _ in calls["save_relations"]]
    assert saved_repos == ["steiner385/cc-account-switcher", "steiner385/KinDash"]
    for _, rels in calls["save_relations"]:
        assert all(isinstance(r, IssueRelView) for r in rels)


@pytest.mark.asyncio
async def test_unmatched_project_title_is_ignored():
    ctx = Ctx()

    def owner_fetch(owner, query):
        if "projectsV2" in query:
            return _owner_projects_payload(title="Something else")
        return {"data": {}}

    def repo_fetch(query):
        return _relations_payload()

    loop, calls = _loop(ctx, products=[_product()],
                        owner_fetch=owner_fetch, repo_fetch=repo_fetch)
    await loop.tick()  # must not raise

    assert calls["save_gh_project"] == []
    assert calls["save_project_fields"] == []
    assert calls["save_project_items"] == []


@pytest.mark.asyncio
async def test_project_fetch_failure_does_not_break_repo_sync():
    """The projects query for one product raising must not stall the mirror pass
    nor crash the tick — best-effort, mirroring per-repo error handling."""
    ctx = Ctx()

    def owner_fetch(owner, query):
        if "projectsV2" in query:
            raise RuntimeError("read:project scope missing")
        return {"data": {}}

    def repo_fetch(query):
        return _relations_payload()

    loop, calls = _loop(ctx, products=[_product()],
                        owner_fetch=owner_fetch, repo_fetch=repo_fetch)
    await loop.tick()  # swallowed, no crash

    # The project pass failed so nothing was linked/saved, but the tick survived
    # and a warning was surfaced.
    assert calls["save_gh_project"] == []
    assert any(level == "warning" for level, _, _ in ctx.events)


# ---- issue #973: interval short-circuit, per-owner backoff, board-delete halt --


def _deleted_node_payload():
    """node(id:) resolves null — the board was DELETED on GitHub (spec §10)."""
    return {"data": {"node": None},
            "errors": [{"message": "Could not resolve to a node."}]}


@pytest.mark.asyncio
async def test_projects_pass_short_circuits_within_interval():
    """The Projects v2 + relations pass must not re-run every tick: inside the
    min interval the whole pass is skipped (issue #973 — was unconditional)."""
    ctx = Ctx()
    fetch_count = {"n": 0}

    def owner_fetch(owner, query):
        fetch_count["n"] += 1
        if "projectsV2" in query:
            return _owner_projects_payload()
        if "fields" in query:
            return _fields_payload()
        return _items_payload()

    def repo_fetch(query):
        return _relations_payload()

    loop, calls = _loop(ctx, products=[_product()], owner_fetch=owner_fetch,
                        repo_fetch=repo_fetch, config={"projects_sync_interval_s": 300})
    await loop.tick()
    await loop.tick()  # same clock -> still inside the 300s window
    assert len(calls["save_gh_project"]) == 1
    assert len(calls["save_relations"]) == 1
    first_round = fetch_count["n"]
    assert first_round > 0
    # no further owner fetches happened on the second tick
    assert fetch_count["n"] == first_round


@pytest.mark.asyncio
async def test_persistent_owner_error_warns_once_then_backs_off():
    """A persistent per-owner failure (e.g. missing read:project) must warn on
    onset only — not every pass — and back the owner off (issue #973)."""
    ctx = Ctx()
    clock = {"t": datetime(2026, 2, 1, tzinfo=timezone.utc)}

    def owner_fetch(owner, query):
        raise RuntimeError("read:project scope missing")

    def repo_fetch(query):
        return _relations_payload()

    loop, calls = _loop(ctx, products=[_product()], owner_fetch=owner_fetch,
                        repo_fetch=repo_fetch, now_fn=lambda: clock["t"],
                        config={"projects_sync_interval_s": 300})
    await loop.tick()                                   # onset: one warning
    clock["t"] += timedelta(seconds=301)               # past interval, in backoff
    await loop.tick()                                   # deduped: no new warning
    warns = [e for e in ctx.events if e[0] == "warning"
             and e[2].get("kind") == "gh_projects_error"]
    assert len(warns) == 1
    assert calls["save_gh_project"] == []


@pytest.mark.asyncio
async def test_deleted_board_halts_loudly_without_pruning():
    """node(id:) null = board deleted: emit a loud needs-human halt and DO NOT
    save/prune the mirror items. Relations (repo-scoped) still run (issue #973)."""
    ctx = Ctx()

    def owner_fetch(owner, query):
        if "projectsV2" in query:
            return _owner_projects_payload()
        # fields (first node query) comes back null -> board deleted.
        return _deleted_node_payload()

    def repo_fetch(query):
        return _relations_payload()

    loop, calls = _loop(ctx, products=[_product()], owner_fetch=owner_fetch,
                        repo_fetch=repo_fetch)
    await loop.tick()
    # No silent prune: neither fields nor items were saved.
    assert calls["save_project_fields"] == []
    assert calls["save_project_items"] == []
    # A loud, human-actionable error was surfaced.
    assert any(level == "error" and kw.get("kind") == "gh_project_deleted"
               for level, _, kw in ctx.events)
    # Repo relations are board-independent and still ran.
    assert [repo for repo, _ in calls["save_relations"]] == \
        ["steiner385/cc-account-switcher"]
