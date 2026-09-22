# Capability Management + Inline Triage Creation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add capability retire/merge to the PM layer (with a dependents guard and
auto-reparenting of children), a re-map action for initiatives/issues, a Products →
Capabilities tab, and inline capability creation during triage accept.

**Architecture:** Retire and merge are new operations on `MapChangesService`
(`server/pm/map_changes.py`), built from the same commit-to-branch primitive the
existing `create`/`rename`/`reparent`/`summary`/`links` ops already use, plus direct
DB re-pointing for merge's initiative/issue/link relationships (which aren't doc
fields). Both call a new `_resync_now` helper that runs the pm-sync indexer inline
(reusing the exact seams `PmSyncLoop` uses) so the DB reflects the change before the
HTTP response returns, instead of waiting up to 300s for the background tick.
Re-mapping initiatives/issues and the Capabilities tab reuse existing single-item
PATCH endpoints. Triage-inline capability creation reuses the existing `create` op
plus the same inline resync, then classifies the work item in the same request.

**Tech Stack:** Python/FastAPI/SQLAlchemy (async) on the server; React/TypeScript +
TanStack Query on the web client. Existing test conventions: `pytest` with a
`FakeGh`-backed `TestClient` for map-changes HTTP tests; `vitest` for web.

**Spec:** `docs/superpowers/specs/2026-09-22-capability-management-design.md`

## Global Constraints

- Every capability doc edit is a git commit on the product's `pm/map-changes-<slug>`
  branch — never write `docs/product/features/*.md` any other way.
- `MapOpError` is the one exception type for capability-op failures; it always maps
  to HTTP 422 via the existing `_map_changes_http` (`server/api/pm.py:1088`) — do not
  add a new exception type or a new status-code branch there.
- `_resync_now` (new) is the only place outside `PmSyncLoop` allowed to call the
  pm-sync seams (`_pm_sync_read_feature_docs` / `_pm_sync_list_feature_rows` /
  `_pm_sync_apply_feature_plan`) — never call them ad hoc elsewhere.
- Existing `create`/`rename`/`reparent`/`summary`/`links` dialogs keep their current
  async (up to 300s) indexing lag — do not change their behavior in this work.
- Web: mutation hooks follow the existing `useEditMapChanges`/`useUpdateInitiative`
  shape (`useMutation` + `PM_MUTATION_META` + targeted `invalidateQueries`) — do not
  introduce a different data-fetching pattern.
- Run the **full** `npm run test` after any web change (not just changed files) —
  ~47 web test files mock `@/api/hooks`, so a new hook can silently break tests in
  files never touched directly.
- `server/tests/` run via `env -i` with an isolated `HOME` and
  `PYTHONPATH=$PWD/src`; unset `CCSWITCH_SESSIOND_*`/`CCSWITCH_DAEMON_ROLE` first.

---

## PR 1 — Retire

### Task 1: `merge_branch_over_main` — the doc-merge-by-slug helper

**Files:**
- Modify: `server/pm/feature_index.py`
- Test: `server/tests/test_pm_feature_index.py`

**Interfaces:**
- Produces: `merge_branch_over_main(main_docs: list[tuple[str, str]], branch_docs: list[tuple[str, str]], *, pr_number: int | None, pr_url: str | None) -> tuple[list[tuple[str, FeatureDoc]], dict[str, tuple[int | None, str | None]], list[str]]` — returns `(docs, proposed, skipped)`, consumed by Task 3's `_resync_now`.

A capability doc committed by `retire`/`merge`/`create` lives ONLY on the product's
map-changes branch until its PR merges — reading just the default branch would miss
it entirely. `PmSyncLoop._feature_index` (`server/automations/pm_sync.py:654-694`)
already does exactly this merge inline (parse every default-branch doc, then
override by slug with any branch doc that differs from what the branch merely
inherited — R8b-4). This task extracts the pure decision logic into a standalone,
independently-testable function. **This is a new function used only by the new
`_resync_now` (Task 3) — `PmSyncLoop._feature_index`'s own inline block is left
untouched**: it carries throttled per-doc logging and feed-event emission
(`self._bad_doc_log`, `self._ctx.emit`) that are specific to a long-running
background loop and not worth the risk of touching in this PR series; the two
implementations share the same core algorithm by construction (this task's docstring
cites the other one), not by a forced single call site.

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_pm_feature_index.py — append

from server.pm.feature_index import merge_branch_over_main


ROOT_TEXT = "---\nslug: sessions\ntitle: Sessions\nstate: live\n---\n# Sessions\n"
CHILD_TEXT = ("---\nslug: dictation\ntitle: Dictation\nstate: live\nparent: sessions\n"
              "---\n# Dictation\n")


def test_merge_branch_over_main_uses_main_when_no_branch_docs():
    docs, proposed, skipped = merge_branch_over_main(
        [("docs/product/features/sessions.md", ROOT_TEXT)], [],
        pr_number=None, pr_url=None)
    assert [d.slug for _p, d in docs] == ["sessions"]
    assert proposed == {}
    assert skipped == []


def test_merge_branch_over_main_overrides_only_changed_slugs():
    """A branch inherits every default-branch doc — an UNCHANGED inherited doc
    must not be marked proposed (R8b-4), only a doc that is new or edited there."""
    changed_root = ROOT_TEXT.replace("state: live", "state: sunset")
    docs, proposed, skipped = merge_branch_over_main(
        main_docs=[("docs/product/features/sessions.md", ROOT_TEXT),
                   ("docs/product/features/dictation.md", CHILD_TEXT)],
        branch_docs=[("docs/product/features/sessions.md", changed_root),  # changed
                     ("docs/product/features/dictation.md", CHILD_TEXT)],   # unchanged
        pr_number=7, pr_url="https://github.com/o/r/pull/7")
    by_slug = {d.slug: d for _p, d in docs}
    assert by_slug["sessions"].state == "sunset"
    assert by_slug["dictation"].state == "live"
    assert proposed == {"sessions": (7, "https://github.com/o/r/pull/7")}
    assert skipped == []


def test_merge_branch_over_main_collects_unparseable_docs_from_either_side():
    docs, proposed, skipped = merge_branch_over_main(
        main_docs=[("docs/product/features/broken.md", "not frontmatter at all")],
        branch_docs=[("docs/product/features/also-broken.md", "still not frontmatter")],
        pr_number=1, pr_url=None)
    assert docs == []
    assert len(skipped) == 2
    assert "broken.md" in skipped[0] and "also-broken.md" in skipped[1]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=$PWD/src python -m pytest server/tests/test_pm_feature_index.py -k merge_branch_over_main -v`
Expected: FAIL with `ImportError: cannot import name 'merge_branch_over_main'`

- [ ] **Step 3: Implement `merge_branch_over_main`**

Add to `server/pm/feature_index.py`, near `plan_feature_index`. First widen the
existing top-of-file import (currently `from server.pm.featuredoc import FeatureDoc,
FeatureLinkRef`) to also bring in `FeatureDocError, parse_feature_doc`:

```python
from server.pm.featuredoc import FeatureDoc, FeatureDocError, FeatureLinkRef, parse_feature_doc
```

Then add:

```python
def merge_branch_over_main(
    main_docs: list[tuple[str, str]],
    branch_docs: list[tuple[str, str]],
    *, pr_number: int | None, pr_url: str | None,
) -> tuple[list[tuple[str, FeatureDoc]], dict[str, tuple[int | None, str | None]], list[str]]:
    """Parse every default-branch doc, then override by slug with any branch
    doc that differs from what the branch merely inherited (R8b-4, mirrored
    from the inline block in ``PmSyncLoop._feature_index``,
    ``server/automations/pm_sync.py:654-694`` — see that method for the
    throttled-logging/feed-event variant of this same decision used by the
    background loop).

    A branch inherits every default-branch doc, so the branch read returns all
    of them — stamping the whole set "proposed" would null every untouched
    capability's ``commit_sha`` and hand it this PR's number, so abandoning
    the PR would delete the entire index. An unchanged inherited doc is
    therefore skipped: it keeps its default-branch identity untouched.

    Returns ``(docs, proposed, skipped)``: ``docs`` is ready for
    ``plan_feature_index``, ``proposed`` maps a proposed slug to
    ``(pr_number, pr_url)``, ``skipped`` is one ``"path: message"`` string per
    doc (from either side) that failed to parse, for the caller to log.
    """
    docs_by_slug: dict[str, tuple[str, FeatureDoc]] = {}
    main_text_by_slug: dict[str, str] = {}
    skipped: list[str] = []
    for path, text in main_docs:
        try:
            doc = parse_feature_doc(text, path=path)
        except FeatureDocError as e:
            skipped.append(f"{path}: {e}")
            continue
        docs_by_slug[doc.slug] = (path, doc)
        main_text_by_slug[doc.slug] = text

    proposed: dict[str, tuple[int | None, str | None]] = {}
    for path, text in branch_docs:
        try:
            doc = parse_feature_doc(text, path=path)
        except FeatureDocError as e:
            skipped.append(f"{path}: {e}")
            continue
        if main_text_by_slug.get(doc.slug) == text:
            continue  # inherited unchanged — not this PR's doing
        docs_by_slug[doc.slug] = (path, doc)
        proposed[doc.slug] = (pr_number, pr_url)

    return list(docs_by_slug.values()), proposed, skipped
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=$PWD/src python -m pytest server/tests/test_pm_feature_index.py -v`
Expected: PASS (all tests in the file, including the pre-existing ones)

- [ ] **Step 5: Commit**

```bash
git add server/pm/feature_index.py server/tests/test_pm_feature_index.py
git commit -m "feat(pm): add merge_branch_over_main doc-merge helper"
```

---

### Task 2: Extract `_commit_and_track` from `MapChangesService.edit()`

**Files:**
- Modify: `server/pm/map_changes.py:378-454` (the `edit()` method)
- Test: `server/tests/test_pm_map_changes_api.py` (regression only — no new tests)

**Interfaces:**
- Produces: `MapChangesService._commit_and_track(db, project, gh, repo, branch, base, base_sha, pointer, op) -> tuple[FeatureMapChange, str, str]` — returns `(pointer_row, path, sha)`. Consumed by `retire()` (Task 5) and `merge()` (Task 6), each calling it once per doc commit (N reparents + 1 retire), and by `edit()` itself (one call, behavior-preserving).

`retire`/`merge` need to make several commits (reparenting children, then retiring
the capability itself) as ONE logical action — one audit row, one inline resync at
the end — not `edit()`'s per-call audit+watermark. This task pulls `edit()`'s
commit-and-update-pointer machinery into a reusable primitive with **no behavior
change to `edit()` itself**: same steps, same order, verified by the existing full
`test_pm_map_changes_api.py` suite passing unchanged.

- [ ] **Step 1: Run the existing map-changes API suite to record the baseline**

Run: `PYTHONPATH=$PWD/src python -m pytest server/tests/test_pm_map_changes_api.py -v 2>&1 | tail -20`
Expected: PASS (all tests) — this is the regression baseline the refactor must not break.

- [ ] **Step 2: Extract `_commit_and_track`**

In `server/pm/map_changes.py`, replace the current `edit()` method (lines 378-454)
with:

```python
    async def _commit_and_track(
        self, db, project: Project, gh: GhWrite, repo: str, branch: str, base: str,
        base_sha: str, pointer: FeatureMapChange | None, op: MapOp,
    ) -> tuple[FeatureMapChange, str, str]:
        """Commit one validated op to the branch, opening/adopting the PR on
        the first commit for this product, and return the (now-current)
        pointer row, doc path and commit sha.

        Caller has already run ``validate_op``; this only refuses the shapes
        ``apply_op`` itself cannot compute (e.g. no doc at head for a
        non-create op). Shared by ``edit()`` (one op, one audit row) and
        ``retire()``/``merge()`` (several ops, one audit row for the whole
        action) — extracted from what was previously ``edit()``'s own body,
        with no behavior change to ``edit()``."""
        message = commit_message(op, project.name or project.slug or "")

        async def _read_and_apply() -> tuple[str, str, str]:
            # Re-read the branch head even when the row has one: the row may
            # be stale (a merge, or a commit by another writer) and it is
            # cheap.
            head = await asyncio.to_thread(gh.ensure_branch, branch, base_sha)
            path = feature_doc_path(op.slug)
            current, _blob = await asyncio.to_thread(gh.read_file, path, branch)
            if op.op == "create" and current is not None:
                raise MapOpError("slug_taken", _slug_taken_on_branch(op.slug, branch))
            path, text = apply_op(op, current)
            return head, path, text

        head, path, text = await _read_and_apply()
        try:
            sha = await asyncio.to_thread(gh.commit_file, branch, head, path, text, message)
        except GhWriteError as e:
            if not e.conflict:
                raise
            # Someone committed between the read and the write. Re-read the
            # head AND the doc and re-apply the op — `fileChanges.additions`
            # replaces the file wholesale, so committing the text computed at
            # the old head would silently drop their change. A second
            # conflict is the caller's to resolve (409).
            head, path, text = await _read_and_apply()
            sha = await asyncio.to_thread(gh.commit_file, branch, head, path, text, message)

        if pointer is None:
            pr = await self._adopt_or_open_pr(gh, branch, base, project)
            # Re-run the lookup immediately before inserting: "one open
            # pointer per product" is enforced by lookup, not by a partial
            # unique index (R8b-5), so two concurrent first edits would
            # otherwise both insert. Everything above is idempotent — the
            # commit is atomic and the PR is adopted, not re-opened — so the
            # loser just reuses the winner's row.
            existing = await open_pointer_row(db, project.id)
            if existing is not None:
                pointer = existing
                pointer.head_sha = sha
                if not pointer.pr_number and pr.number:
                    pointer.pr_number, pointer.pr_url = pr.number, pr.url or None
            else:
                pointer = FeatureMapChange(
                    project_id=project.id, repo=repo, branch=branch,
                    pr_number=pr.number or None, pr_url=pr.url or None,
                    status=MAP_CHANGES_STATUS_OPEN, head_sha=sha,
                    behind=pr.behind, conflicting=pr.conflicting, opened_at=self._now())
                db.add(pointer)
        else:
            pointer.head_sha = sha
        await db.flush()
        return pointer, path, sha

    async def edit(
        self, db, project: Project, op: MapOp, identity: str,
    ) -> tuple[Pointer, str, str]:
        """Apply one edit and return ``(pointer, doc path, commit sha)``."""
        # Cheap, context-free checks first: everything below costs GitHub reads.
        validate_op_shape(op)
        branch = map_changes_branch_name(project.slug or "")
        repo = await self._primary_repo(db, project)
        # Building the seam mints a GitHub App token: it reads the PEM, takes a
        # process-global lock and may POST — all blocking, so never on the loop.
        gh = await asyncio.to_thread(self._gh_factory, repo)
        pointer = await self.open_pointer(db, project)
        docs = await self.branch_docs(gh, branch, pointer=pointer)
        if op.op == "create" and any(d.slug == op.slug for d in docs):
            raise MapOpError("slug_taken", _slug_taken_on_branch(op.slug, branch))
        validate_op(op, await self.context(db, project, docs))

        base = await self._default_branch(repo)
        base_sha = await asyncio.to_thread(gh.default_branch_head, base)
        pointer, path, sha = await self._commit_and_track(
            db, project, gh, repo, branch, base, base_sha, pointer, op)

        await request_project_sync(db, project.id, self._now())
        await record_audit(
            db=db, type=f"pm.map_changes.{op.op}", identity=identity,
            detail={"slug": project.slug, "op": op.op, "capability": op.slug,
                    "path": path, "commitSha": sha, "prNumber": pointer.pr_number})
        return Pointer.from_row(pointer), path, sha
```

- [ ] **Step 3: Run the full map-changes API suite to confirm no regression**

Run: `PYTHONPATH=$PWD/src python -m pytest server/tests/test_pm_map_changes_api.py -v 2>&1 | tail -20`
Expected: PASS — identical result to Step 1 (same tests, same assertions on `argv`,
committed text, and pointer rows, since `_commit_and_track`'s body is a verbatim
extraction).

- [ ] **Step 4: Commit**

```bash
git add server/pm/map_changes.py
git commit -m "refactor(pm): extract _commit_and_track from MapChangesService.edit"
```

---

### Task 3: Wire pm-sync seams into `MapChangesService` + add `_resync_now`

**Files:**
- Modify: `server/pm/map_changes.py` (constructor, imports, new `_resync_now`)
- Modify: `server/sessions/orchestrator.py:10122-10135` (`pm_map_changes()`)
- Test: `server/tests/test_pm_map_changes_api.py`

**Interfaces:**
- Consumes: `merge_branch_over_main` (Task 1), `plan_feature_index` (existing,
  `server/pm/feature_index.py:99`), orchestrator's `_pm_sync_read_feature_docs`,
  `_pm_sync_list_feature_rows`, `_pm_sync_apply_feature_plan` (existing,
  `server/sessions/orchestrator.py:8378,8503,8515`).
- Produces: `MapChangesService._resync_now(db, project) -> None`, consumed by
  `retire()` (Task 5) and `merge()` (Task 6).

- [ ] **Step 1: Add the three optional constructor params**

In `server/pm/map_changes.py`, widen the imports:

```python
from .feature_index import FeatureDocsRead, FeatureIndexPlan, merge_branch_over_main, plan_feature_index
```

Change `MapChangesService.__init__` (currently lines 229-237):

```python
    def __init__(
        self,
        gh_factory: Callable[[str], GhWrite] = GhWrite,
        default_branch: Callable[[str], Awaitable[str]] | None = None,
        now: Callable[[], datetime] | None = None,
        read_feature_docs: Callable[..., Awaitable[FeatureDocsRead]] | None = None,
        list_feature_rows: Callable[[str], Awaitable[list]] | None = None,
        apply_feature_plan: Callable[[str, FeatureIndexPlan], Awaitable[None]] | None = None,
    ) -> None:
        self._gh_factory = gh_factory
        self._default_branch = default_branch or resolve_default_branch
        self._now = now or (lambda: datetime.now(timezone.utc))
        # Only wired by `pm_map_changes()` — a bare `MapChangesService()` (as
        # existing tests construct it for reconcile_updates/manual_update)
        # leaves these None; `_resync_now` treats that as a no-op.
        self._read_feature_docs = read_feature_docs
        self._list_feature_rows = list_feature_rows
        self._apply_feature_plan = apply_feature_plan
```

- [ ] **Step 2: Add `_resync_now`**

Add as a new method on `MapChangesService`, placed after `context()` (before the
`# -- writes --` section, since it reads before it "writes" in the retire/merge flow):

```python
    async def _resync_now(self, db, project: Project) -> None:
        """Materialize this product's just-committed doc changes into the
        Feature table synchronously, instead of leaving them to the next
        pm-sync tick (up to 300s away) — used by ``retire()``, ``merge()``,
        and triage's inline capability-create path, where the caller needs
        the DB to already reflect a doc edit before its own response
        returns. A no-op if this service was built without the three sync
        seams wired (e.g. a bare unit-test instance for
        reconcile_updates/manual_update, which never calls this)."""
        if (self._read_feature_docs is None or self._list_feature_rows is None
                or self._apply_feature_plan is None):
            return
        repo = await self._primary_repo(db, project)
        main = await self._read_feature_docs(repo)
        pointer = await self.open_pointer(db, project)
        branch_docs: list[tuple[str, str]] = []
        pr_number = pr_url = None
        if pointer is not None and pointer.status == MAP_CHANGES_STATUS_OPEN and pointer.branch:
            branch = await self._read_feature_docs(repo, pointer.branch)
            branch_docs = branch.docs
            pr_number, pr_url = pointer.pr_number, pointer.pr_url
        docs, proposed, skipped = merge_branch_over_main(
            main.docs, branch_docs, pr_number=pr_number, pr_url=pr_url)
        if skipped:
            log.warning("map_changes.resync_skipped_docs project=%s %s", project.slug, skipped)
        rows = await self._list_feature_rows(project.id)
        plan = plan_feature_index(rows, docs, read_sha=main.sha, proposed=proposed)
        await self._apply_feature_plan(project.id, plan)
```

- [ ] **Step 3: Wire the three seams in `pm_map_changes()`**

In `server/sessions/orchestrator.py`, change `pm_map_changes()` (lines 10122-10135):

```python
    def pm_map_changes(self) -> "MapChangesService":
        """The capability map-changes service (roadmap spec §9), wired to this
        orchestrator's injectable ``gh`` runner, token provider and environment
        plus its default-branch probe and pm-sync seams — so the API routes
        never construct a GitHub client themselves and a test that swaps
        ``orch._gh_run``/``orch._pm_sync_read_feature_docs`` for a recorder is
        genuinely offline, credentials included."""
        from server.pm.map_changes import MapChangesService
        from server.pm.map_changes_gh import GhWrite
        return MapChangesService(
            gh_factory=lambda repo: GhWrite(
                repo, run=self._gh_run, app_token=self._gh_token_provider,
                env=self._gh_env),
            default_branch=self._sdlc_default_branch,
            read_feature_docs=self._pm_sync_read_feature_docs,
            list_feature_rows=self._pm_sync_list_feature_rows,
            apply_feature_plan=self._pm_sync_apply_feature_plan,
        )
```

- [ ] **Step 4: Add a `_resync_now` regression test using the existing `FakeGh` fixture**

Add to `server/tests/test_pm_map_changes_api.py` a reusable fake and one test
exercising `_resync_now` indirectly is deferred to Task 5 (retire calls it) — this
step only proves the constructor wiring doesn't break existing tests, since
`_resync_now` is unreachable until `retire()`/`merge()` exist.

Run: `PYTHONPATH=$PWD/src python -m pytest server/tests/test_pm_map_changes_api.py -v 2>&1 | tail -20`
Expected: PASS — the new constructor params are all optional/defaulted, so every
existing call site (including bare `MapChangesService()` in
`test_map_update_background.py`) is unaffected.

- [ ] **Step 5: Commit**

```bash
git add server/pm/map_changes.py server/sessions/orchestrator.py
git commit -m "feat(pm): wire pm-sync seams into MapChangesService, add _resync_now"
```

---

### Task 4: Add the `retire` map op

**Files:**
- Modify: `server/pm/map_changes_ops.py`
- Test: `server/tests/test_pm_map_changes_ops.py`

**Interfaces:**
- Produces: `MapOp(op="retire", slug=...)` validated by `validate_op` and applied by
  `apply_op` (sets `doc.state = "sunset"`). Consumed by `retire()`/`merge()` (Tasks 5, 6).

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_pm_map_changes_ops.py — append

def test_retire_sets_state_to_sunset_and_keeps_everything_else():
    op = MapOp(op="retire", slug="dictation")
    validate_op(op, CTX)
    path, text = apply_op(op, DOC)
    assert path == "docs/product/features/dictation.md"
    doc = parse_feature_doc(text)
    assert doc.state == "sunset"
    assert doc.title == "Dictation" and doc.parent == "sessions"
    assert "More prose that must survive." in doc.body


def test_retire_refuses_a_missing_slug():
    with pytest.raises(MapOpError) as e:
        validate_op(MapOp(op="retire", slug="ghost"), CTX)
    assert e.value.code == "not_found"


def test_retire_preserves_route_labels():
    """Same regression class as the existing rename/reparent/summary/links
    label-preservation test — every op must dataclasses.replace() rather than
    reconstruct, or a doc's `labels:` frontmatter silently drops (fix round 1,
    Task 7)."""
    _, text = apply_op(MapOp(op="retire", slug="dictation"), LABELLED_DOC)
    doc = parse_feature_doc(text)
    assert doc.state == "sunset"
    assert doc.labels  # unchanged from LABELLED_DOC's frontmatter


def test_commit_message_names_retire():
    assert commit_message(MapOp(op="retire", slug="dictation"), "KinDash") == (
        "map(kindash): retire capability dictation")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=$PWD/src python -m pytest server/tests/test_pm_map_changes_ops.py -k retire -v`
Expected: FAIL — `retire` is not in `MAP_OPS`, so `validate_op` raises `bad_op` instead
of the expected behavior, and `commit_message` raises `KeyError`.

- [ ] **Step 3: Implement**

In `server/pm/map_changes_ops.py`:

```python
MAP_OPS: tuple[str, ...] = ("create", "rename", "reparent", "summary", "links", "retire")
```

`validate_op` already handles `retire` correctly via its existing `not_found`
branch (`elif not exists: raise MapOpError("not_found", ...)`) since `retire` is not
`create` — no change needed there beyond the `MAP_OPS` tuple membership check.

In `apply_op`, add a branch before the final `else`:

```python
    elif op.op == "retire":
        doc = replace(doc, state="sunset")
```

In `commit_message`, add to the verb dict:

```python
    verb = {"create": "add capability", "rename": "rename capability",
            "reparent": "move capability", "summary": "update summary of",
            "links": "update links of", "retire": "retire capability"}[op.op]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=$PWD/src python -m pytest server/tests/test_pm_map_changes_ops.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Commit**

```bash
git add server/pm/map_changes_ops.py server/tests/test_pm_map_changes_ops.py
git commit -m "feat(pm): add retire map op (state=sunset)"
```

---

### Task 5: `MapChangesService.retire()` + API route

**Files:**
- Modify: `server/pm/map_changes.py`
- Modify: `server/api/pm.py`
- Test: `server/tests/test_pm_map_changes_api.py`

**Interfaces:**
- Consumes: `_commit_and_track` (Task 2), `_resync_now` (Task 3), `retire` op (Task 4).
- Produces: `MapChangesService.retire(db, project, slug, identity) -> Pointer`;
  route `POST /api/pm/projects/{slug}/map-changes/retire`.

- [ ] **Step 1: Add two small private helpers**

In `server/pm/map_changes.py`, add near `_primary_repo`:

```python
    async def _feature_by_slug(self, db, project: Project, slug: str) -> Feature | None:
        return (await db.execute(select(Feature).where(
            Feature.project_id == project.id, Feature.slug == slug,
            Feature.orphaned.isnot(True)))).scalars().first()

    async def _guard_no_dependents(self, db, target: Feature, slug: str) -> None:
        """Refuse to retire/fold away a capability that initiatives or issues
        are still classified onto — the caller re-maps them first (or uses
        ``merge``, which re-points them automatically)."""
        has_work_item = (await db.execute(select(WorkItem.id).where(
            WorkItem.feature_id == target.id).limit(1))).first() is not None
        has_initiative = (await db.execute(select(InitiativeFeature.id).where(
            InitiativeFeature.feature_id == target.id).limit(1))).first() is not None
        if has_work_item or has_initiative:
            raise MapOpError(
                "has_dependents",
                f"capability '{slug}' still has initiatives or issues mapped to it; "
                "re-map them first, or use merge")
```

- [ ] **Step 2: Add `retire()`**

```python
    async def retire(self, db, project: Project, slug: str, identity: str) -> Pointer:
        """Retire a capability: refuse while it still has WorkItem/
        InitiativeFeature dependents, auto-reparent its live children to its
        own parent, commit ``retire`` (state=sunset) for the capability
        itself, then resync this product inline so the tree and state are
        consistent before returning."""
        target = await self._feature_by_slug(db, project, slug)
        if target is None:
            raise MapOpError("not_found", f"capability '{slug}' does not exist")
        await self._guard_no_dependents(db, target, slug)
        children = (await db.execute(select(Feature).where(
            Feature.parent_feature_id == target.id,
            Feature.orphaned.isnot(True)))).scalars().all()

        branch = map_changes_branch_name(project.slug or "")
        repo = await self._primary_repo(db, project)
        gh = await asyncio.to_thread(self._gh_factory, repo)
        pointer = await self.open_pointer(db, project)
        docs = await self.branch_docs(gh, branch, pointer=pointer)
        ctx = await self.context(db, project, docs)
        grandparent_slug = ctx.parents.get(target.slug or "")
        validate_op(MapOp(op="retire", slug=slug), ctx)

        base = await self._default_branch(repo)
        base_sha = await asyncio.to_thread(gh.default_branch_head, base)
        reparented = 0
        for child in children:
            validate_op(MapOp(op="reparent", slug=child.slug, parent_slug=grandparent_slug), ctx)
            pointer, _path, _sha = await self._commit_and_track(
                db, project, gh, repo, branch, base, base_sha, pointer,
                MapOp(op="reparent", slug=child.slug, parent_slug=grandparent_slug))
            reparented += 1
        pointer, path, sha = await self._commit_and_track(
            db, project, gh, repo, branch, base, base_sha, pointer,
            MapOp(op="retire", slug=slug))

        await self._resync_now(db, project)
        await record_audit(
            db=db, type="pm.map_changes.retire", identity=identity,
            detail={"slug": project.slug, "capability": slug,
                    "childrenReparented": reparented, "commitSha": sha,
                    "prNumber": pointer.pr_number})
        return Pointer.from_row(pointer)
```

- [ ] **Step 3: Add the API route**

In `server/api/pm.py`, near `map_changes_edit` (after `MapChangeBody`), add:

```python
class MapChangesRetireBody(BaseModel):
    slug: str


@router.post("/projects/{slug}/map-changes/retire", status_code=status.HTTP_200_OK)
async def map_changes_retire(
    slug: str,
    body: MapChangesRetireBody,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Retire a capability (capability-management spec). Refuses (422
    ``has_dependents``) while it still has initiative/issue dependents;
    auto-reparents live children to its own parent."""
    p = await _load_project(db, slug)
    svc = request.app.state.sessions.pm_map_changes()
    try:
        pointer = await svc.retire(db, p, body.slug, identity)
    except (MapOpError, NoPrimaryRepo, GhWriteError, subprocess.TimeoutExpired) as e:
        raise _map_changes_http(e) from e
    return {"pointer": pointer.to_dict()}
```

- [ ] **Step 4: Write the API test**

Add to `server/tests/test_pm_map_changes_api.py`. This reuses the file's existing
`_client`/`_seed`/`FakeGh`/`_committed` conventions. `_all_committed` generalizes the
existing `_committed(gh, index)` (line 438) over the whole call history, and
`_stub_pm_sync` wires the orchestrator's doc-read seam to replay it — `_pm_sync_list_feature_rows`/`_pm_sync_apply_feature_plan` are left real (DB-only, already
pointed at the test DB via `CCSWITCH_DB_URL`):

```python
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


def test_retire_refuses_with_live_dependents():
    gh = FakeGh([])
    with _seeded(gh) as (c, db_url):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from server.db.models import Feature, InitiativeFeature, Initiative, Project

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

        r = c.post(f"/api/pm/projects/{SLUG}/map-changes/retire", json={"slug": "dictation"})
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["error"] == "has_dependents"


def test_retire_reparents_children_and_syncs_the_db_inline():
    gh = FakeGh(_retire_answers())  # implementer: build from _first_create_answers()'s
                                     # shape — one ensure_branch/read_file/commit per op,
                                     # see test_create_opens_the_branch_the_pr_and_the_pointer_row
                                     # for the exact per-op answer sequence to mirror.
    with _seeded(gh) as (c, db_url, orch):
        _stub_pm_sync(orch, gh, {
            f"{DOCS}/sessions.md": ROOT_DOC, f"{DOCS}/dictation.md": CHILD_DOC})
        r = c.post(f"/api/pm/projects/{SLUG}/map-changes/retire", json={"slug": "sessions"})
        assert r.status_code == 200, r.text

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
```

The `_seeded`/`_client` context managers in this file currently `yield c` only
(`TestClient`) — this task's tests also need the underlying `db_url` (for a
synchronous verification session) and `orch` (for `_stub_pm_sync`). **Implementer:
before writing these two tests, check whether `_seeded`/`_client` already yield a
richer tuple** (the fixture may have grown since the research pass that fed this
plan); if not, extend `_client`'s `yield c` to `yield c, db_url, orch` and update
`_seeded`'s own `yield` and its two current call sites accordingly, since both new
tests depend on it. `_retire_answers()` needs a matching FakeGh answer queue —
build it from `_first_create_answers()`'s style, one `(stdout, 0, "")` tuple per
`ensure_branch`/`read_file`/`commit_file` GitHub call `_commit_and_track` makes (two
calls total for `test_retire_refuses_with_live_dependents`'s sibling test since
`sessions` has one child `dictation`: one reparent commit + one retire commit — run
the test once first to see the actual `FakeGh` "ran out of answers" error, which
names how many calls were attempted, then size the queue to match, matching the
existing convention other tests in this file already use for multi-op sequences).

- [ ] **Step 5: Run the new tests, iterating on the `FakeGh` answer queue until they pass**

Run: `PYTHONPATH=$PWD/src python -m pytest server/tests/test_pm_map_changes_api.py -k retire -v`
Expected: PASS

- [ ] **Step 6: Run the full map-changes API suite**

Run: `PYTHONPATH=$PWD/src python -m pytest server/tests/test_pm_map_changes_api.py -v 2>&1 | tail -20`
Expected: PASS (all tests, including Task 2's regression baseline)

- [ ] **Step 7: Commit**

```bash
git add server/pm/map_changes.py server/api/pm.py server/tests/test_pm_map_changes_api.py
git commit -m "feat(pm): add capability retire with dependents guard and API route"
```

- [ ] **Step 8: Open PR 1**

```bash
git fetch origin
git push -u origin HEAD
gh pr create --base master --title "feat(pm): capability retire (map op + service + API)" \
  --body-file /tmp/pr1-body.md
gh pr ready <number>
gh pr merge <number> --squash --auto
```

(Write the PR body to `/tmp/pr1-body.md` first, summarizing: new `retire` map op,
`_commit_and_track` extraction, pm-sync seams wired into `MapChangesService`, the
dependents guard, auto-reparenting of children, and the new route — link the spec
at `docs/superpowers/specs/2026-09-22-capability-management-design.md`. End with the
`Co-Authored-By` trailer.)

---

## PR 2 — Merge

### Task 6: Rename `_is_ancestor` → `is_ancestor` (second call site)

**Files:**
- Modify: `server/pm/map_changes_ops.py`
- Test: `server/tests/test_pm_map_changes_ops.py` (no behavior change — existing
  tests must still pass; this is a pure rename)

**Interfaces:**
- Produces: `is_ancestor(ctx, candidate, of) -> bool` (was `_is_ancestor`), consumed
  by Task 7's `merge()`.

`_is_ancestor` is currently private to `map_changes_ops.py`, used only by its own
`validate_op`. `merge()` needs the same cycle check (is the merge target a
descendant of the source?), so it is no longer module-private — drop the leading
underscore rather than importing a "private" name into `map_changes.py`.

- [ ] **Step 1: Rename in `map_changes_ops.py`**

Change the definition (currently lines 55-65) and its one call site inside
`validate_op` (currently line 94) from `_is_ancestor` to `is_ancestor`. No behavior
change — same body, same tests.

- [ ] **Step 2: Run the existing tests to confirm no regression**

Run: `PYTHONPATH=$PWD/src python -m pytest server/tests/test_pm_map_changes_ops.py -v`
Expected: PASS (unchanged — this is a pure rename)

- [ ] **Step 3: Commit**

```bash
git add server/pm/map_changes_ops.py
git commit -m "refactor(pm): rename _is_ancestor to is_ancestor (merge() needs it too)"
```

---

### Task 7: `MapChangesService.merge()` + API route

**Files:**
- Modify: `server/pm/map_changes.py`
- Modify: `server/api/pm.py`
- Test: `server/tests/test_pm_map_changes_api.py`

**Interfaces:**
- Consumes: `_commit_and_track` (Task 2), `_resync_now` (Task 3), `retire`/`reparent`
  ops (Task 4, existing), `is_ancestor` (Task 6), `_feature_by_slug` (Task 5).
- Produces: `MapChangesService.merge(db, project, from_slug, into_slug, identity) ->
  Pointer`; route `POST /api/pm/projects/{slug}/map-changes/merge`.

- [ ] **Step 1: Add `merge()`**

In `server/pm/map_changes.py`, widen the `sqlalchemy` import at the top of the file
(it already imports `delete, or_, select, update` per the existing `cleanup_after_abandon`
— confirm `update` is present; it is) and add:

```python
    async def merge(
        self, db, project: Project, from_slug: str, into_slug: str, identity: str,
    ) -> Pointer:
        """Fold ``from_slug`` into ``into_slug``: repoint every WorkItem/
        InitiativeFeature/FeatureLink/TriageProposal reference, reparent
        ``from_slug``'s children onto ``into_slug``, retire ``from_slug``,
        resync inline. ``into_slug`` must not be ``from_slug`` itself or one
        of its descendants — a merge target below the source would cycle once
        the source's other children reparent onto it."""
        if from_slug == into_slug:
            raise MapOpError("bad_merge", "a capability cannot be merged into itself")
        src = await self._feature_by_slug(db, project, from_slug)
        if src is None:
            raise MapOpError("not_found", f"capability '{from_slug}' does not exist")
        dst = await self._feature_by_slug(db, project, into_slug)
        if dst is None:
            raise MapOpError("not_found", f"capability '{into_slug}' does not exist")

        branch = map_changes_branch_name(project.slug or "")
        repo = await self._primary_repo(db, project)
        gh = await asyncio.to_thread(self._gh_factory, repo)
        pointer = await self.open_pointer(db, project)
        docs = await self.branch_docs(gh, branch, pointer=pointer)
        ctx = await self.context(db, project, docs)
        if is_ancestor(ctx, into_slug, from_slug):
            raise MapOpError("parent_cycle",
                              f"'{into_slug}' is '{from_slug}' or one of its descendants")

        moved_work_items = (await db.execute(update(WorkItem).where(
            WorkItem.feature_id == src.id).values(feature_id=dst.id))).rowcount or 0

        existing_pairs = {
            row.initiative_id for row in (await db.execute(select(InitiativeFeature).where(
                InitiativeFeature.feature_id == dst.id))).scalars().all()}
        src_links = (await db.execute(select(InitiativeFeature).where(
            InitiativeFeature.feature_id == src.id))).scalars().all()
        moved_initiatives = 0
        for row in src_links:
            if row.initiative_id in existing_pairs:
                if row.is_primary:
                    await db.execute(update(InitiativeFeature).where(
                        InitiativeFeature.initiative_id == row.initiative_id,
                        InitiativeFeature.feature_id == dst.id).values(is_primary=True))
                await db.delete(row)
            else:
                row.feature_id = dst.id
            moved_initiatives += 1
        await db.flush()  # the FeatureLink dedup probes below must see these
                           # adds/deletes, not a stale read

        moved_links = 0
        for side, other_attr in (("src_feature_id", "dst_feature_id"),
                                  ("dst_feature_id", "src_feature_id")):
            rows = (await db.execute(select(FeatureLink).where(
                getattr(FeatureLink, side) == src.id))).scalars().all()
            for link in rows:
                other = getattr(link, other_attr)
                dup = (await db.execute(select(FeatureLink.id).where(
                    getattr(FeatureLink, side) == dst.id,
                    FeatureLink.kind == link.kind,
                    getattr(FeatureLink, other_attr) == other).limit(1))).first()
                if dup is not None:
                    await db.delete(link)
                else:
                    setattr(link, side, dst.id)
                await db.flush()  # each dedup probe must see prior iterations
                moved_links += 1

        await db.execute(update(TriageProposal).where(
            TriageProposal.proposed_feature_id == src.id).values(proposed_feature_id=dst.id))

        children = (await db.execute(select(Feature).where(
            Feature.parent_feature_id == src.id,
            Feature.orphaned.isnot(True)))).scalars().all()

        base = await self._default_branch(repo)
        base_sha = await asyncio.to_thread(gh.default_branch_head, base)
        reparented = 0
        for child in children:
            validate_op(MapOp(op="reparent", slug=child.slug, parent_slug=into_slug), ctx)
            pointer, _path, _sha = await self._commit_and_track(
                db, project, gh, repo, branch, base, base_sha, pointer,
                MapOp(op="reparent", slug=child.slug, parent_slug=into_slug))
            reparented += 1
        validate_op(MapOp(op="retire", slug=from_slug), ctx)
        pointer, path, sha = await self._commit_and_track(
            db, project, gh, repo, branch, base, base_sha, pointer,
            MapOp(op="retire", slug=from_slug))

        await self._resync_now(db, project)
        await record_audit(
            db=db, type="pm.map_changes.merge", identity=identity,
            detail={"slug": project.slug, "from": from_slug, "into": into_slug,
                    "workItems": moved_work_items, "initiatives": moved_initiatives,
                    "links": moved_links, "childrenReparented": reparented,
                    "commitSha": sha, "prNumber": pointer.pr_number})
        return Pointer.from_row(pointer)
```

- [ ] **Step 2: Add the API route**

In `server/api/pm.py`, near `map_changes_retire`:

```python
class MapChangesMergeBody(BaseModel):
    fromSlug: str
    intoSlug: str


@router.post("/projects/{slug}/map-changes/merge", status_code=status.HTTP_200_OK)
async def map_changes_merge(
    slug: str,
    body: MapChangesMergeBody,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Fold one capability into another (capability-management spec):
    repoints every initiative/issue/link/proposal reference, reparents the
    source's children onto the target, retires the source."""
    p = await _load_project(db, slug)
    svc = request.app.state.sessions.pm_map_changes()
    try:
        pointer = await svc.merge(db, p, body.fromSlug, body.intoSlug, identity)
    except (MapOpError, NoPrimaryRepo, GhWriteError, subprocess.TimeoutExpired) as e:
        raise _map_changes_http(e) from e
    return {"pointer": pointer.to_dict()}
```

- [ ] **Step 3: Write the tests**

Add to `server/tests/test_pm_map_changes_api.py`, reusing `_stub_pm_sync`/`_all_committed`
from Task 5:

```python
def test_merge_refuses_a_cycle():
    gh = FakeGh([])
    with _seeded(gh) as (c, db_url, orch):
        # dictation is a CHILD of sessions — merging sessions into its own
        # child would cycle once sessions' other children reparent onto it.
        r = c.post(f"/api/pm/projects/{SLUG}/map-changes/merge",
                   json={"fromSlug": "sessions", "intoSlug": "dictation"})
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["error"] == "parent_cycle"


def test_merge_repoints_work_items_and_initiatives_and_retires_the_source():
    gh = FakeGh(_merge_answers())  # implementer: one retire commit only —
                                     # dictation (the merge source here) has no
                                     # children in the seed fixture, so this is
                                     # a single ensure_branch/read_file/commit
                                     # sequence; mirror the single-op answer
                                     # shape from test_pm_map_changes_ops.py's
                                     # sibling API tests.
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

        r = c.post(f"/api/pm/projects/{SLUG}/map-changes/merge",
                   json={"fromSlug": "dictation", "intoSlug": "sessions"})
        assert r.status_code == 200, r.text
        body = r.json()["pointer"]
        assert body["status"] == "open"

        with Session() as s:
            proj = s.query(Project).filter_by(slug=SLUG).one()
            sessions_row = s.query(Feature).filter_by(project_id=proj.id, slug="dictation").one()
            assert sessions_row.state == "sunset"
            wi = s.query(WorkItem).filter_by(repo="o/r", number=1, kind="issue").one()
            sessions_cap = s.query(Feature).filter_by(project_id=proj.id, slug="sessions").one()
            assert wi.feature_id == sessions_cap.id
            link = s.query(InitiativeFeature).filter_by(initiative_id=init.id).one()
            assert link.feature_id == sessions_cap.id and link.is_primary is True
```

- [ ] **Step 4: Run the tests, iterating on the `FakeGh` answer queue until they pass**

Run: `PYTHONPATH=$PWD/src python -m pytest server/tests/test_pm_map_changes_api.py -k merge -v`
Expected: PASS

- [ ] **Step 5: Run the full map-changes API suite**

Run: `PYTHONPATH=$PWD/src python -m pytest server/tests/test_pm_map_changes_api.py -v 2>&1 | tail -20`
Expected: PASS (all tests)

- [ ] **Step 6: Commit**

```bash
git add server/pm/map_changes.py server/api/pm.py server/tests/test_pm_map_changes_api.py
git commit -m "feat(pm): add capability merge (repoint dependents, retire source)"
```

- [ ] **Step 7: Open PR 2** (stacked on PR 1's branch, or on `master` if PR 1 has
  already merged by this point — check `gh pr view <PR1-number>` first)

```bash
gh pr create --base master --title "feat(pm): capability merge (fold one into another)" \
  --body-file /tmp/pr2-body.md
gh pr ready <number>
gh pr merge <number> --squash --auto
```

---

## PR 3 — Products → Capabilities tab

### Task 8: API client + hooks

**Files:**
- Modify: `web/src/api/pm.ts`
- Modify: `web/src/api/hooks.ts`
- Test: none (thin wrappers; covered by Task 12's component tests)

**Interfaces:**
- Produces: `retireCapability(slug, body: RetireCapabilityInput) ->
  Promise<MapChangeResult>`; `mergeCapabilities(slug, body: MergeCapabilitiesInput) ->
  Promise<MapChangeResult>`; `useRetireCapability(slug)`, `useMergeCapabilities(slug)`
  hooks. Consumed by Tasks 9-11.

- [ ] **Step 1: Add the API client functions**

In `web/src/api/pm.ts`, near `editMapChanges` (line 307):

```typescript
export interface RetireCapabilityInput {
  slug: string
}

export interface MergeCapabilitiesInput {
  fromSlug: string
  intoSlug: string
}

export function retireCapability(
  slug: string, input: RetireCapabilityInput,
): Promise<MapChangeResult> {
  return api.post<MapChangeResult>(
    `/pm/projects/${encodeURIComponent(slug)}/map-changes/retire`, input)
}

export function mergeCapabilities(
  slug: string, input: MergeCapabilitiesInput,
): Promise<MapChangeResult> {
  return api.post<MapChangeResult>(
    `/pm/projects/${encodeURIComponent(slug)}/map-changes/merge`, input)
}
```

(`MapChangeResult` is the existing type at `pm.ts:294-298` — both new endpoints
return the same `{pointer, path?, commitSha?}` shape, though `path`/`commitSha`
reflect the LAST commit made, not the whole action; the `CapabilitiesTab` UI (Task
9) only reads `pointer`.)

- [ ] **Step 2: Add the hooks**

In `web/src/api/hooks.ts`, near `useEditMapChanges` (line 2200):

```typescript
export function useRetireCapability(slug: string) {
  const qc = useQueryClient()
  return useMutation<MapChangeResult, ApiRequestError, RetireCapabilityInput>({
    mutationFn: (input) => retireCapability(slug, input),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: QK.roadmapMap })
      void qc.invalidateQueries({ queryKey: QK.project(slug) })
    },
    meta: PM_MUTATION_META,
  })
}

export function useMergeCapabilities(slug: string) {
  const qc = useQueryClient()
  return useMutation<MapChangeResult, ApiRequestError, MergeCapabilitiesInput>({
    mutationFn: (input) => mergeCapabilities(slug, input),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: QK.roadmapMap })
      void qc.invalidateQueries({ queryKey: QK.project(slug) })
    },
    meta: PM_MUTATION_META,
  })
}
```

Add `retireCapability, mergeCapabilities, type RetireCapabilityInput, type
MergeCapabilitiesInput` to `hooks.ts`'s existing import from `./pm`.

- [ ] **Step 3: Typecheck**

Run: `(cd web && npm run typecheck)`
Expected: no errors

- [ ] **Step 4: Commit**

```bash
git add web/src/api/pm.ts web/src/api/hooks.ts
git commit -m "feat(web): add retire/merge capability API client and hooks"
```

---

### Task 9: `CapabilitiesTab.tsx` — list + retire action

**Files:**
- Create: `web/src/features/products/CapabilitiesTab.tsx`
- Test: `web/src/features/products/CapabilitiesTab.test.tsx`

**Interfaces:**
- Consumes: `useRetireCapability` (Task 8), `CapabilityNode`/`flattenCapabilities`
  (existing, `web/src/api/roadmap.ts`, `web/src/features/roadmap/lookup.ts`),
  `CAPABILITY`/`CAPABILITY_STATE_LABEL` (existing, `web/src/features/products/vocab.ts`).
- Produces: `<CapabilitiesTab product={ProductNode} capabilities={CapabilityNode[]} />`,
  consumed by Task 12's `ProductDetail.tsx` wiring. Also produces `<MergeCapabilityDialog>`
  and `<RemapDependentsDialog>` as later tasks add them to the same file.

- [ ] **Step 1: Write the failing test**

```typescript
// web/src/features/products/CapabilitiesTab.test.tsx
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { CapabilitiesTab } from './CapabilitiesTab'
import * as hooks from '@/api/hooks'
import type { CapabilityNode } from '@/api/roadmap'

vi.mock('@/api/hooks')

const CAPS: CapabilityNode[] = [
  {
    id: 'f1', slug: 'sessions', title: 'Sessions', state: 'live', summary: null,
    docPath: null, commitSha: 'abc', prNumber: null, parentSlug: null, tier: 'core',
    counts: { initiatives: 0, workItems: 0 }, links: [], initiatives: [],
    children: [
      {
        id: 'f2', slug: 'dictation', title: 'Dictation', state: 'live', summary: null,
        docPath: null, commitSha: 'def', prNumber: null, parentSlug: 'sessions', tier: 'core',
        counts: { initiatives: 1, workItems: 2 }, links: [], initiatives: [], children: [],
      },
    ],
  },
]

describe('CapabilitiesTab', () => {
  it('lists every capability with its dependent counts', () => {
    vi.mocked(hooks.useRetireCapability).mockReturnValue({
      mutateAsync: vi.fn(), isPending: false,
    } as unknown as ReturnType<typeof hooks.useRetireCapability>)
    render(<CapabilitiesTab product={{ slug: 'kindash' } as never} capabilities={CAPS} />)
    expect(screen.getByText('Sessions')).toBeInTheDocument()
    expect(screen.getByText('Dictation')).toBeInTheDocument()
    expect(screen.getByText(/1 initiative/i)).toBeInTheDocument()
    expect(screen.getByText(/2 issues/i)).toBeInTheDocument()
  })

  it('retiring a capability with dependents shows the server refusal', async () => {
    const mutateAsync = vi.fn().mockRejectedValue({
      response: { data: { detail: { error: 'has_dependents', message: 'nope' } } },
    })
    vi.mocked(hooks.useRetireCapability).mockReturnValue({
      mutateAsync, isPending: false,
    } as unknown as ReturnType<typeof hooks.useRetireCapability>)
    render(<CapabilitiesTab product={{ slug: 'kindash' } as never} capabilities={CAPS} />)
    fireEvent.click(screen.getAllByRole('button', { name: /retire/i })[1]) // dictation
    fireEvent.click(screen.getByRole('button', { name: /confirm/i }))
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledWith({ slug: 'dictation' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/nope/i)
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `(cd web && npx vitest run src/features/products/CapabilitiesTab.test.tsx)`
Expected: FAIL — `CapabilitiesTab.tsx` does not exist yet.

- [ ] **Step 3: Implement**

```tsx
// web/src/features/products/CapabilitiesTab.tsx
import { useState } from 'react'
import { useRetireCapability } from '@/api/hooks'
import type { CapabilityNode, ProductNode } from '@/api/roadmap'
import { flattenCapabilities } from '@/features/roadmap/lookup'
import { extractMutationErrorMessage } from '@/api/errors'
import { Button } from '@/ui/Button'
import { CAPABILITY, CAPABILITY_STATE_LABEL } from './vocab'

interface Props {
  product: ProductNode
  capabilities: CapabilityNode[]
}

/** Products → <product> → Capabilities (capability-management spec, PR 3):
 *  list every capability with its dependent counts, retire one (server
 *  guards on live dependents), or merge one into another (Task 10) and
 *  re-map its dependents (Task 11). */
export function CapabilitiesTab({ product, capabilities }: Props) {
  const rows = flattenCapabilities(capabilities)
  const retire = useRetireCapability(product.slug)
  const [confirming, setConfirming] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const doRetire = async (slug: string) => {
    setError(null)
    try {
      await retire.mutateAsync({ slug })
      setConfirming(null)
    } catch (e) {
      setError(extractMutationErrorMessage(e, `Could not retire ${slug}`))
    }
  }

  return (
    <div className="flex flex-col gap-4">
      {error && <div role="alert" className="text-sm text-danger">{error}</div>}
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-xs uppercase text-ink-muted">
            <th className="py-1">{CAPABILITY.singular}</th>
            <th className="py-1">State</th>
            <th className="py-1">Dependents</th>
            <th className="py-1" />
          </tr>
        </thead>
        <tbody>
          {rows.map(({ node, depth }) => (
            <tr key={node.id} className="border-t border-line">
              <td className="py-2" style={{ paddingLeft: depth * 16 }}>
                {node.title ?? node.slug}
              </td>
              <td className="py-2">{CAPABILITY_STATE_LABEL[node.state ?? ''] ?? node.state}</td>
              <td className="py-2">
                {node.counts.initiatives} initiative{node.counts.initiatives === 1 ? '' : 's'},{' '}
                {node.counts.workItems} issue{node.counts.workItems === 1 ? '' : 's'}
              </td>
              <td className="py-2 text-right">
                {confirming === node.slug ? (
                  <span className="flex items-center justify-end gap-2">
                    <span className="text-xs text-ink-muted">Retire {node.title}?</span>
                    <Button size="sm" variant="danger" onClick={() => void doRetire(node.slug)}
                            disabled={retire.isPending}>
                      Confirm
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => setConfirming(null)}>
                      Cancel
                    </Button>
                  </span>
                ) : (
                  <Button size="sm" variant="ghost" onClick={() => setConfirming(node.slug)}>
                    Retire
                  </Button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
```

`CapabilityNode.counts` currently has fields (per the earlier research pull) that
need confirming against the real interface in `web/src/api/roadmap.ts` before this
compiles — **implementer: read `CapabilityCounts`'s exact field names there first**
(the plan's research pass saw `counts: CapabilityCounts` but not that sub-interface's
fields; adjust `node.counts.initiatives`/`node.counts.workItems` above to match
whatever it actually exports, e.g. it may already be `initiativeCount`/`workItemCount`).

- [ ] **Step 4: Run the test to verify it passes**

Run: `(cd web && npx vitest run src/features/products/CapabilitiesTab.test.tsx)`
Expected: PASS

- [ ] **Step 5: Typecheck**

Run: `(cd web && npm run typecheck)`
Expected: no errors (fix the `counts` field-name mismatch from Step 3 here if typecheck
flags it)

- [ ] **Step 6: Commit**

```bash
git add web/src/features/products/CapabilitiesTab.tsx web/src/features/products/CapabilitiesTab.test.tsx
git commit -m "feat(web): add CapabilitiesTab with list + retire action"
```

---

### Task 10: Merge action UI

**Files:**
- Modify: `web/src/features/products/CapabilitiesTab.tsx`
- Modify: `web/src/features/products/CapabilitiesTab.test.tsx`

**Interfaces:**
- Consumes: `useMergeCapabilities` (Task 8).
- Produces: a "Merge into…" action per row, opening an inline target picker.

- [ ] **Step 1: Write the failing test**

```typescript
// append to CapabilitiesTab.test.tsx

it('merging a capability shows the repointed counts from the response', async () => {
  const mutateAsync = vi.fn().mockResolvedValue({
    pointer: { branch: 'pm/map-changes-kindash', prNumber: 9, prUrl: null, status: 'open',
               openedAt: null, headSha: 'x', behind: false, conflicting: false },
  })
  vi.mocked(hooks.useMergeCapabilities).mockReturnValue({
    mutateAsync, isPending: false,
  } as unknown as ReturnType<typeof hooks.useMergeCapabilities>)
  vi.mocked(hooks.useRetireCapability).mockReturnValue({
    mutateAsync: vi.fn(), isPending: false,
  } as unknown as ReturnType<typeof hooks.useRetireCapability>)
  render(<CapabilitiesTab product={{ slug: 'kindash' } as never} capabilities={CAPS} />)
  fireEvent.click(screen.getAllByRole('button', { name: /merge into/i })[1]) // dictation
  fireEvent.change(screen.getByRole('combobox', { name: /merge dictation into/i }), {
    target: { value: 'sessions' },
  })
  fireEvent.click(screen.getByRole('button', { name: /confirm merge/i }))
  await waitFor(() => expect(mutateAsync).toHaveBeenCalledWith({
    fromSlug: 'dictation', intoSlug: 'sessions',
  }))
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `(cd web && npx vitest run src/features/products/CapabilitiesTab.test.tsx)`
Expected: FAIL — no "Merge into" button exists yet.

- [ ] **Step 3: Implement**

Add to `CapabilitiesTab.tsx`: a `merging: string | null` state (the slug being
merged), a `<Select>` populated from `rows` excluding the row itself and its own
descendants (client-side filter mirroring `EditCapabilityDialog`'s
`selfAndDescendants()` guard — server still validates `parent_cycle`), and a
"Confirm merge" button calling `useMergeCapabilities(product.slug).mutateAsync({
fromSlug, intoSlug })`. Follow the exact same confirm/cancel/error pattern already
built for retire in Task 9 (a `merging`/`mergeError` pair of state variables next to
`confirming`/`error`), rendered as an alternate action in the same actions cell.

- [ ] **Step 4: Run the test to verify it passes**

Run: `(cd web && npx vitest run src/features/products/CapabilitiesTab.test.tsx)`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add web/src/features/products/CapabilitiesTab.tsx web/src/features/products/CapabilitiesTab.test.tsx
git commit -m "feat(web): add merge action to CapabilitiesTab"
```

---

### Task 11: Re-map action UI

**Files:**
- Create: `web/src/features/products/RemapDependentsDialog.tsx`
- Modify: `web/src/features/products/CapabilitiesTab.tsx` (opens the dialog per row)
- Test: `web/src/features/products/RemapDependentsDialog.test.tsx`

**Interfaces:**
- Consumes: `useUpdateInitiative` (existing, `web/src/api/hooks.ts:1743`,
  `{slug, input: {primaryCapabilityId?, capabilityIds?}}`), `useUpdateOverlay`
  (existing, `web/src/api/hooks.ts:2540`, `{itemId, edit: {featureId?}}`).
- Produces: `<RemapDependentsDialog capability={CapabilityNode} allCapabilities=
  {CapabilityNode[]} initiatives={{slug, title}[]} workItems={{id, title}[]}
  open onClose />`, opened from `CapabilitiesTab.tsx`'s new "Re-map" action per row.

Per the design decision, no new bulk endpoint — this fires the existing single-item
`useUpdateInitiative`/`useUpdateOverlay` mutations once per selected item.

- [ ] **Step 1: Write the failing test**

```typescript
// web/src/features/products/RemapDependentsDialog.test.tsx
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { RemapDependentsDialog } from './RemapDependentsDialog'
import * as hooks from '@/api/hooks'

vi.mock('@/api/hooks')

describe('RemapDependentsDialog', () => {
  it('re-maps the selected initiatives and issues onto the target capability', async () => {
    const updateInitiative = vi.fn().mockResolvedValue({})
    const updateOverlay = vi.fn().mockResolvedValue({})
    vi.mocked(hooks.useUpdateInitiative).mockReturnValue({
      mutateAsync: updateInitiative, isPending: false,
    } as unknown as ReturnType<typeof hooks.useUpdateInitiative>)
    vi.mocked(hooks.useUpdateOverlay).mockReturnValue({
      mutateAsync: updateOverlay, isPending: false,
    } as unknown as ReturnType<typeof hooks.useUpdateOverlay>)

    render(
      <RemapDependentsDialog
        capability={{ id: 'f2', slug: 'dictation', title: 'Dictation' } as never}
        allCapabilities={[{ id: 'f1', slug: 'sessions', title: 'Sessions' } as never]}
        initiatives={[{ slug: 'init-a', title: 'Init A' }]}
        workItems={[{ id: 'wi-1', title: 'Issue 1' }]}
        open onClose={() => {}}
      />,
    )
    fireEvent.click(screen.getByRole('checkbox', { name: /init a/i }))
    fireEvent.click(screen.getByRole('checkbox', { name: /issue 1/i }))
    fireEvent.change(screen.getByRole('combobox', { name: /move to/i }), {
      target: { value: 'sessions' },
    })
    fireEvent.click(screen.getByRole('button', { name: /move selected/i }))

    await waitFor(() => {
      expect(updateInitiative).toHaveBeenCalledWith({
        slug: 'init-a', input: { capabilityIds: ['sessions'], primaryCapabilityId: 'sessions' },
      })
      expect(updateOverlay).toHaveBeenCalledWith({ itemId: 'wi-1', edit: { featureId: 'sessions' } })
    })
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `(cd web && npx vitest run src/features/products/RemapDependentsDialog.test.tsx)`
Expected: FAIL — the component does not exist yet.

- [ ] **Step 3: Implement**

```tsx
// web/src/features/products/RemapDependentsDialog.tsx
import { useState } from 'react'
import { useUpdateInitiative, useUpdateOverlay } from '@/api/hooks'
import type { CapabilityNode } from '@/api/roadmap'
import { Dialog } from '@/ui/Dialog'
import { Button } from '@/ui/Button'
import { Select } from '@/ui/Select'
import { Field } from '@/ui/Field'
import { extractMutationErrorMessage } from '@/api/errors'

interface InitiativeRow { slug: string; title: string | null }
interface WorkItemRow { id: string; title: string | null }

interface Props {
  capability: CapabilityNode
  allCapabilities: CapabilityNode[]
  initiatives: InitiativeRow[]
  workItems: WorkItemRow[]
  open: boolean
  onClose: () => void
}

/** Re-map one or more initiatives/issues off `capability` onto another one, in
 *  one action — fires the existing single-item PATCH per selected item (no
 *  new bulk endpoint, per the capability-management design). */
export function RemapDependentsDialog(
  { capability, allCapabilities, initiatives, workItems, open, onClose }: Props,
) {
  const [selectedInitiatives, setSelectedInitiatives] = useState<Set<string>>(new Set())
  const [selectedWorkItems, setSelectedWorkItems] = useState<Set<string>>(new Set())
  const [target, setTarget] = useState('')
  const [error, setError] = useState<string | null>(null)
  const updateInitiative = useUpdateInitiative()
  const updateOverlay = useUpdateOverlay()
  const moving = updateInitiative.isPending || updateOverlay.isPending

  const toggle = (set: Set<string>, setter: (s: Set<string>) => void, key: string) => {
    const next = new Set(set)
    if (next.has(key)) next.delete(key)
    else next.add(key)
    setter(next)
  }

  const moveSelected = async () => {
    setError(null)
    try {
      await Promise.all([
        ...[...selectedInitiatives].map((slug) => updateInitiative.mutateAsync({
          slug, input: { capabilityIds: [target], primaryCapabilityId: target },
        })),
        ...[...selectedWorkItems].map((id) => updateOverlay.mutateAsync({
          itemId: id, edit: { featureId: target },
        })),
      ])
      onClose()
    } catch (e) {
      setError(extractMutationErrorMessage(e, 'Could not move every selected item'))
    }
  }

  if (!open) return null
  return (
    <Dialog title={`Re-map dependents of ${capability.title ?? capability.slug}`} onClose={onClose}>
      {error && <div role="alert" className="text-sm text-danger">{error}</div>}
      <fieldset className="flex flex-col gap-1">
        <legend className="text-xs uppercase text-ink-muted">Initiatives</legend>
        {initiatives.map((i) => (
          <label key={i.slug} className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={selectedInitiatives.has(i.slug)}
                   onChange={() => toggle(selectedInitiatives, setSelectedInitiatives, i.slug)} />
            {i.title ?? i.slug}
          </label>
        ))}
      </fieldset>
      <fieldset className="flex flex-col gap-1">
        <legend className="text-xs uppercase text-ink-muted">Issues</legend>
        {workItems.map((w) => (
          <label key={w.id} className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={selectedWorkItems.has(w.id)}
                   onChange={() => toggle(selectedWorkItems, setSelectedWorkItems, w.id)} />
            {w.title ?? w.id}
          </label>
        ))}
      </fieldset>
      <Field label="Move to" htmlFor="remap-target">
        <Select id="remap-target" aria-label="Move to" value={target}
                onChange={(e) => setTarget(e.target.value)}>
          <option value="">— choose a capability —</option>
          {allCapabilities.filter((c) => c.slug !== capability.slug).map((c) => (
            <option key={c.id} value={c.slug}>{c.title ?? c.slug}</option>
          ))}
        </Select>
      </Field>
      <div className="flex justify-end gap-2">
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
        <Button
          disabled={!target || (selectedInitiatives.size === 0 && selectedWorkItems.size === 0) || moving}
          onClick={() => void moveSelected()}
        >
          Move selected
        </Button>
      </div>
    </Dialog>
  )
}
```

`Dialog`/`Field`/`Select`/`Button` import paths above match `EditCapabilityDialog.tsx`'s
existing imports — **implementer: confirm the exact paths (`@/ui/Dialog` etc.) against
that file** before finalizing, since the research pass summarized its structure but
did not capture its import lines verbatim.

- [ ] **Step 4: Run the test to verify it passes**

Run: `(cd web && npx vitest run src/features/products/RemapDependentsDialog.test.tsx)`
Expected: PASS

- [ ] **Step 5: Wire a "Re-map" action into `CapabilitiesTab.tsx`**

Add a third per-row action button ("Re-map…") that opens `RemapDependentsDialog` with
`capability={node}`, `allCapabilities={rows.map(r => r.node)}`, and `initiatives`/
`workItems` sourced from whatever prop `CapabilitiesTab` already receives for the
product's initiatives/work items (check `ProductDetail.tsx`'s existing data —
Task 12 wires `CapabilitiesTab` in and is where those props get threaded through;
if `CapabilitiesTab` doesn't yet receive initiative/work-item lists, add
`initiatives: InitiativeRow[]` and `workItems: WorkItemRow[]` to its own `Props` in
this step, filtered to `capability.slug`'s current dependents client-side before
passing to the dialog).

- [ ] **Step 6: Commit**

```bash
git add web/src/features/products/RemapDependentsDialog.tsx \
        web/src/features/products/RemapDependentsDialog.test.tsx \
        web/src/features/products/CapabilitiesTab.tsx
git commit -m "feat(web): add re-map action for capability dependents"
```

---

### Task 12: Wire the Capabilities tab into `ProductDetail.tsx`

**Files:**
- Modify: `web/src/features/roadmap/detail/ProductDetail.tsx`
- Modify: `web/src/features/products/vocab.ts`
- Modify: `docs/PAGE-TABS.md`
- Test: existing `ProductDetail` tests (regression) + full `npm run test`

**Interfaces:**
- Consumes: `CapabilitiesTab` (Tasks 9-11).

- [ ] **Step 1: Add the 7th tab**

In `ProductDetail.tsx`, change:

```typescript
const TABS = ['overview', 'in-flight', 'needs-you', 'ranked', 'dependencies', 'automation'] as const
```

to:

```typescript
const TABS = ['overview', 'in-flight', 'needs-you', 'ranked', 'dependencies', 'automation', 'capabilities'] as const
```

Add to the `items` array (after `automation`):

```typescript
{ id: 'capabilities', label: CAPABILITY.plural },
```

(`CAPABILITY.plural` — `'Capabilities'` — already exists in `vocab.ts:19-22`; reuse
it directly rather than adding a redundant `TAB_LABEL.capabilities` entry. Import
`CAPABILITY` alongside the file's existing `TAB_LABEL`/`TIER_LABEL` imports from
`../../products/vocab` if not already imported.)

Add the panel branch (after the `automation` branch):

```tsx
{tab === 'capabilities' && (
  <CapabilitiesTab product={product} capabilities={product.capabilities ?? []} />
)}
```

Import `CapabilitiesTab` from `../../products/CapabilitiesTab`.

- [ ] **Step 2: Record the tab-cap exception in `docs/PAGE-TABS.md`**

Add a row to the existing §8 "Exceptions on record" table, alongside the #2078 entry:

```markdown
| Product Capabilities | 7th tab past the 6-tab cap (§4) | capability retire/merge/re-map needs its own home, distinct from Overview (creation only) and Dependencies (cross-product links) | #<this-PR-number> |
```

- [ ] **Step 3: Run existing `ProductDetail` tests**

Run: `(cd web && npx vitest run src/features/roadmap/detail/ProductDetail.test.tsx)`
Expected: PASS (the new tab is additive; existing tab tests are unaffected)

- [ ] **Step 4: Run the FULL web test suite**

Run: `(cd web && npm run test 2>&1 | tee /tmp/web-test-capabilities-tab.log | tail -40)`
Expected: PASS — running the full suite (not just changed files) matters here since
`ProductDetail.tsx` is a shared component many other test files mock.

- [ ] **Step 5: Typecheck, lint, build**

Run: `(cd web && npm run typecheck && npm run lint && npm run build)`
Expected: all green

- [ ] **Step 6: Commit**

```bash
git add web/src/features/roadmap/detail/ProductDetail.tsx docs/PAGE-TABS.md
git commit -m "feat(web): add Products Capabilities tab (7th tab, exception recorded)"
```

- [ ] **Step 7: Open PR 3**

```bash
gh pr create --base master --title "feat(web): Products Capabilities tab" \
  --body-file /tmp/pr3-body.md
gh pr ready <number>
gh pr merge <number> --squash --auto
```

---

## PR 4 — Triage inline capability creation

### Task 13: Backend — `accept_triage` handles `edits.newCapability`

**Files:**
- Modify: `server/api/pm.py` (`accept_triage`)
- Test: `server/tests/test_pm_triage_accept.py` (or wherever `accept_triage`'s
  existing tests live — grep for `accept_triage` under `server/tests/` first, since
  the exact file name was not confirmed during research; the plan's Global
  Constraints note the same env setup every `server/tests/` run needs)

**Interfaces:**
- Consumes: `MapChangesService.edit` (existing), `MapChangesService._resync_now`
  (Task 3).
- Produces: `edits.newCapability: {title: str, slug: str} | None`, an alternative
  to `edits.featureId`, handled entirely in the route (no change to
  `apply_accept`/`triage_accept.py`, which already just reads `edits["featureId"]`
  as a plain dict key).

- [ ] **Step 1: Locate the existing test coverage for `accept_triage`**

Run: `grep -rl "accept_triage\|/triage/.*accept" server/tests/*.py`

Note the file(s) found — this task's tests extend whichever file already covers
`POST /api/pm/triage/{proposal_id}/accept`.

- [ ] **Step 2: Write the failing tests**

Add to the file found in Step 1 (adapt fixture/helper names to match that file's
existing conventions for seeding a `TriageProposal` + its `WorkItem` + `Project`):

```python
def test_accept_with_new_capability_creates_it_and_classifies_in_one_call(...):
    """... build on whatever proposal/project fixture this file already uses;
    the assertions below are the contract regardless of fixture shape."""
    r = client.post(f"/api/pm/triage/{proposal_id}/accept",
                    json={"edits": {"newCapability": {"title": "Payments", "slug": "payments"}}})
    assert r.status_code == 200, r.text
    # the work item is classified onto the newly created (now-indexed) capability
    feature = db_session.query(Feature).filter_by(project_id=project.id, slug="payments").one()
    assert feature.state == "proposed"
    work_item = db_session.query(WorkItem).filter_by(id=work_item_id).one()
    assert work_item.feature_id == feature.id


def test_accept_rejects_both_feature_id_and_new_capability(...):
    r = client.post(f"/api/pm/triage/{proposal_id}/accept",
                    json={"edits": {"featureId": existing_feature_id,
                                    "newCapability": {"title": "X", "slug": "x"}}})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "bad_edit"


def test_accept_new_capability_slug_collision_is_409(...):
    # seed an existing capability slug "payments" first
    r = client.post(f"/api/pm/triage/{proposal_id}/accept",
                    json={"edits": {"newCapability": {"title": "Payments", "slug": "payments"}}})
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "slug_taken"
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `PYTHONPATH=$PWD/src python -m pytest server/tests/<the file from Step 1> -k new_capability -v`
Expected: FAIL — `newCapability` is not read anywhere yet.

- [ ] **Step 4: Implement**

In `server/api/pm.py`'s `accept_triage` (currently lines 1417-1448), between reading
`edits` and calling `apply_accept`:

```python
    from server.pm.map_changes import MapOp, MapOpError

    edits = (body or {}).get("edits")
    new_capability = (edits or {}).get("newCapability")
    if new_capability and (edits or {}).get("featureId") is not None:
        raise HTTPException(status_code=400, detail={
            "error": "bad_edit", "message": "featureId and newCapability are mutually exclusive"})
    if new_capability:
        prop_for_project = await _load_pending_proposal(db, proposal_id)
        project = await _project_for_work_item(db, prop_for_project.work_item_id)
        svc = request.app.state.sessions.pm_map_changes()
        op = MapOp(op="create", slug=new_capability["slug"], title=new_capability["title"])
        try:
            await svc.edit(db, project, op, identity)
            await svc._resync_now(db, project)
        except MapOpError as e:
            raise _map_changes_http(e) from e
        feature = (await db.execute(select(Feature).where(
            Feature.project_id == project.id, Feature.slug == new_capability["slug"],
        ))).scalars().first()
        edits = {**(edits or {}), "featureId": feature.id if feature else None}

    await _validate_accept_edits(db, edits)
```

`_project_for_work_item` does not exist yet — **implementer: `accept_triage` needs
the proposal's owning `Project` to call `svc.edit`/`svc._resync_now`; grep
`server/api/pm.py`/`server/pm/triage_accept.py` for how the existing code already
resolves a `WorkItem`'s owning `Project`** (the repo-to-product resolution seen
elsewhere in this codebase, e.g. `owning_projects_by_repo` in
`server/pm/repo_project.py`, referenced by the PR-doctor-priority design doc in this
same repo) and use that existing helper rather than writing a new one — this is
exactly the kind of "find the existing source of truth before adding a new one"
case the repo's working norms call out. `request` needs adding as a parameter to
`accept_triage`'s signature (`request: Request`) if not already present, to reach
`request.app.state.sessions.pm_map_changes()`. Also add `request.app.state.sessions`
availability by re-checking `_map_changes_http`, `MapOp`, `MapOpError`, `Feature`
are all already imported at the top of `pm.py` (they are — `map_changes_edit`
already uses `MapOpError`, `MapOp`, `_map_changes_http`; `Feature` is imported for
`_validate_accept_edits`).

Note `_load_pending_proposal` is called TWICE if the route also calls it later for
`apply_accept`'s own load — check the existing route body from the research pull:
it already calls `prop = await _load_pending_proposal(db, proposal_id)` once,
before `edits = (body or {}).get("edits")`. **Reorder so the existing single call
to `_load_pending_proposal` is reused** (move the `new_capability` handling to
after `prop = await _load_pending_proposal(...)`, use `prop` directly instead of a
second `prop_for_project` call) rather than loading it twice.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `PYTHONPATH=$PWD/src python -m pytest server/tests/<the file from Step 1> -k new_capability -v`
Expected: PASS

- [ ] **Step 6: Run the full file's suite (regression)**

Run: `PYTHONPATH=$PWD/src python -m pytest server/tests/<the file from Step 1> -v 2>&1 | tail -30`
Expected: PASS (all tests, existing `featureId`-only accept path unchanged)

- [ ] **Step 7: Commit**

```bash
git add server/api/pm.py server/tests/<the file from Step 1>
git commit -m "feat(pm): accept a triage proposal onto a newly created capability"
```

---

### Task 14: Web — inline "+ New capability" in `ProposalDetail.tsx`

**Files:**
- Modify: `web/src/api/triage.ts` (`OverlayEdits`, `acceptProposal`)
- Modify: `web/src/features/roadmap/inbox/ProposalDetail.tsx`
- Test: `web/src/features/roadmap/inbox/ProposalDetail.test.tsx` (extend existing)

**Interfaces:**
- Consumes: `slugify` (existing, `web/src/features/roadmap/slug.ts`, already used by
  `NewCapabilityDialog.tsx`/`EditCapabilityDialog.tsx`).
- Produces: `OverlayEdits.newCapability?: { title: string; slug: string }`.

- [ ] **Step 1: Widen `OverlayEdits`**

In `web/src/api/triage.ts`, change:

```typescript
export interface OverlayEdits {
  reach?: number
  impact?: number
  effort?: number
  confidence?: number
  featureId?: string | null
  triageState?: string
}
```

to add one field:

```typescript
export interface OverlayEdits {
  reach?: number
  impact?: number
  effort?: number
  confidence?: number
  featureId?: string | null
  newCapability?: { title: string; slug: string }
  triageState?: string
}
```

`acceptProposal(id, edits)` already forwards the whole `edits` object as-is
(`return api.post(... , edits ? { edits } : undefined)`), so no change needed there.

- [ ] **Step 2: Write the failing test**

Extend `ProposalDetail.test.tsx` (create it if it doesn't already exist, following
the file's existing mock-hook conventions used elsewhere in `roadmap/inbox/`):

```typescript
it('accepting with a newly typed capability sends newCapability instead of featureId', async () => {
  const mutate = vi.fn()
  vi.mocked(hooks.useAcceptProposal).mockReturnValue({
    mutate, isPending: false,
  } as unknown as ReturnType<typeof hooks.useAcceptProposal>)

  render(<ProposalDetail p={PROPOSAL} onDone={() => {}} />)
  fireEvent.change(screen.getByRole('combobox', { name: /capability/i }), {
    target: { value: '__new__' },
  })
  fireEvent.change(screen.getByLabelText(/new capability title/i), {
    target: { value: 'Payments' },
  })
  fireEvent.click(screen.getByRole('button', { name: /save edits & accept/i }))

  expect(mutate).toHaveBeenCalledWith({
    id: PROPOSAL.id,
    edits: { newCapability: { title: 'Payments', slug: 'payments' } },
  })
})
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `(cd web && npx vitest run src/features/roadmap/inbox/ProposalDetail.test.tsx)`
Expected: FAIL — no `__new__` sentinel or title field exists yet.

- [ ] **Step 4: Implement**

In `ProposalDetail.tsx`: import `slugify` from `../slug` (matching
`NewCapabilityDialog.tsx`'s import). Add local state:

```typescript
const [creatingCapability, setCreatingCapability] = useState(false)
const [newCapTitle, setNewCapTitle] = useState('')
const [newCapSlug, setNewCapSlug] = useState('')
const [slugTouched, setSlugTouched] = useState(false)
```

Change the capability `<Select>`'s `onChange` (currently `(e) => setEdits((s) =>
({ ...s, featureId: e.target.value || undefined }))`) to:

```typescript
onChange={(e) => {
  const value = e.target.value
  if (value === '__new__') {
    setCreatingCapability(true)
    setEdits((s) => ({ ...s, featureId: undefined }))
    return
  }
  setCreatingCapability(false)
  setEdits((s) => ({ ...s, featureId: value || undefined }))
}}
```

Add a `<option value="__new__">+ New {CAPABILITY.singular.toLowerCase()}</option>`
after the existing `<option value="">` line, before the `options.map(...)` list.
When `creatingCapability` is true, render title/slug `Input`s in place of (or
alongside, with the select disabled) the `<Select>`:

```tsx
{creatingCapability && (
  <div className="flex flex-col gap-2">
    <Field label="New capability title" htmlFor="new-cap-title">
      <Input id="new-cap-title" value={newCapTitle} onChange={(e) => {
        const title = e.target.value
        setNewCapTitle(title)
        if (!slugTouched) setNewCapSlug(slugify(title))
      }} />
    </Field>
    <Field label="Slug" htmlFor="new-cap-slug">
      <Input id="new-cap-slug" value={newCapSlug} onChange={(e) => {
        setSlugTouched(true)
        setNewCapSlug(e.target.value)
      }} />
    </Field>
  </div>
)}
```

Change `edited` (currently built as a diff of `SCORE_FIELDS` plus
`featureChanged`/`featureId`) to also carry `newCapability` when
`creatingCapability` is true:

```typescript
const edited: OverlayEdits = {}
for (const f of SCORE_FIELDS) if (differs(f)) edited[f] = effective[f]
if (creatingCapability && newCapTitle.trim() && newCapSlug.trim()) {
  edited.newCapability = { title: newCapTitle.trim(), slug: newCapSlug.trim() }
} else if (featureChanged) {
  edited.featureId = effective.featureId
}
```

`saveAndAccept`'s existing `accept.mutate({ id: p.id, edits: edited })` call needs no
change — `edited` now carries `newCapability` when applicable. The "Save edits &
accept" button's `disabled={!changed || accept.isPending}` guard needs `changed` to
also become true when `creatingCapability && newCapTitle.trim()` — extend whatever
`changed` boolean the file already computes (check its current definition before
editing; the research pass did not capture it verbatim) to include this case, no
parent picker in v1 (a triage-created capability lands at root — reparenting later
goes through the Capabilities tab).

- [ ] **Step 5: Run the test to verify it passes**

Run: `(cd web && npx vitest run src/features/roadmap/inbox/ProposalDetail.test.tsx)`
Expected: PASS

- [ ] **Step 6: Run the FULL web test suite**

Run: `(cd web && npm run test 2>&1 | tee /tmp/web-test-triage-inline-create.log | tail -40)`
Expected: PASS

- [ ] **Step 7: Typecheck, lint, build**

Run: `(cd web && npm run typecheck && npm run lint && npm run build)`
Expected: all green

- [ ] **Step 8: Commit**

```bash
git add web/src/api/triage.ts web/src/features/roadmap/inbox/ProposalDetail.tsx \
        web/src/features/roadmap/inbox/ProposalDetail.test.tsx
git commit -m "feat(web): inline capability creation during triage accept"
```

- [ ] **Step 9: Open PR 4**

```bash
gh pr create --base master --title "feat: inline capability creation during triage accept" \
  --body-file /tmp/pr4-body.md
gh pr ready <number>
gh pr merge <number> --squash --auto
```

---

## Self-Review Notes

**Spec coverage:** Retire (§1 spec) → Tasks 4-5. Merge (§1 spec) → Tasks 6-7. Re-map
action (§2 spec) → Task 11. Capabilities tab (§1 spec's "Expose ... through ... the
Products UI") → Tasks 9-10, 12. Triage inline creation (§3 spec) → Tasks 13-14.
`_resync_now`/`merge_branch_over_main` correctness fix (spec's two "Correction"
sections) → Tasks 1, 3.

**Known follow-ups for the implementer, called out explicitly in-place rather than
hidden as generic placeholders:** the exact `_client`/`_seeded` tuple shape and
`FakeGh` answer-queue sizing in Task 5/7 (existing test infra the plan couldn't
directly re-verify token-for-token); `CapabilityCounts`'s exact field names in Task
9; `Dialog`/`Field`/`Select` import paths in Task 11; the existing `Project`-lookup
helper name in Task 13; `ProposalDetail.tsx`'s exact `changed` boolean in Task 14.
Each is named with an exact file to check, not a vague "verify this works."
