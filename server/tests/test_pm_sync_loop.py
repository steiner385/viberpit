"""PmSyncLoop tests — backfill resumption, watermark rules, per-repo isolation."""
from datetime import datetime, timezone

import pytest

from server.automations.pm_sync import PmSyncLoop, RepoSyncView
from server.pm.feature_index import FeatureDocsRead
from server.pm.map_changes import Pointer


class Ctx:
    def __init__(self):
        self.events = []

    async def emit(self, level, text, **kw):
        self.events.append((level, text, kw))


def _view(**over):
    base = dict(project_id="p1", project_slug="proj", repo="o/r", is_primary=True,
                backfill_done=False, backfill_phase="issues", backfill_cursor=None,
                watermark=None, last_synced_at=None, sync_requested_at=None)
    base.update(over)
    return RepoSyncView(**base)


def _issue_page(numbers, cursor, has_next):
    return {"data": {"repository": {"issues": {
        "pageInfo": {"endCursor": cursor, "hasNextPage": has_next},
        "nodes": [{"number": n, "title": f"i{n}", "body": "", "state": "OPEN",
                   "url": "", "createdAt": "2026-01-01T00:00:00Z",
                   "updatedAt": f"2026-01-0{min(n, 9)}T00:00:00Z", "closedAt": None,
                   "author": {"login": "t"}, "labels": {"nodes": []}}
                  for n in numbers]}}}}


def _pr_page(numbers, cursor, has_next, updated="2026-01-02T00:00:00Z"):
    return {"data": {"repository": {"pullRequests": {
        "pageInfo": {"endCursor": cursor, "hasNextPage": has_next},
        "nodes": [{"number": n, "title": f"p{n}", "body": "", "state": "OPEN",
                   "url": "", "createdAt": "2026-01-01T00:00:00Z",
                   "updatedAt": updated, "closedAt": None,
                   "author": {"login": "t"}, "labels": {"nodes": []},
                   "closingIssuesReferences": {"nodes": []}}
                  for n in numbers]}}}}


def _named(page, name):
    """Stamp GitHub's canonical ``nameWithOwner`` onto a canned page (issue #999)."""
    page["data"]["repository"]["nameWithOwner"] = name
    return page


def _loop(ctx, views, pages, *, config=None, feature_docs=None, rename_repo=None,
          read_feature_docs=None, read_map_changes=None, refresh_map_changes=None):
    """Wire a loop over canned data. `pages` is a list popped per fetch call.

    ``read_feature_docs``/``read_map_changes`` let a test fully replace the
    default-branch-only fake below (e.g. to answer differently per ``ref`` or
    to serve a map-changes pointer) while every caller that only passes
    ``feature_docs`` keeps getting a plain single-branch ``FeatureDocsRead``.
    """
    saved = []
    upserted = []
    applied = []
    unindexed_saved = []

    async def read_config():
        return "on", dict(config or {})

    async def list_repos():
        return views

    async def fetch(query):
        return pages.pop(0)

    async def upsert(items):
        upserted.extend(items)

    async def save(view, **updates):
        saved.append((view.repo, updates))

    async def read_docs(repo, ref=None):
        return FeatureDocsRead(sha="sha-fake", docs=list(feature_docs or []))

    async def list_rows(project_id):
        return []

    async def apply_plan(project_id, plan):
        applied.append((project_id, plan))

    async def save_unindexed(project_id, pairs):
        unindexed_saved.append((project_id, pairs))

    loop = PmSyncLoop(
        ctx, read_config=read_config, list_repos=list_repos, fetch=fetch,
        upsert_items=upsert, save_sync_state=save,
        read_feature_docs=read_feature_docs or read_docs,
        list_feature_rows=list_rows, apply_feature_plan=apply_plan,
        now_utc=lambda: datetime(2026, 2, 1, tzinfo=timezone.utc),
        save_unindexed_docs=save_unindexed,
        rename_repo=rename_repo, read_map_changes=read_map_changes,
        refresh_map_changes=refresh_map_changes)
    # Not part of the 4-tuple every existing caller unpacks — tests that care
    # about the persisted unindexed-docs list read it off the loop instance.
    loop.unindexed_saved = unindexed_saved
    return loop, saved, upserted, applied


# --- canonical repo name (issue #999) ----------------------------------------

@pytest.mark.asyncio
async def test_incremental_canonicalizes_a_renamed_repo_before_saving():
    """GitHub answers a query for the stored alias with its canonical
    ``nameWithOwner``. The loop must rename the mirror FIRST, then key every
    item and the sync-state save on the canonical name — otherwise the alias
    keeps splitting the graph (#999)."""
    ctx = Ctx()
    renamed = []

    async def rename(old, new):
        renamed.append((old, new))
        return {"work_items": 3, "deps": 2}

    pages = [_named(_issue_page([1], None, False), "o/new"), _pr_page([], None, False)]
    loop, saved, upserted, _ = _loop(
        ctx, [_view(repo="o/old", backfill_done=True)], pages, rename_repo=rename)
    await loop.tick()
    assert renamed == [("o/old", "o/new")]
    assert {i.repo for i in upserted} == {"o/new"}
    assert saved and all(repo == "o/new" for repo, _ in saved)
    assert any(kw.get("kind") == "repo_renamed" and kw.get("target") == "o/new"
               for _, _, kw in ctx.events)


@pytest.mark.asyncio
async def test_backfill_canonicalizes_on_its_first_page():
    ctx = Ctx()
    renamed = []

    async def rename(old, new):
        renamed.append((old, new))
        return {}

    pages = [_named(_issue_page([1], None, False), "o/new"), _pr_page([2], None, False)]
    loop, saved, upserted, _ = _loop(ctx, [_view(repo="o/old")], pages, rename_repo=rename)
    await loop.tick()
    assert renamed == [("o/old", "o/new")]
    assert {i.repo for i in upserted} == {"o/new"}
    assert saved[-1][0] == "o/new" and saved[-1][1].get("backfill_done") is True


@pytest.mark.asyncio
async def test_sync_without_a_rename_seam_keeps_the_stored_name():
    """Pre-existing wiring (no seam) must be unaffected: the payload's name is
    informational only and nothing is renamed."""
    ctx = Ctx()
    pages = [_named(_issue_page([1], None, False), "o/new"), _pr_page([], None, False)]
    loop, saved, upserted, _ = _loop(ctx, [_view(repo="o/old", backfill_done=True)], pages)
    await loop.tick()
    assert {i.repo for i in upserted} == {"o/old"}
    assert all(repo == "o/old" for repo, _ in saved)


@pytest.mark.asyncio
async def test_backfill_saves_cursor_per_page_and_flips_done():
    ctx = Ctx()
    pages = [_issue_page([1, 2], "C1", True), _issue_page([3], None, False),
             _pr_page([4], None, False)]
    loop, saved, upserted, applied = _loop(ctx, [_view()], pages)
    await loop.tick()
    assert [n.number for n in upserted] == [1, 2, 3, 4]
    # cursor saved after the first issues page (resumable backfill)
    assert any(u.get("backfill_cursor") == "C1" for _, u in saved)
    # final save marks backfill done and stamps the watermark + last_synced_at
    final = saved[-1][1]
    assert final.get("backfill_done") is True
    assert final.get("watermark") is not None
    assert final.get("last_synced_at") is not None


@pytest.mark.asyncio
async def test_watermark_not_advanced_when_pass_fails():
    ctx = Ctx()

    class Boom(Exception):
        pass

    async def fetch(_q):
        raise Boom()

    loop, saved, upserted, applied = _loop(ctx, [_view()], [])
    loop._fetch = fetch  # replace seam with a failing one
    await loop.tick()
    assert all("watermark" not in u for _, u in saved)
    assert any(level == "warn" for level, _, _ in ctx.events)


@pytest.mark.asyncio
async def test_watermark_not_advanced_when_incremental_fails():
    """Incremental watermark must NOT advance when _fetch raises mid-pass.

    This guards the spec's correctness crux: the watermark is written ONCE
    after a fully successful pass.  A failure mid-pass must leave it untouched
    so the next tick re-fetches the missed window (at-least-once semantics).
    """
    ctx = Ctx()
    wm = datetime(2026, 1, 15, tzinfo=timezone.utc)
    view = _view(backfill_done=True, watermark=wm, sync_requested_at=wm)
    loop, saved, upserted, applied = _loop(ctx, [view], [])

    async def boom(_q):
        raise RuntimeError("network error")

    loop._fetch = boom
    await loop.tick()
    # Watermark must NOT appear in any saved update
    assert all("watermark" not in u for _, u in saved)
    # A warning event must have been emitted for the failed repo
    assert any(level == "warn" for level, _, _ in ctx.events)


@pytest.mark.asyncio
async def test_incremental_prs_stop_at_watermark():
    ctx = Ctx()
    wm = datetime(2026, 1, 15, tzinfo=timezone.utc)
    view = _view(backfill_done=True, watermark=wm, sync_requested_at=wm)
    pages = [_issue_page([], None, False),
             _pr_page([9], "C9", True, updated="2026-01-10T00:00:00Z")]  # older than wm
    loop, saved, upserted, applied = _loop(ctx, [view], pages)
    await loop.tick()
    # PR older than watermark: page consumed, but no further page requested
    assert pages == []


@pytest.mark.asyncio
async def test_one_repo_failure_does_not_stall_others():
    ctx = Ctx()
    ok_pages = [_issue_page([], None, False), _pr_page([], None, False)]
    views = [_view(repo="o/bad"), _view(repo="o/good", project_id="p2")]
    calls = {"n": 0}

    async def fetch(query):
        if '"bad"' in query:
            raise RuntimeError("api down")
        calls["n"] += 1
        return ok_pages.pop(0)

    loop, saved, upserted, applied = _loop(ctx, views, [])
    loop._fetch = fetch
    await loop.tick()
    assert calls["n"] == 2  # good repo fully synced despite bad repo failing


@pytest.mark.asyncio
async def test_feature_read_failure_skips_reindex_without_orphaning():
    """A failed feature-doc read must SKIP the reindex, never orphan features.

    The atomic reader raises on a partial/untrustworthy read (rather than
    returning a subset).  ``_feature_index`` must swallow that and skip — running
    ``apply_feature_plan`` on an incomplete doc set would orphan the valid docs
    the read missed (the dogfood bug: 21 live features silently orphaned).  The
    work-item sync is independent and must still complete: the watermark advances
    and a warning is surfaced.
    """
    ctx = Ctx()
    wm = datetime(2026, 1, 15, tzinfo=timezone.utc)
    view = _view(backfill_done=True, watermark=wm, sync_requested_at=wm)
    pages = [_issue_page([], None, False), _pr_page([], None, False)]
    loop, saved, upserted, applied = _loop(ctx, [view], pages)

    async def boom(_repo):
        raise RuntimeError("git read partial/untrustworthy")

    loop._read_feature_docs = boom
    await loop.tick()
    # Reindex skipped — apply_feature_plan never ran, so nothing was orphaned.
    assert applied == []
    # Work-item sync is decoupled and still completed: watermark advanced.
    assert any("watermark" in u for _, u in saved)
    # The skipped reindex is surfaced as a warning.
    assert any(level == "warn" for level, _, _ in ctx.events)


@pytest.mark.asyncio
async def test_interval_gate_skips_recent_unless_requested():
    ctx = Ctx()
    now = datetime(2026, 2, 1, tzinfo=timezone.utc)
    recent = _view(backfill_done=True, last_synced_at=now)
    loop, saved, upserted, applied = _loop(ctx, [recent], [],
                                           config={"min_sync_interval_s": 300})
    await loop.tick()
    assert saved == []  # skipped — nothing fetched, nothing saved
    assert not hasattr(ctx, "observed_at")


# --- map-changes branch merge (R8b-4) -----------------------------------------


def _fdoc(slug: str, title: str) -> str:
    return f"---\nslug: {slug}\ntitle: {title}\nstate: proposed\n---\n# {title}\n"


def _open_pointer(branch="pm/map-changes-proj", pr_number=12,
                   pr_url="https://github.com/o/r/pull/12"):
    return Pointer(branch=branch, pr_number=pr_number, pr_url=pr_url, status="open",
                   opened_at=None, head_sha="sha-branch", behind=False, conflicting=False)


async def _merge_plan(ctx, main, branch):
    """One incremental pass with *main* on the default branch and *branch* on an
    open map-changes branch; returns the applied ``{slug: upsert}``."""
    wm = datetime(2026, 1, 15, tzinfo=timezone.utc)
    view = _view(backfill_done=True, watermark=wm, sync_requested_at=wm)
    pages = [_issue_page([], None, False), _pr_page([], None, False)]

    async def read_docs(repo, ref=None):
        return branch if ref == "pm/map-changes-proj" else main

    async def read_map_changes(project_id):
        return _open_pointer()

    loop, _saved, _upserted, applied = _loop(
        ctx, [view], pages, read_feature_docs=read_docs,
        read_map_changes=read_map_changes)
    await loop.tick()
    assert len(applied) == 1
    _, plan = applied[0]
    return {u.slug: u for u in plan.upserts}


@pytest.mark.asyncio
async def test_a_doc_the_branch_only_inherited_is_not_proposed():
    """THE critical case (R8b-4 as amended): a map-changes branch inherits every
    default-branch doc, so the branch read returns all of them. A doc that is
    byte-identical on both is NOT this PR's doing — it keeps the default-branch
    ``commit_sha`` and gets no PR fields.

    Stamping it proposed would null ``commit_sha`` product-wide, and abandoning
    the PR (which deletes every ``pr_number == N AND commit_sha IS NULL`` row)
    would then delete the whole capability index."""
    ctx = Ctx()
    same = _fdoc("a", "A")
    by_slug = await _merge_plan(
        ctx,
        FeatureDocsRead("sha-main", [("docs/product/features/a.md", same)]),
        FeatureDocsRead("sha-branch", [("docs/product/features/a.md", same)]))
    assert by_slug["a"].commit_sha == "sha-main"
    assert by_slug["a"].pr_number is None
    assert by_slug["a"].pr_url is None


@pytest.mark.asyncio
async def test_a_doc_changed_on_the_branch_wins_by_slug_and_is_proposed():
    """Present on both but CHANGED: the branch content wins, the PR fields are
    stamped and ``commit_sha`` is NULL (the edit is not on the default branch)."""
    ctx = Ctx()
    by_slug = await _merge_plan(
        ctx,
        FeatureDocsRead("sha-main", [
            ("docs/product/features/a.md", _fdoc("a", "A")),
            ("docs/product/features/b.md", _fdoc("b", "B")),
        ]),
        FeatureDocsRead("sha-branch", [
            ("docs/product/features/a.md", _fdoc("a", "A")),
            ("docs/product/features/b.md", _fdoc("b", "B Proposed")),
        ]))
    assert by_slug["a"].commit_sha == "sha-main"     # inherited unchanged
    assert by_slug["a"].pr_number is None
    assert by_slug["b"].title == "B Proposed"
    assert by_slug["b"].commit_sha is None
    assert by_slug["b"].pr_number == 12
    assert by_slug["b"].pr_url == "https://github.com/o/r/pull/12"


@pytest.mark.asyncio
async def test_a_branch_only_doc_is_proposed_with_the_pr_fields():
    ctx = Ctx()
    by_slug = await _merge_plan(
        ctx,
        FeatureDocsRead("sha-main", [("docs/product/features/a.md", _fdoc("a", "A"))]),
        FeatureDocsRead("sha-branch", [
            ("docs/product/features/a.md", _fdoc("a", "A")),
            ("docs/product/features/c.md", _fdoc("c", "C")),
        ]))
    assert by_slug["c"].commit_sha is None
    assert by_slug["c"].pr_number == 12
    assert by_slug["c"].pr_url == "https://github.com/o/r/pull/12"
    assert by_slug["a"].commit_sha == "sha-main"


@pytest.mark.asyncio
async def test_no_open_map_changes_pointer_skips_the_branch_read():
    ctx = Ctx()
    wm = datetime(2026, 1, 15, tzinfo=timezone.utc)
    view = _view(backfill_done=True, watermark=wm, sync_requested_at=wm)
    pages = [_issue_page([], None, False), _pr_page([], None, False)]
    calls = []

    async def read_docs(repo, ref=None):
        calls.append(ref)
        return FeatureDocsRead("sha-main", [])

    async def read_map_changes(project_id):
        return None

    loop, saved, upserted, applied = _loop(
        ctx, [view], pages, read_feature_docs=read_docs,
        read_map_changes=read_map_changes)
    await loop.tick()
    assert calls == [None]  # only the default-branch read happened


@pytest.mark.asyncio
async def test_branch_read_error_is_best_effort_default_plan_still_applied():
    """The branch read is best-effort (R8b-4): a raise there must not poison
    the default-branch plan, unlike the atomic-or-raise default-branch read."""
    ctx = Ctx()
    wm = datetime(2026, 1, 15, tzinfo=timezone.utc)
    view = _view(backfill_done=True, watermark=wm, sync_requested_at=wm)
    pages = [_issue_page([], None, False), _pr_page([], None, False)]
    main = FeatureDocsRead("sha-main", [("docs/product/features/a.md", _fdoc("a", "A"))])

    async def read_docs(repo, ref=None):
        if ref is None:
            return main
        raise RuntimeError("branch does not exist on the remote")

    async def read_map_changes(project_id):
        return _open_pointer()

    loop, saved, upserted, applied = _loop(
        ctx, [view], pages, read_feature_docs=read_docs,
        read_map_changes=read_map_changes)
    await loop.tick()

    assert len(applied) == 1
    _, plan = applied[0]
    assert plan.upserts[0].commit_sha == "sha-main"
    assert any("watermark" in u for _, u in saved)  # work-item sync still completed
    assert any(kw.get("kind") == "map_changes_read_error" for _, _, kw in ctx.events)
    assert any(level == "warn" for level, _, _ in ctx.events)


@pytest.mark.asyncio
async def test_a_standing_bad_doc_is_reported_once_not_every_tick():
    """#1902/#1635: a doc that cannot be parsed cannot be parsed next tick
    either, so the unthrottled warning was one record per tick — 254 in a day
    for one file, and an auto-error issue per path. The condition must report
    on its first sighting and then hold until the throttle window elapses."""
    ctx = Ctx()
    wm = datetime(2026, 1, 15, tzinfo=timezone.utc)
    view = _view(backfill_done=True, watermark=wm, sync_requested_at=wm)
    pages = [_issue_page([], None, False), _pr_page([], None, False)] * 3
    docs = [("docs/product/features/a.md", _fdoc("a", "A")),
            ("docs/product/features/bad.md", "---\nslug: bad\n")]  # unterminated

    loop, _saved, _upserted, _applied = _loop(ctx, [view], pages, feature_docs=docs)
    for _ in range(3):
        await loop.tick()

    skips = [t for _, t, kw in ctx.events if kw.get("kind") == "feature_doc_skipped"]
    assert len(skips) == 1, skips


@pytest.mark.asyncio
async def test_a_changed_skip_set_reports_immediately():
    """The throttle keys on the failure's identity, not on the tick, so a doc
    that is newly broken (or newly fixed) must not be held down by the window
    the previous condition opened — otherwise the heartbeat hides real news."""
    ctx = Ctx()
    wm = datetime(2026, 1, 15, tzinfo=timezone.utc)
    view = _view(backfill_done=True, watermark=wm, sync_requested_at=wm)
    pages = [_issue_page([], None, False), _pr_page([], None, False)] * 2
    docs = [("docs/product/features/bad.md", "---\nslug: bad\n")]

    loop, _saved, _upserted, _applied = _loop(ctx, [view], pages, feature_docs=docs)
    await loop.tick()
    docs.append(("docs/product/features/worse.md", "---\nslug: worse\n"))
    await loop.tick()

    skips = [t for _, t, kw in ctx.events if kw.get("kind") == "feature_doc_skipped"]
    assert len(skips) == 2
    assert "worse.md" in skips[1]


@pytest.mark.asyncio
async def test_skipped_docs_are_persisted_onto_the_project_row():
    """Issue #2166: roadmap_health reads Project.unindexed_docs, so a skipped
    doc must be saved as structured (path, error) pairs, not just logged."""
    ctx = Ctx()
    wm = datetime(2026, 1, 15, tzinfo=timezone.utc)
    view = _view(backfill_done=True, watermark=wm, sync_requested_at=wm)
    pages = [_issue_page([], None, False), _pr_page([], None, False)]
    docs = [("docs/product/features/bad.md", "---\nslug: bad\n")]

    loop, _saved, _upserted, _applied = _loop(ctx, [view], pages, feature_docs=docs)
    await loop.tick()

    (project_id, pairs) = loop.unindexed_saved[-1]
    assert project_id == "p1"
    assert pairs == [("docs/product/features/bad.md", "unterminated frontmatter fence")]


@pytest.mark.asyncio
async def test_a_fixed_doc_clears_the_persisted_unindexed_list():
    """A doc that stops being broken must clear the project row the same
    pass, not leave it stale until something else happens to rewrite it."""
    ctx = Ctx()
    wm = datetime(2026, 1, 15, tzinfo=timezone.utc)
    view = _view(backfill_done=True, watermark=wm, sync_requested_at=wm)
    pages = [_issue_page([], None, False), _pr_page([], None, False)] * 2
    docs = [("docs/product/features/bad.md", "---\nslug: bad\n")]

    loop, _saved, _upserted, _applied = _loop(ctx, [view], pages, feature_docs=docs)
    await loop.tick()
    assert loop.unindexed_saved[-1][1] != []

    docs[0] = ("docs/product/features/bad.md", _fdoc("bad", "Bad"))
    await loop.tick()
    assert loop.unindexed_saved[-1][1] == []


@pytest.mark.asyncio
async def test_a_lifecycle_synonym_is_indexed_not_skipped():
    """#1903: ghostdriver-mcp's doc says ``state: in-progress``. It named one
    canonical state unambiguously, so it belongs on the map — a skip left the
    capability invisible and re-warned forever."""
    ctx = Ctx()
    wm = datetime(2026, 1, 15, tzinfo=timezone.utc)
    view = _view(backfill_done=True, watermark=wm, sync_requested_at=wm)
    pages = [_issue_page([], None, False), _pr_page([], None, False)]
    docs = [("docs/product/features/gtk.md",
             "---\nslug: gtk\ntitle: GTK\nstate: in-progress\n---\n# GTK\nbody\n")]

    loop, _saved, _upserted, applied = _loop(ctx, [view], pages, feature_docs=docs)
    await loop.tick()

    _, plan = applied[0]
    assert [(u.slug, u.state) for u in plan.upserts] == [("gtk", "delivering")]
    assert not [t for _, t, kw in ctx.events if kw.get("kind") == "feature_doc_skipped"]


@pytest.mark.asyncio
async def test_a_doc_with_no_frontmatter_is_indexed_from_its_path():
    """#1874/#1635: docs predating the frontmatter convention were dropped on
    every pass. ``docs/product/features/<slug>.md`` IS the slug convention, so
    the path names the capability and the H1 titles it."""
    ctx = Ctx()
    wm = datetime(2026, 1, 15, tzinfo=timezone.utc)
    view = _view(backfill_done=True, watermark=wm, sync_requested_at=wm)
    pages = [_issue_page([], None, False), _pr_page([], None, False)]
    docs = [("docs/product/features/deploy-monitor.md",
             "# Deploy Monitor\n\nWatches deployment records.\n")]

    loop, _saved, _upserted, applied = _loop(ctx, [view], pages, feature_docs=docs)
    await loop.tick()

    _, plan = applied[0]
    assert [(u.slug, u.title, u.state) for u in plan.upserts] == [
        ("deploy-monitor", "Deploy Monitor", "proposed")]
    assert not [t for _, t, kw in ctx.events if kw.get("kind") == "feature_doc_skipped"]


@pytest.mark.asyncio
async def test_the_skipped_doc_denominator_counts_deduplicated_slugs():
    """A doc on BOTH the default branch and the map-changes branch is ONE
    capability: "N of M could not be parsed" must count de-duplicated slugs plus
    the skipped docs, not raw doc rows (a 1-of-3 truth read as 1-of-4)."""
    ctx = Ctx()
    wm = datetime(2026, 1, 15, tzinfo=timezone.utc)
    view = _view(backfill_done=True, watermark=wm, sync_requested_at=wm)
    pages = [_issue_page([], None, False), _pr_page([], None, False)]
    main = FeatureDocsRead("sha-main", [
        ("docs/product/features/a.md", _fdoc("a", "A")),
        ("docs/product/features/b.md", _fdoc("b", "B")),
        ("docs/product/features/bad.md", "---\nslug: bad\n"),  # unterminated fence
    ])
    branch = FeatureDocsRead("sha-branch", [
        ("docs/product/features/b.md", _fdoc("b", "B Proposed"))])

    async def read_docs(repo, ref=None):
        return branch if ref == "pm/map-changes-proj" else main

    async def read_map_changes(project_id):
        return _open_pointer()

    loop, _saved, _upserted, _applied = _loop(
        ctx, [view], pages, read_feature_docs=read_docs,
        read_map_changes=read_map_changes)
    await loop.tick()

    (text,) = [t for _, t, kw in ctx.events if kw.get("kind") == "feature_doc_skipped"]
    assert "1 of 3 feature docs" in text


@pytest.mark.asyncio
async def test_an_unparseable_doc_on_the_branch_is_skipped_and_reported():
    """A bad doc on the map-changes branch must not poison the pass either: it
    is skipped like a default-branch one and named in the same warning, so a
    capability the UI wrote and cannot parse back is visible."""
    ctx = Ctx()
    wm = datetime(2026, 1, 15, tzinfo=timezone.utc)
    view = _view(backfill_done=True, watermark=wm, sync_requested_at=wm)
    pages = [_issue_page([], None, False), _pr_page([], None, False)]
    main = FeatureDocsRead("sha-main", [
        ("docs/product/features/a.md", _fdoc("a", "A"))])
    branch = FeatureDocsRead("sha-branch", [
        ("docs/product/features/a.md", _fdoc("a", "A")),          # inherited
        ("docs/product/features/c.md", _fdoc("c", "C")),          # new, good
        ("docs/product/features/broken.md", "---\nslug: broken\n"),  # unterminated fence
    ])

    async def read_docs(repo, ref=None):
        return branch if ref == "pm/map-changes-proj" else main

    async def read_map_changes(project_id):
        return _open_pointer()

    loop, _saved, _upserted, applied = _loop(
        ctx, [view], pages, read_feature_docs=read_docs,
        read_map_changes=read_map_changes)
    await loop.tick()

    # The good docs still indexed — one bad doc never poisons the index.
    _, plan = applied[0]
    assert {u.slug for u in plan.upserts} == {"a", "c"}
    (text,) = [t for _, t, kw in ctx.events if kw.get("kind") == "feature_doc_skipped"]
    assert "1 of 3 feature docs" in text
    assert "broken.md" in text


# --- map-changes PR lifecycle refresh (R8b-6) ---------------------------------


def _incremental_view():
    wm = datetime(2026, 1, 15, tzinfo=timezone.utc)
    return _view(backfill_done=True, watermark=wm, sync_requested_at=wm)


@pytest.mark.asyncio
async def test_incremental_refreshes_the_map_changes_pr_before_the_index():
    """R8b-6: one PR read per open pointer per incremental pass, driven by the
    loop rather than the session PR poller (which watches session branches) —
    and BEFORE the index, whose branch read is gated on the pointer's status."""
    ctx = Ctx()
    pages = [_issue_page([], None, False), _pr_page([], None, False)]
    order = []

    async def refresh(project_id):
        order.append(("refresh", project_id))

    async def read_docs(repo, ref=None):
        order.append(("index", ref))
        return FeatureDocsRead("sha-main", [])

    loop, saved, _upserted, applied = _loop(
        ctx, [_incremental_view()], pages, refresh_map_changes=refresh,
        read_feature_docs=read_docs)
    await loop.tick()
    assert order == [("refresh", "p1"), ("index", None)]
    assert len(applied) == 1          # …and the feature index still ran


@pytest.mark.asyncio
async def test_a_pointer_that_merged_this_tick_is_not_indexed_as_open():
    """The lifecycle pass runs first, so a PR that merged since the last tick is
    out of ``open`` before the index looks: no branch read that pass (the branch
    may already be gone), and therefore no ``map_changes_read_error``."""
    ctx = Ctx()
    pages = [_issue_page([], None, False), _pr_page([], None, False)]
    state = {"status": "open"}
    refs = []

    async def refresh(project_id):
        state["status"] = "merged"

    async def read_map_changes(project_id):
        return Pointer(branch="pm/map-changes-proj", pr_number=12,
                       pr_url="https://github.com/o/r/pull/12",
                       status=state["status"], opened_at=None,
                       head_sha="sha-branch", behind=False, conflicting=False)

    async def read_docs(repo, ref=None):
        refs.append(ref)
        if ref is not None:
            raise RuntimeError("branch deleted on merge")
        return FeatureDocsRead("sha-main", [])

    loop, _saved, _upserted, applied = _loop(
        ctx, [_incremental_view()], pages, refresh_map_changes=refresh,
        read_map_changes=read_map_changes, read_feature_docs=read_docs)
    await loop.tick()
    assert refs == [None]             # the branch was never read
    assert len(applied) == 1
    assert not [kw for _, _, kw in ctx.events
                if kw.get("kind") == "map_changes_read_error"]


@pytest.mark.asyncio
async def test_a_non_primary_repo_never_refreshes_map_changes():
    ctx = Ctx()
    pages = [_issue_page([], None, False), _pr_page([], None, False)]
    refreshed = []

    async def refresh(project_id):
        refreshed.append(project_id)

    view = _view(backfill_done=True, is_primary=False,
                 watermark=datetime(2026, 1, 15, tzinfo=timezone.utc),
                 sync_requested_at=datetime(2026, 1, 15, tzinfo=timezone.utc))
    loop, *_ = _loop(ctx, [view], pages, refresh_map_changes=refresh)
    await loop.tick()
    assert refreshed == []


@pytest.mark.asyncio
async def test_a_failing_map_changes_refresh_is_best_effort():
    """GitHub being down must not stall the work-item mirror: the refresh warns
    and the pass still saves its watermark."""
    ctx = Ctx()
    pages = [_issue_page([], None, False), _pr_page([], None, False)]

    async def refresh(project_id):
        raise RuntimeError("GET pulls/12 failed")

    loop, saved, _upserted, applied = _loop(
        ctx, [_incremental_view()], pages, refresh_map_changes=refresh)
    await loop.tick()
    assert any("watermark" in u for _, u in saved)
    assert any(kw.get("kind") == "map_changes_refresh_error" for _, _, kw in ctx.events)
    assert not any(kw.get("kind") == "sync_error" for _, _, kw in ctx.events)
