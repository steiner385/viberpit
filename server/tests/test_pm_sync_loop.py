"""PmSyncLoop tests — backfill resumption, watermark rules, per-repo isolation."""
from datetime import datetime, timezone

import pytest

from server.automations.pm_sync import PmSyncLoop, RepoSyncView


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


def _loop(ctx, views, pages, *, config=None, feature_docs=None, rename_repo=None):
    """Wire a loop over canned data. `pages` is a list popped per fetch call."""
    saved = []
    upserted = []
    applied = []

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

    async def read_docs(repo):
        return list(feature_docs or [])

    async def list_rows(project_id):
        return []

    async def apply_plan(project_id, plan, docs_by_slug):
        applied.append((project_id, plan))

    loop = PmSyncLoop(
        ctx, read_config=read_config, list_repos=list_repos, fetch=fetch,
        upsert_items=upsert, save_sync_state=save, read_feature_docs=read_docs,
        list_feature_rows=list_rows, apply_feature_plan=apply_plan,
        now_utc=lambda: datetime(2026, 2, 1, tzinfo=timezone.utc),
        rename_repo=rename_repo)
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
