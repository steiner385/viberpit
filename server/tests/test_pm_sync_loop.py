"""PmSyncLoop tests — backfill resumption, watermark rules, per-repo isolation."""
import logging
from datetime import datetime, timezone

import pytest

from server.automations.pm_sync import PmSyncLoop, RepoSyncView
from server.github import host_failures
from server.pm.feature_index import FeatureDocsRead
from server.pm.map_changes import Pointer

from .test_gh_host_failures import STDERR_RATE_LIMITED_IP, STDERR_UNREACHABLE


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
          read_feature_docs=None, read_map_changes=None, refresh_map_changes=None,
          read_constitution=None, list_committed_artifacts=None, read_artifact_doc=None,
          apply_artifact_frontmatter=None, persona_docs=None, read_persona_docs=None):
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
    constitution_saved = []
    persona_applied = []

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

    async def save_constitution(project_id, status):
        constitution_saved.append((project_id, status))

    async def read_persona_docs_fake(repo, ref=None):
        return FeatureDocsRead(sha="sha-fake", docs=list(persona_docs or []))

    async def list_persona_rows_fake(project_id):
        return []

    async def apply_persona_plan_fake(project_id, plan):
        persona_applied.append((project_id, plan))

    loop = PmSyncLoop(
        ctx, read_config=read_config, list_repos=list_repos, fetch=fetch,
        upsert_items=upsert, save_sync_state=save,
        read_feature_docs=read_feature_docs or read_docs,
        list_feature_rows=list_rows, apply_feature_plan=apply_plan,
        now_utc=lambda: datetime(2026, 2, 1, tzinfo=timezone.utc),
        save_unindexed_docs=save_unindexed,
        rename_repo=rename_repo, read_map_changes=read_map_changes,
        refresh_map_changes=refresh_map_changes,
        read_constitution=read_constitution, save_constitution_status=save_constitution,
        list_committed_artifacts=list_committed_artifacts, read_artifact_doc=read_artifact_doc,
        apply_artifact_frontmatter=apply_artifact_frontmatter,
        read_persona_docs=read_persona_docs or read_persona_docs_fake,
        list_persona_rows=list_persona_rows_fake,
        apply_persona_plan=apply_persona_plan_fake,
    )
    # Not part of the 4-tuple every existing caller unpacks — tests that care
    # about the persisted unindexed-docs list read it off the loop instance.
    loop.unindexed_saved = unindexed_saved
    loop.constitution_saved = constitution_saved
    loop.persona_applied = persona_applied
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


@pytest.fixture(autouse=True)
def _fresh_host_throttle():
    """The host-condition collapse window is module state on host_failures; each
    test here starts at first sighting, same isolation as test_gh_host_failures.py."""
    host_failures._HOST_THROTTLE.clear()
    yield
    host_failures._HOST_THROTTLE.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("stderr", [STDERR_UNREACHABLE, STDERR_RATE_LIMITED_IP])
async def test_repo_error_collapses_a_host_condition_instead_of_fanning_out(
        stderr, caplog):
    """A dead network or an unauthenticated-IP rate limit hits every repo's
    fetch in the same tick — filing ``pm_sync.repo_error`` at WARNING per repo
    re-mints the fan-out host_failures.py already fixed for ci_issues,
    deployments and pr_poller on this exact incident (#1917-#1921, one outage
    on 2026-09-17 filed as five per-repo issues). It must route through
    log_gh_failure and collapse into the one host record instead."""
    ctx = Ctx()
    views = [_view(repo="o/bad"), _view(repo="o/good", project_id="p2")]
    ok_pages = [_issue_page([], None, False), _pr_page([], None, False)]

    async def fetch(query):
        if '"bad"' in query:
            raise RuntimeError(f"gh graphql failed: {stderr}")
        return ok_pages.pop(0)

    loop, saved, upserted, applied = _loop(ctx, views, [])
    loop._fetch = fetch
    with caplog.at_level(logging.DEBUG):
        await loop.tick()

    repo_records = [r for r in caplog.records if r.name == "server.automations.pm_sync"]
    warned = [r for r in repo_records if r.levelno >= logging.WARNING]
    assert warned == [], f"a host condition must not warn per-repo: {[r.getMessage() for r in warned]}"
    debugged = [r for r in repo_records if r.levelno == logging.DEBUG]
    assert any("pm_sync.repo_error repo=o/bad" in r.getMessage() for r in debugged)

    host_records = [r for r in caplog.records if r.name == "server.github.host"]
    assert any(r.levelno == logging.WARNING for r in host_records)


@pytest.mark.asyncio
async def test_repo_error_still_warns_for_a_non_host_failure(caplog):
    """The demotion is specific to a recognized host condition — any other
    per-repo failure keeps reporting at WARNING with repo= exactly as before."""
    ctx = Ctx()
    views = [_view(repo="o/bad"), _view(repo="o/good", project_id="p2")]
    ok_pages = [_issue_page([], None, False), _pr_page([], None, False)]

    async def fetch(query):
        if '"bad"' in query:
            raise RuntimeError("gh graphql failed: gh: not found (HTTP 404)")
        return ok_pages.pop(0)

    loop, saved, upserted, applied = _loop(ctx, views, [])
    loop._fetch = fetch
    with caplog.at_level(logging.DEBUG):
        await loop.tick()

    warned = [r for r in caplog.records
              if r.name == "server.automations.pm_sync" and r.levelno >= logging.WARNING]
    assert len(warned) == 1
    assert "pm_sync.repo_error repo=o/bad" in warned[0].getMessage()


def _incremental_pr_fetch_failing(stderr):
    """A backfilled o/bad whose incremental issues query succeeds but whose
    ``build_prs_query`` fetch fails with *stderr* — the #3022 shape."""
    wm = datetime(2026, 1, 15, tzinfo=timezone.utc)
    views = [_view(repo="o/bad", backfill_done=True, watermark=wm),
             _view(repo="o/good", project_id="p2")]
    ok_pages = [_issue_page([], None, False), _pr_page([], None, False)]
    calls = []

    async def fetch(query):
        if '"bad"' in query:
            calls.append("prs" if "pullRequests" in query else "issues")
            if "pullRequests" in query:
                raise RuntimeError(f"gh graphql failed: {stderr}")
            return _issue_page([], None, False)
        return ok_pages.pop(0)

    return views, fetch, calls


@pytest.mark.asyncio
async def test_repo_error_logs_a_github_edge_5xx_at_info(caplog):
    """GitHub's own edge returning 504 on the incremental PR query is an outage
    retried next tick, not a defect — it must not auto-file a
    ``pm_sync.repo_error`` WARNING (#3022), while the operator card still gets
    the ``sync_error`` warn event."""
    ctx = Ctx()
    views, fetch, calls = _incremental_pr_fetch_failing("gh: HTTP 504")
    loop, saved, upserted, applied = _loop(ctx, views, [])
    loop._fetch = fetch
    with caplog.at_level(logging.DEBUG):
        await loop.tick()

    assert calls == ["issues", "prs"]  # failed on the incremental PR query
    repo_records = [r for r in caplog.records if r.name == "server.automations.pm_sync"]
    warned = [r for r in repo_records if r.levelno >= logging.WARNING]
    assert warned == [], f"an edge 5xx must not warn: {[r.getMessage() for r in warned]}"
    assert any(r.levelno == logging.INFO
               and "pm_sync.repo_error repo=o/bad" in r.getMessage()
               for r in repo_records)
    assert any(level == "warn" and kw.get("kind") == "sync_error"
               and kw.get("target") == "o/bad" for level, _, kw in ctx.events)


@pytest.mark.asyncio
async def test_repo_error_still_warns_for_a_non_edge_incremental_failure(caplog):
    """The edge demotion must not swallow a real per-repo failure: it keeps the
    WARNING with ``repo=`` unchanged so the existing fingerprint lineage holds."""
    ctx = Ctx()
    views, fetch, calls = _incremental_pr_fetch_failing(
        "GraphQL: Could not resolve to a Repository with the name 'o/bad'.")
    loop, saved, upserted, applied = _loop(ctx, views, [])
    loop._fetch = fetch
    with caplog.at_level(logging.DEBUG):
        await loop.tick()

    warned = [r for r in caplog.records
              if r.name == "server.automations.pm_sync" and r.levelno >= logging.WARNING]
    assert len(warned) == 1
    assert warned[0].getMessage() == "pm_sync.repo_error repo=o/bad"


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
async def test_a_readme_index_is_not_reported_as_a_skipped_doc(caplog):
    """#2478: docs/product/features/README.md has no frontmatter and "README"
    isn't a valid slug — an intentional non-doc, not a malformed one. It must
    not inflate the skipped-doc count, appear in the persisted unindexed list,
    or fire the WARNING that ViberPit's error-triage auto-files."""
    ctx = Ctx()
    wm = datetime(2026, 1, 15, tzinfo=timezone.utc)
    view = _view(backfill_done=True, watermark=wm, sync_requested_at=wm)
    pages = [_issue_page([], None, False), _pr_page([], None, False)]
    docs = [("docs/product/features/a.md", _fdoc("a", "A")),
            ("docs/product/features/README.md", "# Feature registry\n\nSee below.\n")]

    loop, _saved, _upserted, _applied = _loop(ctx, [view], pages, feature_docs=docs)
    with caplog.at_level(logging.DEBUG, logger="server.automations.pm_sync"):
        await loop.tick()

    assert not any(kw.get("kind") == "feature_doc_skipped" for _, _, kw in ctx.events)
    assert loop.unindexed_saved[-1] == ("p1", [])
    assert not any(r.getMessage().startswith("pm_sync.bad_feature_doc")
                   for r in caplog.records)


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
async def test_incremental_classifies_and_saves_constitution_status():
    ctx = Ctx()
    view = _view()

    async def read_constitution(repo, ref=None):
        return "# Real Constitution\n\n**Version**: 1.0.0 | **Ratified**: 2026-01-01 | **Last Amended**: 2026-01-01"

    loop, saved, upserted, applied = _loop(
        ctx, [view], [_issue_page([], None, False), _pr_page([], None, False)],
        read_constitution=read_constitution)
    await loop._incremental(view, datetime(2026, 2, 1, tzinfo=timezone.utc))
    assert loop.constitution_saved == [("p1", None)]  # ratified ⇒ None


@pytest.mark.asyncio
async def test_incremental_classifies_a_missing_constitution():
    ctx = Ctx()
    view = _view()

    async def read_constitution(repo, ref=None):
        return None

    loop, saved, upserted, applied = _loop(
        ctx, [view], [_issue_page([], None, False), _pr_page([], None, False)],
        read_constitution=read_constitution)
    await loop._incremental(view, datetime(2026, 2, 1, tzinfo=timezone.utc))
    assert loop.constitution_saved == [("p1", "missing")]


@pytest.mark.asyncio
async def test_governance_check_skipped_for_non_primary_repo():
    ctx = Ctx()
    view = _view(is_primary=False)

    async def read_constitution(repo, ref=None):
        raise AssertionError("must not be called for a non-primary repo")

    loop, saved, upserted, applied = _loop(
        ctx, [view], [_issue_page([], None, False), _pr_page([], None, False)],
        read_constitution=read_constitution)
    await loop._incremental(view, datetime(2026, 2, 1, tzinfo=timezone.utc))
    assert loop.constitution_saved == []


@pytest.mark.asyncio
async def test_governance_check_is_best_effort_on_read_failure():
    ctx = Ctx()
    view = _view()

    async def read_constitution(repo, ref=None):
        raise RuntimeError("clone failed")

    loop, saved, upserted, applied = _loop(
        ctx, [view], [_issue_page([], None, False), _pr_page([], None, False)],
        read_constitution=read_constitution)
    await loop._incremental(view, datetime(2026, 2, 1, tzinfo=timezone.utc))
    assert loop.constitution_saved == []  # never saved, but the pass must not raise


class _Art:
    def __init__(self, artifact_id, initiative_id, initiative_slug, kind, repo_path):
        self.artifact_id = artifact_id
        self.initiative_id = initiative_id
        self.initiative_slug = initiative_slug
        self.kind = kind
        self.repo_path = repo_path


@pytest.mark.asyncio
async def test_artifact_frontmatter_sync_applies_a_parsed_batch():
    ctx = Ctx()
    view = _view()
    art = _Art("art-1", "ini-1", "my-init", "spec", "docs/sdlc/my-init/spec.md")
    applied = []

    async def list_artifacts(project_id, repo):
        return [art]

    async def read_doc(repo, path, ref=None):
        return "---\ninitiative: my-init\nkind: spec\nepic: o/r#7\n---\nbody\n"

    async def apply(project_id, results):
        applied.append((project_id, results))

    loop, *_ = _loop(
        ctx, [view], [_issue_page([], None, False), _pr_page([], None, False)],
        list_committed_artifacts=list_artifacts, read_artifact_doc=read_doc,
        apply_artifact_frontmatter=apply)
    await loop._incremental(view, datetime(2026, 2, 1, tzinfo=timezone.utc))

    assert len(applied) == 1
    project_id, results = applied[0]
    assert project_id == "p1"
    assert len(results) == 1
    r = results[0]
    assert r.artifact_id == "art-1"
    assert r.frontmatter_present is True
    assert r.epic == "o/r#7"


@pytest.mark.asyncio
async def test_artifact_frontmatter_sync_skipped_for_non_primary_repo():
    ctx = Ctx()
    view = _view(is_primary=False)

    async def list_artifacts(project_id, repo):
        raise AssertionError("must not be called for a non-primary repo")

    loop, *_ = _loop(
        ctx, [view], [_issue_page([], None, False), _pr_page([], None, False)],
        list_committed_artifacts=list_artifacts)
    await loop._incremental(view, datetime(2026, 2, 1, tzinfo=timezone.utc))  # must not raise


@pytest.mark.asyncio
async def test_artifact_frontmatter_sync_skips_a_doc_that_fails_to_read():
    ctx = Ctx()
    view = _view()
    art = _Art("art-1", "ini-1", "my-init", "spec", "docs/sdlc/my-init/spec.md")
    applied = []

    async def list_artifacts(project_id, repo):
        return [art]

    async def read_doc(repo, path, ref=None):
        raise RuntimeError("git show failed")

    async def apply(project_id, results):
        applied.append((project_id, results))

    loop, *_ = _loop(
        ctx, [view], [_issue_page([], None, False), _pr_page([], None, False)],
        list_committed_artifacts=list_artifacts, read_artifact_doc=read_doc,
        apply_artifact_frontmatter=apply)
    await loop._incremental(view, datetime(2026, 2, 1, tzinfo=timezone.utc))  # must not raise
    assert applied == []  # nothing to apply — the one doc's read failed


@pytest.mark.asyncio
async def test_artifact_frontmatter_sync_skips_unparseable_doc_without_poisoning_batch():
    ctx = Ctx()
    view = _view()
    bad = _Art("art-bad", "ini-bad", "bad-init", "spec", "docs/sdlc/bad-init/spec.md")
    good = _Art("art-good", "ini-good", "good-init", "spec", "docs/sdlc/good-init/spec.md")
    applied = []

    async def list_artifacts(project_id, repo):
        return [bad, good]

    async def read_doc(repo, path, ref=None):
        if "bad-init" in path:
            return "---\n[unclosed\n---\nbody\n"
        return "---\ninitiative: good-init\nkind: spec\n---\nbody\n"

    async def apply(project_id, results):
        applied.append((project_id, results))

    loop, *_ = _loop(
        ctx, [view], [_issue_page([], None, False), _pr_page([], None, False)],
        list_committed_artifacts=list_artifacts, read_artifact_doc=read_doc,
        apply_artifact_frontmatter=apply)
    await loop._incremental(view, datetime(2026, 2, 1, tzinfo=timezone.utc))

    assert len(applied) == 1
    _, results = applied[0]
    assert [r.artifact_id for r in results] == ["art-good"]


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


# --- persona index (issue #2164) ----------------------------------------

@pytest.mark.asyncio
async def test_persona_index_runs_and_applies_a_plan():
    ctx = Ctx()
    persona_docs = [("docs/product/personas/teen.md",
                     "---\nslug: teen\ntitle: Teen\n---\nbody\n")]
    loop, _, _, _ = _loop(
        ctx, [_view(repo="o/r", backfill_done=True)],
        [_issue_page([], None, False), _pr_page([], None, False)],
        persona_docs=persona_docs)
    await loop.tick()
    assert len(loop.persona_applied) == 1
    _, plan = loop.persona_applied[0]
    assert plan.upserts[0].slug == "teen"


@pytest.mark.asyncio
async def test_a_readme_index_is_not_reported_as_a_bad_persona_doc(caplog):
    """#2478: docs/product/personas/README.md has no frontmatter and "README"
    isn't a valid slug — an intentional non-doc, not a malformed one. It must
    not be indexed and must not fire the WARNING that ViberPit's error-triage
    auto-files."""
    ctx = Ctx()
    persona_docs = [
        ("docs/product/personas/teen.md", "---\nslug: teen\ntitle: Teen\n---\nbody\n"),
        ("docs/product/personas/README.md", "# Persona registry\n\nSee below.\n"),
    ]
    loop, _, _, _ = _loop(
        ctx, [_view(repo="o/r", backfill_done=True)],
        [_issue_page([], None, False), _pr_page([], None, False)],
        persona_docs=persona_docs)
    with caplog.at_level(logging.DEBUG, logger="server.automations.pm_sync"):
        await loop.tick()

    _, plan = loop.persona_applied[0]
    assert [u.slug for u in plan.upserts] == ["teen"]
    assert not any(r.getMessage().startswith("pm_sync.bad_persona_doc")
                   for r in caplog.records)


@pytest.mark.asyncio
async def test_persona_index_runs_before_feature_index_same_tick():
    """A capability doc citing a persona added in the SAME tick must see it —
    Persona rows have to exist before the feature plan is applied."""
    ctx = Ctx()
    order = []

    async def read_persona(repo, ref=None):
        order.append("persona")
        return FeatureDocsRead(sha="s", docs=[])

    async def read_feature(repo, ref=None):
        order.append("feature")
        return FeatureDocsRead(sha="s", docs=[])

    loop, _, _, _ = _loop(
        ctx, [_view(repo="o/r", backfill_done=True)],
        [_issue_page([], None, False), _pr_page([], None, False)],
        read_persona_docs=read_persona, read_feature_docs=read_feature)
    await loop.tick()
    assert order == ["persona", "feature"]


@pytest.mark.asyncio
async def test_persona_index_is_a_noop_when_seam_not_wired():
    """Every pre-existing PmSyncLoop caller (no persona seams passed) must be
    unaffected — the seams default to None."""
    from server.automations.pm_sync import PmSyncLoop

    ctx = Ctx()

    async def read_config():
        return "on", {}

    async def list_repos():
        return [_view(repo="o/r", backfill_done=True)]

    pages = [_issue_page([], None, False), _pr_page([], None, False)]

    async def fetch(query):
        return pages.pop(0)

    async def upsert(items):
        pass

    async def save(view, **updates):
        pass

    async def read_docs(repo, ref=None):
        return FeatureDocsRead(sha="s", docs=[])

    async def list_rows(project_id):
        return []

    async def apply_plan(project_id, plan):
        pass

    loop = PmSyncLoop(
        ctx, read_config=read_config, list_repos=list_repos, fetch=fetch,
        upsert_items=upsert, save_sync_state=save, read_feature_docs=read_docs,
        list_feature_rows=list_rows, apply_feature_plan=apply_plan,
        now_utc=lambda: datetime(2026, 2, 1, tzinfo=timezone.utc))
    await loop.tick()  # must not raise
