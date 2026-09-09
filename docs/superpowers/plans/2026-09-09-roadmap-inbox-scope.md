# Roadmap Inbox scope (finish-line row 3) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the triage inbox onto the Roadmap list column as an **Inbox** scope with the existing key map and a six-second undo, move the pending-proposal badge to the Roadmap nav entry, and delete the Triage section (`/triage` redirects).

**Architecture:** The server gains a reversible accept/reject: `_apply_accept` and reject snapshot the work item's overlay onto the proposal (`undo_snapshot`, nullable JSON) and `POST /api/pm/triage/{id}/undo` restores it within a server-enforced window. The web adds `'inbox'` to `RoadmapScope`, selects a proposal through `?scope=inbox&proposal=<id>` (the open-item resolver learns to read search params), renders proposals as `CardFrame` rows grouped by product with the verbatim `inboxReducer` key map, and shows the proposal editor as the detail pane. Accept / reject / bulk-accept toasts carry an Undo action for six seconds.

**Tech Stack:** FastAPI + SQLAlchemy async + Alembic revision (record only), pytest; React 19 + react-router + TanStack Query + sonner toasts, vitest + Testing Library.

**Spec:** `docs/superpowers/specs/2026-09-08-finish-line-persona-modes-design.md` §2 D3, §4.2, §4.3, §5 row 3, and `docs/superpowers/specs/2026-09-07-roadmap-drilldown-design.md` §12 (Triage), §10 (`POST /api/pm/triage/{id}/undo`). Issue #1467. Phase 3 (#1478, merged) is the base.

## Global Constraints

- Scopes are `Map · Initiatives · Inbox` (Queue stays hidden until spec 003 C1). The Inbox list is proposals grouped by product; `?product=<slug>` scopes it with a visible chip; `?proposal=<id>` selects. Nav label `Roadmap`; scope label `ROADMAP.inbox` ("Inbox") from `web/src/features/products/vocab.ts`.
- Keys unchanged: `j/k` move · `a` accept · `e` edit · `r` reject · `x` select · `A` accept selected. Handled inside the Inbox scope body (window keydown as today, guarded by `overlayIsOpen()` and `isTypingTarget`); the shortcut registry is NOT extended in this row (a controller ruling — see plan §Rulings).
- Undo: accept and reject fire immediately and show a six-second toast with an **Undo** action (`duration: 6000`), mirroring `useCloseSession`'s archive toast; bulk accept shows one toast whose Undo undoes every landed id. The server window is `TRIAGE_UNDO_WINDOW_S = 60` seconds from `resolved_at`; past it the endpoint answers 409 `undo_expired`.
- Undo semantics: the proposal returns to `pending` with `resolved_at = NULL`; the work item's overlay (`feature_id, reach, impact, effort, confidence, triage_state, classified_by, classified_at`) is restored from the snapshot taken at accept/reject time. Siblings rewritten by `_repropose_siblings` are NOT restored (documented).
- New DB column is nullable (`_reconcile_columns` adds it on both dialects); an Alembic revision `0037_triage_undo_snapshot` chained off `0036_roadmap_tree` is the reviewable record. Never `func.now()`; existing tz-aware columns use `datetime.now(timezone.utc)` in `server/api/pm.py` — follow that file.
- Nav after this row: `sessions, pipeline, automations, roadmap, insights, settings`. `NavBadgeKey` gains `'roadmap'` (pending-proposal count). `/triage` and `/triage?projectSlug=x` redirect (replace) to `/roadmap?scope=inbox` and `/roadmap?scope=inbox&product=x`.
- Deleted in this row: `web/src/triage/TriageInbox.tsx`, `web/src/triage/triage.css`, `web/src/triage/__tests__/TriageInbox.test.tsx` (its reducer cases move with the reducer), the `triage` nav row, the `TriagePage` route. `keyboardState.ts` MOVES to `web/src/features/roadmap/inbox/inboxKeys.ts` verbatim (git mv) with its unit tests.
- Token classes only under `web/src/features|shell|ui` (`ccswitch/no-raw-palette`). Vocabulary from `vocab.ts`. Tile/badge consistency rule: a badge and the list under it count the same population.
- Web gate on every web task: `(cd web && npm run typecheck && npm run lint && npm run test && npm run build)` then remove `web/dist`. Server gate on server tasks: `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/`, and the touched server test files; the full `server/tests/` once at the end with the test-slot acquire in the same shell.
- Bash hook: `(cd web && …)` subshells; commit messages via `git commit -F`; never pair deploy/release/rollout/promote with prod/production in command text.
- Commit trailer: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. Branch `roadmap-inbox` off `origin/master`. One PR; ready + `gh pr merge --squash --auto` when green. PR body: `Fixes #1467`.

## Rulings (controller, pre-execution)

- Selection is a search param per finish-line §4.2, so `OpenItemResolver` widens to `(params, search: URLSearchParams)`; the sessions resolver ignores `search`. Cost if wrong: one signature in `shell/openItems.ts`.
- The Inbox key map stays local to the scope body (as `TriageInbox` did), with the key legend rendered in the scope's controls strip. Registering `roadmap-inbox` in `shell/shortcuts.ts` needs handlers in `AppShellInner` and is deferred to a follow-up issue. Cost if wrong: keys are not in the Help sheet's table.
- "Needs a home" / Reclassify (spec §12 last sentence) is deferred to Phase 4 with issue leaves (no endpoint lists `needs_human` items yet); the pseudo row keeps opening the Inbox scoped to the product.
- The nav badge counts pending proposals via `useTriage()` (all products) in `useNavBadges` — one 15 s poll app-wide, the same poll the old Products page ran. The Inbox scope segment count is the same query, so nav badge and scope badge agree.
- The RICE score shown in the editor mirrors `server/pm/score.py:rice_score` (`reach*impact*confidence/effort`, 2 dp, `None` when any is null or effort is 0) — a cross-language mirror like `ROADMAP_TIERS`, pinned by a test with the server's own example values.

## File map

| File | Responsibility |
|------|----------------|
| `server/db/models.py` | `TriageProposal.undo_snapshot: Mapped[dict \| None]` (JSON, nullable). |
| `server/db/migrations/versions/0037_triage_undo_snapshot.py` | The reviewable record. |
| `server/api/pm.py` | Snapshot in `_apply_accept` + reject; `POST /triage/{id}/undo`; `TRIAGE_UNDO_WINDOW_S`. |
| `server/tests/test_pm_api.py` | Undo tests. |
| `web/src/api/triage.ts`, `web/src/api/hooks.ts` | `undoProposal`, `useUndoProposal`, undo toasts on accept/reject/bulk, `QK.roadmapMap` on reject/undo. |
| `web/src/features/roadmap/roadmapNavState.ts` | `'inbox'` scope, `inboxPath`, `proposalKey`, `roadmapOpenItem(params, search)`. |
| `web/src/shell/openItems.ts`, `openSessionId.ts` | Resolver signature gains `search`. |
| `web/src/features/roadmap/inbox/inboxKeys.ts` (+ test) | Moved `keyboardState.ts`. |
| `web/src/features/roadmap/inbox/rice.ts` (+ test) | `riceScore`. |
| `web/src/features/roadmap/inbox/ProposalCard.tsx` | One proposal row on `CardFrame`. |
| `web/src/features/roadmap/inbox/InboxScopeBody.tsx` (+ test) | The Inbox list: grouping, search, chip, keys, bulk bar. |
| `web/src/features/roadmap/inbox/ProposalDetail.tsx` (+ test) | The editor pane: fields, capability select, Accept / Save & accept / Reject. |
| `web/src/features/roadmap/inbox/InboxWelcome.tsx` | Nothing-open state for the Inbox scope. |
| `web/src/features/roadmap/RoadmapScopeSegment.tsx`, `RoadmapListColumn.tsx`, `useRoadmapWorkspace.ts`, `RoadmapPage.tsx` | Third scope wired; detail branch; workspace props. |
| `web/src/shell/navDefs.ts`, `useNavBadges.ts`, `routes.tsx`, `features/assistant/starters.ts` (+ tests) | Triage row deleted; `roadmap` badge; `/triage` redirects. |
| `web/src/features/roadmap/detail/NeedsYouList.tsx`, `web/src/projects/OnboardingWizard.tsx` (+ tests) | Links → `inboxPath({ product })`. |
| deleted | `web/src/triage/TriageInbox.tsx`, `triage.css`, `__tests__/TriageInbox.test.tsx` (reducer cases moved), `__tests__/hooks.test.tsx` (moved to `web/src/api/__tests__/triageHooks.test.tsx`). |
| `docs/product/features/product-management-layer.md`, roadmap spec status line | Docs. |

---

### Task 1: Server — reversible accept/reject and `POST /api/pm/triage/{id}/undo`

**Files:**
- Modify: `server/db/models.py` (`TriageProposal`), `server/api/pm.py`
- Create: `server/db/migrations/versions/0037_triage_undo_snapshot.py`
- Test: `server/tests/test_pm_api.py`

**Interfaces:**
- Produces: `TriageProposal.undo_snapshot: dict | None` with keys `featureId, reach, impact, effort, confidence, triageState, classifiedBy, classifiedAt` (camelCase, JSON-safe; `classifiedAt` ISO or null). `TRIAGE_UNDO_WINDOW_S = 60`. `POST /api/pm/triage/{proposal_id}/undo` → `200 {"id", "status": "pending", "workItemId", "triageState": <restored>}`; `404 {"error": "not_found"}`; `409 {"error": "not_resolved"}` when the proposal is still pending; `409 {"error": "undo_expired", "message": …}` past the window; `409 {"error": "no_snapshot"}` for rows resolved before this change. Audit `pm.triage.undo` `{proposalId, workItemId, previousStatus}`.

- [ ] **Step 1: Write the failing tests** — append to `server/tests/test_pm_api.py`:

```python
def test_triage_undo_restores_overlay_and_pending():
    with _client() as c:
        _register(c, "ccswitch", repo="o/r")
        pid, wid = _seed_one_pending(c, feature_slug="sessions")
        r = c.post(f"/api/pm/triage/{pid}/accept", json={"edits": {"impact": 9}})
        assert r.status_code == 200
        r = c.post(f"/api/pm/triage/{pid}/undo")
        assert r.status_code == 200, r.text
        assert r.json() == {"id": pid, "status": "pending", "workItemId": wid, "triageState": "proposed"}
        # the proposal is pending again and listed
        assert any(p["id"] == pid for p in c.get("/api/pm/triage").json())
        # the overlay is back to its pre-accept values
        wi = c.get("/api/pm/triage").json()  # list only carries proposals; read the item via overlay PATCH no-op
        r = c.patch(f"/api/pm/work-items/{wid}/overlay", json={})
        body = r.json()
        assert body["triageState"] == "proposed"
        assert body["impact"] is None and body["featureId"] is None
        assert body["classifiedBy"] is None and body["classifiedAt"] is None
        # a second undo is refused: it is pending, not resolved
        assert c.post(f"/api/pm/triage/{pid}/undo").status_code == 409
        assert c.post(f"/api/pm/triage/{pid}/undo").json()["detail"]["error"] == "not_resolved"
        # and it can be accepted again
        assert c.post(f"/api/pm/triage/{pid}/accept").status_code == 200


def test_triage_undo_after_reject():
    with _client() as c:
        _register(c, "ccswitch", repo="o/r")
        pid, wid = _seed_one_pending(c, feature_slug="sessions")
        assert c.post(f"/api/pm/triage/{pid}/reject").status_code == 200
        assert c.patch(f"/api/pm/work-items/{wid}/overlay", json={}).json()["triageState"] == "needs_human"
        r = c.post(f"/api/pm/triage/{pid}/undo")
        assert r.status_code == 200
        assert r.json()["triageState"] == "proposed"
        assert c.patch(f"/api/pm/work-items/{wid}/overlay", json={}).json()["triageState"] == "proposed"


def test_triage_undo_unknown_and_expired(monkeypatch):
    import server.api.pm as pm_api
    with _client() as c:
        _register(c, "ccswitch", repo="o/r")
        assert c.post("/api/pm/triage/nope/undo").status_code == 404
        pid, _ = _seed_one_pending(c, feature_slug="sessions")
        assert c.post(f"/api/pm/triage/{pid}/accept").status_code == 200
        monkeypatch.setattr(pm_api, "TRIAGE_UNDO_WINDOW_S", 0)
        r = c.post(f"/api/pm/triage/{pid}/undo")
        assert r.status_code == 409
        assert r.json()["detail"]["error"] == "undo_expired"


def test_triage_undo_writes_audit_row():
    with _client() as c:
        _register(c, "ccswitch", repo="o/r")
        pid, wid = _seed_one_pending(c, feature_slug="sessions")
        c.post(f"/api/pm/triage/{pid}/accept")
        c.post(f"/api/pm/triage/{pid}/undo")
        rows = c.get("/api/audit?type=pm.triage.undo").json()
        # audit list shape: follow whatever test_triage_accept_and_reject_flow reads; the
        # assertion is that exactly one pm.triage.undo row exists carrying the ids
        found = [r for r in (rows if isinstance(rows, list) else rows.get("items", []))
                 if r.get("type") == "pm.triage.undo"]
        assert len(found) == 1
        assert found[0]["detail"]["proposalId"] == pid and found[0]["detail"]["workItemId"] == wid
        assert found[0]["detail"]["previousStatus"] == "accepted"
```

If there is no `GET /api/audit` route, replace the last test's read with the DB-side helper the file already uses for `test_overlay_patch_with_empty_body_is_a_no_op` (it asserts audit rows) — copy its idiom.

- [ ] **Step 2: Run them to see them fail**

Run: `env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_pm_api.py -q -p no:randomly -k undo`
Expected: FAIL — 404/405 on the undo route.

- [ ] **Step 3: Model + migration**

In `server/db/models.py`, `TriageProposal`, after `resolved_at`:

```python
    # Pre-resolution overlay of the work item (featureId, reach, impact, effort,
    # confidence, triageState, classifiedBy, classifiedAt), written by accept and
    # reject so `POST /triage/{id}/undo` can put it back (finish-line row 3).
    # Nullable so reconcile ALTER-ADDs it on both dialects; NULL = resolved
    # before this column existed → undo answers 409 no_snapshot.
    undo_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
```

`server/db/migrations/versions/0037_triage_undo_snapshot.py`:

```python
"""triage_proposals.undo_snapshot — reversible accept/reject (finish-line row 3).

Revision ID: 0037_triage_undo_snapshot
Revises: 0036_roadmap_tree
"""
from alembic import op
import sqlalchemy as sa

revision = "0037_triage_undo_snapshot"
down_revision = "0036_roadmap_tree"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("triage_proposals", sa.Column("undo_snapshot", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("triage_proposals", "undo_snapshot")
```

- [ ] **Step 4: Snapshot + undo in `server/api/pm.py`**

Add near the other module constants (after the imports block that ends with `request_project_sync`):

```python
# How long after accept/reject a proposal can be undone. The UI toast lasts
# six seconds; the server keeps a wider window so a slow click still lands.
TRIAGE_UNDO_WINDOW_S = 60


def _overlay_snapshot(wi: WorkItem) -> dict:
    return {
        "featureId": wi.feature_id, "reach": wi.reach, "impact": wi.impact,
        "effort": wi.effort, "confidence": wi.confidence, "triageState": wi.triage_state,
        "classifiedBy": wi.classified_by,
        "classifiedAt": wi.classified_at.isoformat() if wi.classified_at else None,
    }


def _restore_overlay(wi: WorkItem, snap: dict) -> None:
    wi.feature_id = snap.get("featureId")
    wi.reach = snap.get("reach")
    wi.impact = snap.get("impact")
    wi.effort = snap.get("effort")
    wi.confidence = snap.get("confidence")
    wi.triage_state = snap.get("triageState")
    wi.classified_by = snap.get("classifiedBy")
    at = snap.get("classifiedAt")
    wi.classified_at = datetime.fromisoformat(at) if at else None
```

In `_apply_accept`, right after the `wi is None` 404 guard and before any overlay write, add `proposal.undo_snapshot = _overlay_snapshot(wi)`. In `reject_triage`, before `wi.triage_state = "needs_human"`, add `prop.undo_snapshot = _overlay_snapshot(wi)` (inside the `if wi is not None:`).

Add the route after `reject_triage`:

```python
@router.post("/triage/{proposal_id}/undo")
async def undo_triage(
    proposal_id: str,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Put an accepted or rejected proposal back to pending and restore the work
    item's overlay from the snapshot accept/reject took (roadmap spec §10, §12).
    Only within TRIAGE_UNDO_WINDOW_S of resolution; siblings re-proposed by the
    drift latch are NOT restored (their no_fit_suggestion is gone)."""
    prop = (await db.execute(select(TriageProposal).where(
        TriageProposal.id == proposal_id))).scalar_one_or_none()
    if prop is None:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    if prop.status == "pending":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
            "error": "not_resolved", "message": "proposal is pending; nothing to undo"})
    resolved = prop.resolved_at
    if resolved is not None and resolved.tzinfo is None:
        resolved = resolved.replace(tzinfo=timezone.utc)  # SQLite hands back naive
    now = datetime.now(timezone.utc)
    if resolved is None or (now - resolved).total_seconds() > TRIAGE_UNDO_WINDOW_S:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
            "error": "undo_expired",
            "message": f"undo is only available for {TRIAGE_UNDO_WINDOW_S}s after a decision"})
    snap = prop.undo_snapshot
    if not snap:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
            "error": "no_snapshot", "message": "this decision predates undo support"})
    wi = (await db.execute(select(WorkItem).where(
        WorkItem.id == prop.work_item_id))).scalar_one_or_none()
    if wi is not None:
        _restore_overlay(wi, snap)
    previous = prop.status
    prop.status = "pending"
    prop.resolved_at = None
    prop.undo_snapshot = None
    await db.flush()
    await record_audit(db=db, type="pm.triage.undo", identity=identity,
                       detail={"proposalId": proposal_id, "workItemId": prop.work_item_id,
                               "previousStatus": previous})
    return {"id": prop.id, "status": "pending", "workItemId": prop.work_item_id,
            "triageState": wi.triage_state if wi is not None else None}
```

`accept_bulk` goes through `_apply_accept`, so each landed id gets its own snapshot for free.

- [ ] **Step 5: Run the tests + the whole file + gate**

Run: `env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_pm_api.py server/tests/test_pm_triage_seams.py server/tests/test_schema_reconcile_coverage.py server/tests/test_postgres_compat.py -q -p no:randomly` (the last two confirm a nullable JSON column needs no list entry) then `.venv/bin/ruff check src/ server/ && .venv/bin/mypy server/ --ignore-missing-imports`.
Expected: PASS, clean.

- [ ] **Step 6: Commit**

```bash
printf 'feat(pm): reversible triage decisions — undo_snapshot and POST /triage/{id}/undo\n\nFinish-line row 3 / roadmap spec §10, §12: accept and reject snapshot the\nwork-item overlay onto the proposal; undo restores it and returns the proposal\nto pending within TRIAGE_UNDO_WINDOW_S. Siblings rewritten by the drift latch\nare not restored.\n\nRefs #1467\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c1.txt
git add server/db/models.py server/db/migrations/versions/0037_triage_undo_snapshot.py server/api/pm.py server/tests/test_pm_api.py && git commit -F /tmp/c1.txt
```

---

### Task 2: Web API — `undoProposal`, `useUndoProposal`, and Undo toasts on accept / reject / bulk

**Files:**
- Modify: `web/src/api/triage.ts`, `web/src/api/hooks.ts`
- Move: `web/src/triage/__tests__/hooks.test.tsx` → `web/src/api/__tests__/triageHooks.test.tsx` (git mv, then extend)

**Interfaces:**
- Produces: `undoProposal(id): Promise<{ id; status: 'pending'; workItemId: string | null; triageState: string | null }>`; `useUndoProposal(projectSlug?)` (mutation over `string` id; invalidates `QK.triage`, `QK.roadmapMap`, and `QK.onboarding(projectSlug)` when scoped; error toast "Could not undo"). `useAcceptProposal`, `useRejectProposal`, `useAcceptBulk` gain a success toast with `{ duration: 6000, action: { label: 'Undo', onClick } }` that calls `undoProposal` for the landed id(s) and invalidates the same keys. `useRejectProposal` now also invalidates `QK.roadmapMap` (the map's `awaitingTriage` / `needsHome` counts move on reject).

- [ ] **Step 1: Move and extend the hooks test**

`git mv web/src/triage/__tests__/hooks.test.tsx web/src/api/__tests__/triageHooks.test.tsx`, fix its imports (`@/api/hooks`, `@/api/client` — unchanged paths), then append:

```tsx
describe('undo', () => {
  it('accept shows a 6 s Undo toast whose action calls the undo endpoint and refreshes', async () => {
    post.mockResolvedValueOnce({ id: 'p1', status: 'accepted', workItemId: 'w1', triageState: 'classified' })
    const { result } = renderHook(() => useAcceptProposal(), { wrapper })
    await act(async () => { await result.current.mutateAsync({ id: 'p1' }) })
    expect(toast.success).toHaveBeenCalledWith(
      'Accepted',
      expect.objectContaining({ duration: 6000, action: expect.objectContaining({ label: 'Undo' }) }),
    )
    post.mockResolvedValueOnce({ id: 'p1', status: 'pending', workItemId: 'w1', triageState: 'proposed' })
    const call = vi.mocked(toast.success).mock.calls.at(-1)![1] as { action: { onClick: () => void } }
    await act(async () => { call.action.onClick() })
    expect(post).toHaveBeenLastCalledWith('/pm/triage/p1/undo', undefined)
  })

  it('bulk accept undoes every landed id from one toast', async () => {
    post.mockResolvedValueOnce({ accepted: 2, results: [{ id: 'a', ok: true }, { id: 'b', ok: true }, { id: 'c', ok: false, error: 'not_pending' }] })
    const { result } = renderHook(() => useAcceptBulk(), { wrapper })
    await act(async () => { await result.current.mutateAsync(['a', 'b', 'c']) })
    const call = vi.mocked(toast.success).mock.calls.at(-1)!
    expect(call[0]).toBe('Accepted 2')
    post.mockResolvedValue({ id: 'x', status: 'pending', workItemId: null, triageState: 'proposed' })
    await act(async () => { (call[1] as { action: { onClick: () => void } }).action.onClick() })
    const undone = post.mock.calls.filter(([p]) => String(p).endsWith('/undo')).map(([p]) => p)
    expect(undone).toEqual(['/pm/triage/a/undo', '/pm/triage/b/undo'])
  })

  it('useUndoProposal posts and toasts on failure', async () => {
    post.mockRejectedValueOnce(new ApiRequestError(409, { detail: { error: 'undo_expired' } }))
    const { result } = renderHook(() => useUndoProposal(), { wrapper })
    await act(async () => { await result.current.mutateAsync('p1').catch(() => undefined) })
    expect(post).toHaveBeenCalledWith('/pm/triage/p1/undo', undefined)
    expect(toast.error).toHaveBeenCalled()
  })
})
```

Adapt `post`, `wrapper`, `toast` mock names to what the moved file already declares (it mocks `@/api/client` and `sonner`; read it first).

- [ ] **Step 2: Run to see the new cases fail**

Run: `(cd web && npx vitest run src/api/__tests__/triageHooks.test.tsx)`
Expected: the three new cases FAIL (`useUndoProposal` undefined; no toast on accept).

- [ ] **Step 3: `web/src/api/triage.ts`**

```ts
export interface UndoResult { id: string; status: 'pending'; workItemId: string | null; triageState: string | null }

/** Put an accepted/rejected proposal back to pending (server window ≈ 60 s). */
export function undoProposal(id: string): Promise<UndoResult> {
  return api.post<UndoResult>(`/pm/triage/${encodeURIComponent(id)}/undo`)
}
```

- [ ] **Step 4: `web/src/api/hooks.ts`**

Import `undoProposal` from `./triage`. Add one private helper beside the triage hooks:

```ts
/** Six seconds to change your mind — same shape as the archive toast in
 *  `useCloseSession`. `ids` are the landed proposal ids; every one is undone. */
function triageUndoToast(qc: QueryClient, message: string, ids: string[], projectSlug?: string) {
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: QK.triage })
    void qc.invalidateQueries({ queryKey: QK.roadmapMap })
    if (projectSlug) void qc.invalidateQueries({ queryKey: QK.onboarding(projectSlug) })
  }
  toast.success(message, {
    duration: 6000,
    action: {
      label: 'Undo',
      onClick: () => {
        void Promise.allSettled(ids.map((id) => undoProposal(id))).then((results) => {
          const failed = results.filter((r) => r.status === 'rejected')
          if (failed.length) toast.error(failed.length === ids.length ? 'Could not undo' : `Could not undo ${failed.length} of ${ids.length}`)
          refresh()
        })
      },
    },
  })
}
```

Then:
- `useAcceptProposal`: in `onSuccess(_res, vars)` call `triageUndoToast(qc, 'Accepted', [vars.id], projectSlug)` (keep the existing invalidations).
- `useRejectProposal`: `onSuccess(_res, vars)` → `triageUndoToast(qc, 'Rejected', [vars.id], projectSlug)` and add `void qc.invalidateQueries({ queryKey: QK.roadmapMap })`.
- `useAcceptBulk`: replace `toast.success(\`Accepted ${res.accepted}\`)` with `if (res.accepted) triageUndoToast(qc, \`Accepted ${res.accepted}\`, res.results.filter((r) => r.ok).map((r) => r.id), projectSlug)`.
- New hook:

```ts
export function useUndoProposal(projectSlug?: string) {
  const qc = useQueryClient()
  return useMutation<UndoResult, ApiRequestError, string>({
    mutationFn: (id) => undoProposal(id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: QK.triage })
      void qc.invalidateQueries({ queryKey: QK.roadmapMap })
      if (projectSlug) void qc.invalidateQueries({ queryKey: QK.onboarding(projectSlug) })
    },
    onError: (e) => toast.error(extractMutationErrorMessage(e, 'Could not undo')),
    meta: PM_MUTATION_META,
  })
}
```

(`QueryClient` type import from `@tanstack/react-query`; `UndoResult` type import from `./triage`.)

- [ ] **Step 5: Tests, typecheck, lint, full vitest; commit**

Run: `(cd web && npx vitest run src/api/__tests__/triageHooks.test.tsx && npm run typecheck && npm run lint && npm run test)`

```bash
printf 'feat(web): six-second Undo on triage accept, reject and bulk accept; useUndoProposal\n\nRefs #1467\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c2.txt
git add web/src/api web/src/triage && git commit -F /tmp/c2.txt
```

---

### Task 3: Navigation state — the `inbox` scope, `?proposal=` selection, search-aware open-item resolver

**Files:**
- Modify: `web/src/features/roadmap/roadmapNavState.ts` (+ `.test.ts`), `web/src/shell/openItems.ts`, `web/src/shell/openSessionId.ts` (+ `openSessionId.test.tsx`)

**Interfaces:**
- Produces: `RoadmapScope = 'map' | 'initiatives' | 'inbox'`; `ROADMAP_SCOPES` includes `'inbox'`; `roadmapScopeForSearch` is a membership test; `roadmapPath('inbox') === '/roadmap?scope=inbox'`; `inboxPath({ product?: string; proposal?: string }): string` → `/roadmap?scope=inbox[&product=…][&proposal=…]`; `inboxProductForSearch(search): string | null`; `RoadmapOpenItem` gains `{ kind: 'proposal'; proposalId: string }`; `roadmapOpenItem(params, search: URLSearchParams)` returns the proposal item when `search.get('scope') === 'inbox' && search.get('proposal')`; `proposalKey(id) = 'proposal:' + id`; `keyForOpenItem` maps it.
- Produces: `OpenItemResolver = (params: Readonly<Params<string>>, search: URLSearchParams) => OpenItem | null`; `useOpenItem()` passes `new URLSearchParams(useLocation().search)`.

- [ ] **Step 1: Failing tests** — `roadmapNavState.test.ts` additions:

```ts
  it('knows the inbox scope and builds its paths', () => {
    expect(roadmapScopeForSearch('?scope=inbox')).toBe('inbox')
    expect(roadmapPath('inbox')).toBe('/roadmap?scope=inbox')
    expect(inboxPath({})).toBe('/roadmap?scope=inbox')
    expect(inboxPath({ product: 'kin dash' })).toBe('/roadmap?scope=inbox&product=kin%20dash')
    expect(inboxPath({ product: 'kindash', proposal: 'p1' })).toBe('/roadmap?scope=inbox&product=kindash&proposal=p1')
    expect(inboxProductForSearch('?scope=inbox&product=kindash')).toBe('kindash')
    expect(inboxProductForSearch('?scope=map&product=kindash')).toBeNull()
  })
  it('resolves an open proposal from the search, never from a route param', () => {
    const sp = (s: string) => new URLSearchParams(s)
    expect(roadmapOpenItem({}, sp('?scope=inbox&proposal=p1'))).toEqual({ kind: 'proposal', proposalId: 'p1' })
    expect(roadmapOpenItem({}, sp('?scope=map&proposal=p1'))).toBeNull()
    expect(roadmapOpenItem({ productSlug: 'k' }, sp('?scope=inbox&proposal=p1'))).toEqual({ kind: 'product', productSlug: 'k' })
    expect(keyForOpenItem({ kind: 'proposal', proposalId: 'p1' })).toBe('proposal:p1')
  })
```

Update the existing `roadmapOpenItem(...)` calls in that file to pass `new URLSearchParams('')` as the second argument. In `openSessionId.test.tsx` add a row: at `/roadmap?scope=inbox&proposal=p1` the open item is `{ kind: 'proposal', proposalId: 'p1' }` and `useOpenSessionId()` is null.

- [ ] **Step 2: Run to fail** — `(cd web && npx vitest run src/features/roadmap/roadmapNavState.test.ts src/shell/openSessionId.test.tsx)`

- [ ] **Step 3: Implement**

`roadmapNavState.ts`:

```ts
export type RoadmapScope = 'map' | 'initiatives' | 'inbox'
export const ROADMAP_SCOPES: readonly RoadmapScope[] = ['map', 'initiatives', 'inbox']

export function roadmapScopeForSearch(search: string): RoadmapScope {
  const v = new URLSearchParams(search).get('scope')
  return (ROADMAP_SCOPES as readonly string[]).includes(v ?? '') ? (v as RoadmapScope) : 'map'
}

/** The Inbox scope, optionally narrowed to one product and/or with one proposal open. */
export function inboxPath({ product, proposal }: { product?: string | null; proposal?: string | null } = {}): string {
  const q = new URLSearchParams({ scope: 'inbox' })
  if (product) q.set('product', product)
  if (proposal) q.set('proposal', proposal)
  return `${ROADMAP_PATH}?${q.toString()}`
}

export function inboxProductForSearch(search: string): string | null {
  const q = new URLSearchParams(search)
  return q.get('scope') === 'inbox' ? (q.get('product') || null) : null
}

export type RoadmapOpenItem =
  | { kind: 'product'; productSlug: string }
  | { kind: 'capability'; productSlug: string; capSlug: string }
  | { kind: 'initiative'; initiativeSlug: string }
  | { kind: 'proposal'; proposalId: string }

export function roadmapOpenItem(
  params: Readonly<Record<string, string | undefined>>,
  search: URLSearchParams,
): RoadmapOpenItem | null {
  const { productSlug, capSlug, initiativeSlug } = params
  if (initiativeSlug) return { kind: 'initiative', initiativeSlug }
  if (productSlug && capSlug) return { kind: 'capability', productSlug, capSlug }
  if (productSlug) return { kind: 'product', productSlug }
  const proposal = search.get('proposal')
  if (search.get('scope') === 'inbox' && proposal) return { kind: 'proposal', proposalId: proposal }
  return null
}

export const proposalKey = (id: string) => `proposal:${id}`
// keyForOpenItem: add `if (item.kind === 'proposal') return proposalKey(item.proposalId)`
```

`shell/openItems.ts`: `export type OpenItemResolver = (params: Readonly<Params<string>>, search: URLSearchParams) => OpenItem | null`; `sessions: ({ id, archivedId }) => …` unchanged (second arg ignored); `roadmap: roadmapOpenItem`. `shell/openSessionId.ts`: `const { pathname, search } = useLocation()` and `OPEN_ITEM_RESOLVERS[def.id]?.(params, new URLSearchParams(search)) ?? null` (memoise the `URLSearchParams` with `useMemo` on `search`). Fix every other caller of `roadmapOpenItem` (`useRoadmapWorkspace.ts`, `RoadmapPage.tsx`) to pass `new URLSearchParams(useLocation().search)`.

- [ ] **Step 4: Gate + commit** — `(cd web && npx vitest run src/features/roadmap src/shell && npm run typecheck && npm run lint)`

```bash
printf 'feat(roadmap): inbox scope in the nav state; ?proposal= selection; search-aware open-item resolver\n\nRefs #1467\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c3.txt
git add web/src/features/roadmap web/src/shell && git commit -F /tmp/c3.txt
```

---

### Task 4: The Inbox scope in the list column — rows, grouping, key map, bulk bar

**Files:**
- Move: `web/src/triage/keyboardState.ts` → `web/src/features/roadmap/inbox/inboxKeys.ts` (git mv, content unchanged); move the `inboxReducer` / `resolveCursorIndex` / `scoreInputValue` / `isTypingTarget` `describe` blocks out of `web/src/triage/__tests__/TriageInbox.test.tsx` into `web/src/features/roadmap/inbox/inboxKeys.test.ts` (node environment).
- Create: `web/src/features/roadmap/inbox/ProposalCard.tsx`, `web/src/features/roadmap/inbox/InboxScopeBody.tsx`, `web/src/features/roadmap/inbox/InboxScopeBody.test.tsx`
- Modify: `web/src/features/roadmap/RoadmapScopeSegment.tsx` (+ test if one exists), `RoadmapListColumn.tsx` (+ test), `useRoadmapWorkspace.ts`, `web/src/shell/roadmapWorkspace.test.tsx`

**Interfaces:**
- `ProposalCard({ proposal: TriageProposal; selected: boolean; checked: boolean; cursor: boolean; onOpen(): void; onToggleSelect(): void })` on `CardFrame` (`role` via a wrapping `<li>`: the list is `role="list"`). Accessible names: open control `Open proposal <title ?? id>`; checkbox `Select <title ?? id>` (`aria-pressed`). Line 1: tier dot by confidence (`high → success`, `medium → info`, `low → neutral`), title; trailing: `Badge` with `proposedFeatureSlug` or `no fit → <suggestion>`; line 2: `repo#number · R I E C`.
- `InboxScopeBody({ proposals, isLoading, isError, product, selectedKey, onOpen(proposal), onClearProduct })` — `product` is the `?product=` chip; the key map (`j/k/x/a/r/e/A`) and the bulk bar live here; `e` opens the proposal in the detail pane with `?edit=1` (the editor is the detail pane — spec §12 — so "edit" means "open with the score fields focused").
- `RoadmapListColumnProps` gains `inbox: { proposals: TriageProposal[]; isLoading: boolean; isError: boolean; product: string | null; onOpenProposal: (p: TriageProposal) => void; onClearProduct: () => void }`; `RoadmapScopeSegmentProps` gains `inboxCount: number`.
- `useRoadmapWorkspace` supplies `inbox` from `useTriage(product ?? undefined)` (enabled always — the count feeds the segment in every scope) and `onOpenInbox(productSlug)` → `navigate(inboxPath({ product: productSlug }))`.

- [ ] **Step 1: Failing tests** — `InboxScopeBody.test.tsx`:

```tsx
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { TriageProposal } from '@/api/triage'
import { InboxScopeBody } from './InboxScopeBody'

const accept = vi.fn(); const reject = vi.fn(); const bulk = vi.fn()
vi.mock('@/api/hooks', () => ({
  useAcceptProposal: () => ({ mutate: accept, isPending: false }),
  useRejectProposal: () => ({ mutate: reject, isPending: false }),
  useAcceptBulk: () => ({ mutate: bulk, isPending: false }),
  useTriageLive: () => undefined,
}))
vi.mock('@/shell/useOverlayState', () => ({ overlayIsOpen: () => false }))

const p = (over: Partial<TriageProposal> & { id: string }): TriageProposal => ({
  workItemId: `w-${over.id}`, projectSlug: 'kindash', repo: 'cairnea/KinDash', number: 1, kind: 'issue',
  title: over.id, url: null, proposedFeatureId: null, proposedFeatureSlug: 'tasks', noFitSuggestion: null,
  reach: 3, impact: 3, effort: 2, confidence: 80, rationale: null, confidenceLevel: 'high', status: 'pending',
  createdAt: null, ...over,
})
const ROWS = [p({ id: 'First' }), p({ id: 'Second', projectSlug: 'viberpit', repo: 'steiner385/viberpit' }), p({ id: 'Third' })]

function mount(over: Partial<React.ComponentProps<typeof InboxScopeBody>> = {}) {
  const onOpen = vi.fn(); const onClearProduct = vi.fn()
  render(<MemoryRouter><InboxScopeBody proposals={ROWS} isLoading={false} isError={false} product={null}
    selectedKey={null} onOpen={onOpen} onClearProduct={onClearProduct} {...over} /></MemoryRouter>)
  return { onOpen, onClearProduct }
}

describe('InboxScopeBody', () => {
  beforeEach(() => { accept.mockReset(); reject.mockReset(); bulk.mockReset(); localStorage.clear() })
  it('groups by product by default and opens a row', () => {
    const { onOpen } = mount()
    expect(screen.getByText('kindash')).toBeInTheDocument()
    expect(screen.getByText('viberpit')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Open proposal Second' }))
    expect(onOpen).toHaveBeenCalledWith(expect.objectContaining({ id: 'Second' }))
  })
  it('j/k move the cursor, a accepts the cursor row, r rejects', () => {
    mount()
    fireEvent.keyDown(window, { key: 'j' })
    fireEvent.keyDown(window, { key: 'a' })
    expect(accept).toHaveBeenCalledWith({ id: expect.any(String) })
    fireEvent.keyDown(window, { key: 'r' })
    expect(reject).toHaveBeenCalledWith({ id: expect.any(String) })
  })
  it('x selects and A accepts exactly the selection through the bulk bar', () => {
    mount()
    fireEvent.keyDown(window, { key: 'x' })
    fireEvent.keyDown(window, { key: 'j' })
    fireEvent.keyDown(window, { key: 'x' })
    expect(screen.getByText('2 selected')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Accept selected/ }))
    expect(bulk).toHaveBeenCalledWith(expect.arrayContaining([expect.any(String)]), expect.anything())
    expect(bulk.mock.calls[0][0]).toHaveLength(2)
  })
  it('e opens the cursor row for editing', () => {
    const { onOpen } = mount()
    fireEvent.keyDown(window, { key: 'e' })
    expect(onOpen).toHaveBeenCalledWith(expect.objectContaining({ id: expect.any(String) }), { edit: true })
  })
  it('ignores keys typed into inputs and while an overlay is open', () => {
    mount()
    fireEvent.keyDown(screen.getByRole('searchbox', { name: 'Search inbox' }), { key: 'a' })
    expect(accept).not.toHaveBeenCalled()
  })
  it('shows the product chip and clears it', () => {
    const { onClearProduct } = mount({ product: 'kindash' })
    expect(screen.getByText(/Scope: kindash/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Show every product' }))
    expect(onClearProduct).toHaveBeenCalled()
  })
  it('empty and loading states', () => {
    mount({ proposals: [], isLoading: false })
    expect(screen.getByText(/Inbox zero/)).toBeInTheDocument()
  })
})
```

`inboxKeys.test.ts`: the moved `describe` blocks, unchanged except the import path.

- [ ] **Step 2: Run to fail** — `(cd web && npx vitest run src/features/roadmap/inbox)`

- [ ] **Step 3: `ProposalCard.tsx`**

```tsx
import { Check, Inbox } from 'lucide-react'
import { cn } from '@/lib/utils'
import { CardFrame } from '@/components/card/CardFrame'
import { Badge, type Tone } from '@/ui/Badge'
import type { TriageProposal } from '@/api/triage'
import { TierDot } from '../rows/TierDot'

const CONF_TIER = { high: 'in-flight', medium: 'proposed', low: 'idle' } as const
const CONF_TONE: Record<string, Tone> = { high: 'success', medium: 'info', low: 'neutral' }

export interface ProposalCardProps {
  proposal: TriageProposal
  selected: boolean
  checked: boolean
  cursor: boolean
  onOpen: () => void
  onToggleSelect: () => void
}

export function ProposalCard({ proposal: p, selected, checked, cursor, onOpen, onToggleSelect }: ProposalCardProps) {
  const name = p.title ?? p.id
  const conf = p.confidenceLevel ?? 'low'
  const scores = [p.reach, p.impact, p.effort, p.confidence].map((v) => v ?? '—')
  return (
    <li data-testid={`row-proposal:${p.id}`} aria-current={cursor ? 'true' : undefined}
        className={cn(cursor && 'ring-1 ring-accent/40 rounded-md')}>
      <CardFrame id={`proposal-${p.id}`} openLabel={`Open proposal ${name}`}
                 railClass={checked ? 'bg-accent' : 'bg-neutral/40'} selected={selected} listMode={false}
                 focusable swipeEnabled={false} onOpen={onOpen}
                 leading={
                   <button type="button" aria-label={`Select ${name}`} aria-pressed={checked}
                           onClick={(e) => { e.stopPropagation(); onToggleSelect() }}
                           className={cn('flex size-6 shrink-0 items-center justify-center rounded-sm border border-edge',
                                          checked ? 'bg-accent text-accent-ink' : 'text-ink-faint hover:bg-surface-raised',
                                          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent')}>
                     {checked && <Check size={12} aria-hidden="true" />}
                   </button>
                 }
                 identity={
                   <span className="flex min-w-0 items-center gap-2">
                     <TierDot tier={CONF_TIER[conf as keyof typeof CONF_TIER] ?? 'idle'} />
                     <Inbox size={14} aria-hidden="true" className="shrink-0 text-ink-muted" />
                     <span className="truncate text-sm font-medium text-ink">{name}</span>
                   </span>
                 }
                 trailing={
                   p.proposedFeatureSlug
                     ? <Badge tone={CONF_TONE[conf] ?? 'neutral'}>{p.proposedFeatureSlug}</Badge>
                     : <Badge tone="warning">no fit → {p.noFitSuggestion ?? '?'}</Badge>
                 }>
        <span className="block truncate text-xs text-ink-muted">
          {p.repo}#{p.number} · R{scores[0]} I{scores[1]} E{scores[2]} C{scores[3]}
        </span>
      </CardFrame>
    </li>
  )
}
```

(`TierDot` takes `tier: RoadmapTier`; `'in-flight' | 'proposed' | 'idle'` are members. If `text-accent-ink` is not a token in `index.css`, use `text-canvas`.)

- [ ] **Step 4: `InboxScopeBody.tsx`**

```tsx
/**
 * The Inbox scope (finish-line §4.3 / roadmap spec §12): pending proposals
 * grouped by product, the j/k a r e x A key map from the old Triage page, and
 * a bulk bar. Selection = `?proposal=` (the shell's open item); the cursor is
 * local. The editor is the detail pane, so `e` opens the row with `edit`.
 */
import { useCallback, useEffect, useMemo, useReducer, useState } from 'react'
import type { TriageProposal } from '@/api/triage'
import { useAcceptBulk, useAcceptProposal, useRejectProposal, useTriageLive } from '@/api/hooks'
import { GroupedList } from '@/components/GroupedList'
import { ListSearchInput } from '@/components/ListSearchInput'
import { BulkActionBar } from '@/components/list/BulkActionBar'
import { SessionListControls } from '@/components/SessionListControls'
import { Button } from '@/ui/Button'
import { Banner } from '@/ui/Banner'
import { EmptyState } from '@/ui/EmptyState'
import { Skeleton } from '@/ui/Skeleton'
import { Kbd } from '@/ui/Kbd'
import { useItemOrdering, type OrderingSpec } from '@/session/useSessionOrdering'
import { overlayIsOpen } from '@/shell/useOverlayState'
import { ScopeBody } from '@/features/sessions/ScopeBody'
import { PRODUCT, ROADMAP } from '@/features/products/vocab'
import { proposalKey } from '../roadmapNavState'
import { inboxReducer, initialInboxState, isTypingTarget, resolveCursorIndex } from './inboxKeys'
import { ProposalCard } from './ProposalCard'

export interface InboxScopeBodyProps {
  proposals: readonly TriageProposal[]
  isLoading: boolean
  isError: boolean
  /** The `?product=` chip, or null for every product. */
  product: string | null
  selectedKey: string | null
  onOpen: (p: TriageProposal, opts?: { edit: boolean }) => void
  onClearProduct: () => void
}

type GroupBy = 'product' | 'capability' | 'confidence' | 'none'
type SortBy = 'newest' | 'confidence' | 'title'
const CONF_RANK: Record<string, number> = { high: 0, medium: 1, low: 2 }

const SPEC: OrderingSpec<GroupBy, SortBy, TriageProposal> = {
  namespace: 'roadmap-inbox',
  groupByValues: ['product', 'capability', 'confidence', 'none'],
  sortByValues: ['newest', 'confidence', 'title'],
  defaultPref: { groupBy: 'product', sortBy: 'newest' },
  config: {
    idOf: (p) => p.id,
    groupings: {
      product: { bucketOf: (p) => ({ key: p.projectSlug ?? 'unknown', label: p.projectSlug ?? 'unknown' }) },
      capability: { bucketOf: (p) => ({ key: p.proposedFeatureSlug ?? 'no-fit', label: p.proposedFeatureSlug ?? 'No fit', order: p.proposedFeatureSlug ? 0 : 1 }) },
      confidence: { bucketOf: (p) => ({ key: p.confidenceLevel ?? 'low', label: p.confidenceLevel ?? 'low', order: CONF_RANK[p.confidenceLevel ?? 'low'] ?? 2 }) },
    },
    sorts: {
      newest: (a, b) => (b.createdAt ?? '').localeCompare(a.createdAt ?? '') || a.id.localeCompare(b.id),
      confidence: (a, b) => (CONF_RANK[a.confidenceLevel ?? 'low'] ?? 2) - (CONF_RANK[b.confidenceLevel ?? 'low'] ?? 2) || a.id.localeCompare(b.id),
      title: (a, b) => (a.title ?? a.id).localeCompare(b.title ?? b.id),
    },
  },
}

export function InboxScopeBody({ proposals, isLoading, isError, product, selectedKey, onOpen, onClearProduct }: InboxScopeBodyProps) {
  useTriageLive()
  const [query, setQuery] = useState('')
  const q = query.trim().toLowerCase()
  const visible = useMemo(
    () => (q ? proposals.filter((p) => `${p.title ?? ''} ${p.repo ?? ''}#${p.number ?? ''} ${p.proposedFeatureSlug ?? ''} ${p.noFitSuggestion ?? ''}`.toLowerCase().includes(q)) : proposals),
    [proposals, q],
  )
  const ordering = useItemOrdering(visible, SPEC)
  const ordered = ordering.orderedSessions
  const ids = useMemo(() => ordered.map((p) => p.id), [ordered])
  const [state, dispatch] = useReducer(inboxReducer, initialInboxState)
  useEffect(() => { dispatch({ type: 'rowsChanged', ids }) }, [ids])
  const cursorIndex = resolveCursorIndex(ids, state)
  const current = ordered[cursorIndex]

  const accept = useAcceptProposal(product ?? undefined)
  const reject = useRejectProposal(product ?? undefined)
  const bulk = useAcceptBulk(product ?? undefined)

  const acceptOne = useCallback((p: TriageProposal) => { accept.mutate({ id: p.id }); dispatch({ type: 'deselect', id: p.id }) }, [accept])
  const rejectOne = useCallback((p: TriageProposal) => { reject.mutate({ id: p.id }); dispatch({ type: 'deselect', id: p.id }) }, [reject])
  const acceptSelected = useCallback(() => {
    const sel = [...state.selected]
    if (sel.length) bulk.mutate(sel, { onSuccess: () => dispatch({ type: 'clearSelection' }) })
  }, [bulk, state.selected])

  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => {
      if (ev.defaultPrevented || overlayIsOpen() || ev.ctrlKey || ev.metaKey || ev.altKey) return
      if (isTypingTarget(ev.target)) return
      switch (ev.key) {
        case 'j': dispatch({ type: 'move', delta: 1, ids }); break
        case 'k': dispatch({ type: 'move', delta: -1, ids }); break
        case 'x': if (current) dispatch({ type: 'toggleSelect', id: current.id }); break
        case 'a': if (current) acceptOne(current); break
        case 'r': if (current) rejectOne(current); break
        case 'e': if (current) onOpen(current, { edit: true }); break
        case 'A': acceptSelected(); break
        default: return
      }
      ev.preventDefault()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [ids, current, acceptOne, rejectOne, acceptSelected, onOpen])

  return (
    <ScopeBody controls={
      <div className="flex flex-col gap-2">
        <SessionListControls<GroupBy, SortBy>
          groupBy={ordering.groupBy} sortBy={ordering.sortBy}
          onGroupByChange={ordering.setGroupBy} onSortByChange={ordering.setSortBy}
          searchQuery={query} onSearchChange={setQuery}
          searchPlaceholder="Search inbox…" searchAriaLabel="Search inbox"
          groupOptions={[{ value: 'product', label: PRODUCT.singular }, { value: 'capability', label: 'Capability' }, { value: 'confidence', label: 'Confidence' }, { value: 'none', label: 'None' }]}
          sortOptions={[{ value: 'newest', label: 'Newest' }, { value: 'confidence', label: 'Confidence' }, { value: 'title', label: 'Title' }]} />
        {product && (
          <div className="flex items-center gap-2 text-xs text-ink-muted">
            <span>Scope: {product}</span>
            <Button type="button" variant="ghost" size="sm" onClick={onClearProduct}>Show every product</Button>
          </div>
        )}
        <p className="text-xs text-ink-faint" aria-label="Inbox keys">
          <Kbd>j</Kbd>/<Kbd>k</Kbd> move · <Kbd>a</Kbd> accept · <Kbd>e</Kbd> edit · <Kbd>r</Kbd> reject · <Kbd>x</Kbd> select · <Kbd>A</Kbd> accept selected
        </p>
      </div>
    }>
      {isError && <Banner tone="danger" title="Could not load the inbox." />}
      {isLoading && proposals.length === 0 && <div role="status" aria-label="Loading inbox"><Skeleton lines={5} /></div>}
      {!isLoading && visible.length === 0 && (
        <EmptyState title={q ? 'No matches' : product ? `Inbox zero for ${product}` : 'Inbox zero — nothing to triage.'} />
      )}
      <GroupedList<TriageProposal>
        groups={ordering.groups} collapsedKeys={ordering.collapsedKeys} onToggleCollapse={ordering.toggleCollapse}
        panelRole="list" ariaLabel={ROADMAP.inbox}
        renderRow={(p) => (
          <ProposalCard key={p.id} proposal={p} selected={selectedKey === proposalKey(p.id)}
                        checked={state.selected.has(p.id)} cursor={current?.id === p.id}
                        onOpen={() => { dispatch({ type: 'focusRow', id: p.id, index: ids.indexOf(p.id) }); onOpen(p) }}
                        onToggleSelect={() => dispatch({ type: 'toggleSelect', id: p.id })} />
        )} />
      {state.selected.size > 0 && (
        <BulkActionBar label={`${state.selected.size} selected`} onClear={() => dispatch({ type: 'clearSelection' })}>
          <Button type="button" variant="secondary" size="lg" onClick={acceptSelected} disabled={bulk.isPending}>
            Accept selected ({state.selected.size})
          </Button>
        </BulkActionBar>
      )}
    </ScopeBody>
  )
}
```

Check `ui/Kbd` exists (it does: `web/src/ui/Kbd.tsx`) and its export name; check `SessionListControls`'s generic constraints and whether `GroupedList` renders `<li>` itself (if it wraps rows in `<li>`, make `ProposalCard` render a `<div>` instead — read `GroupedList.tsx` first).

- [ ] **Step 5: Wire the column, segment and workspace**

`RoadmapScopeSegment`: add `inboxCount: number` and a third `ScopeLink` (`to={roadmapPath('inbox')}`, label `ROADMAP.inbox`, `current={scope === 'inbox'}`), badge tone `warning` when not current and `inboxCount > 0`.
`RoadmapListColumn`: props gain `inbox: InboxScopeBodyProps` minus `selectedKey` (the column passes its own); the ternary becomes a `switch (scope)`; the segment receives `inboxCount={inbox.proposals.length}`.
`useRoadmapWorkspace(active)`: `const search = useLocation().search; const product = inboxProductForSearch(search); const triage = useTriage(product ?? undefined)`; `onOpenInbox = (slug) => navigate(inboxPath({ product: slug }))`; `inbox: { proposals: triage.data ?? [], isLoading: triage.isLoading, isError: triage.isError, product, onOpenProposal: (p, opts) => navigate(inboxPath({ product, proposal: p.id }) + (opts?.edit ? '&edit=1' : '')), onClearProduct: () => navigate(inboxPath({})) }`; `selectedKey: keyForOpenItem(roadmapOpenItem(params, new URLSearchParams(search)))`. Note `useTriage` runs whenever the workspace runs (the count feeds the segment) — keep it ungated; `useRoadmapMap` stays gated on `active`.
`roadmapWorkspace.test.tsx`: add a case — at `/roadmap?scope=inbox&product=kindash` the transport is asked for `/pm/triage?projectSlug=kindash` and the column shows the Inbox scope current.
`RoadmapListColumn.test.tsx`: extend `mount` with a default `inbox` prop; add a case that `scope="inbox"` renders the `Inbox` segment link as current and the proposal rows.

- [ ] **Step 6: Gate + commit** — `(cd web && npx vitest run src/features/roadmap src/shell && npm run typecheck && npm run lint && npm run test)`

```bash
printf 'feat(roadmap): Inbox scope — proposals grouped by product on the list column, key map, bulk accept\n\nRefs #1467\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c4.txt
git add -A web/src/features/roadmap web/src/shell web/src/triage && git commit -F /tmp/c4.txt
```

---

### Task 5: The proposal editor as the detail pane; `RoadmapPage` Inbox branch

**Files:**
- Create: `web/src/features/roadmap/inbox/rice.ts` (+ `rice.test.ts`), `ProposalDetail.tsx` (+ `ProposalDetail.test.tsx`), `InboxWelcome.tsx`
- Modify: `web/src/features/roadmap/RoadmapPage.tsx` (+ `RoadmapPage.test.tsx`), `web/src/features/settings/pageContract.test.tsx`

**Interfaces:**
- `riceScore(reach, impact, effort, confidence): number | null` — mirrors `server/pm/score.py:rice_score`: `null` when any is `null`/`undefined` or effort is `0`, else `round2(reach*impact*confidence/effort)`.
- `ProposalDetail({ proposal: TriageProposal; capabilities: { id: string; slug: string; title: string | null; depth: number }[]; autoFocusScores: boolean; onDone: () => void })` — header: title (link when `url`), `repo#number`, product, proposed capability or no-fit suggestion, confidence badge, `createdAt`; body: four labelled number fields `reach / impact / effort / confidence` (`aria-label` lowercase, as the old editor) with a computed **Score** read-out; a capability `Select` over `capabilities` (value `featureId`, "— keep proposal —" default; a no-fit proposal's first option is "— no fit —"); rationale paragraph; actions **Accept** (`useAcceptProposal.mutate({ id })`), **Save edits & accept** (`useUpdateOverlay` when `workItemId` and any field changed, then accept with `edits`), **Reject**. After any action call `onDone()` (the page navigates to the next proposal or the inbox root).
- `InboxWelcome({ count: number; product: string | null })` — `EmptyState` "Pick a proposal" with the count, or "Inbox zero" when 0.
- `RoadmapPage`: when `scope === 'inbox'`: title `ROADMAP.inbox`, crumbs Roadmap › Inbox (› product when scoped); body = `ProposalDetail` for the open proposal (found in the `useTriage(product)` list; if the id is not in the list and the list has loaded → "Proposal not found — it may have been decided" `EmptyState` with a link to `inboxPath({ product })`), else `InboxWelcome`. `?edit=1` → `autoFocusScores`. `onDone` navigates to the next proposal in list order (`replace`), or to `inboxPath({ product })` when none.

- [ ] **Step 1: Failing tests**

`rice.test.ts`:

```ts
// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { riceScore } from './rice'
describe('riceScore mirrors server/pm/score.py', () => {
  it('computes reach*impact*confidence/effort to 2 dp', () => { expect(riceScore(3, 3, 2, 80)).toBe(360) ; expect(riceScore(1, 1, 3, 50)).toBe(16.67) })
  it('is null when any input is missing or effort is 0', () => {
    expect(riceScore(null, 3, 2, 80)).toBeNull(); expect(riceScore(3, 3, 0, 80)).toBeNull(); expect(riceScore(3, 3, undefined, 80)).toBeNull()
  })
})
```

`ProposalDetail.test.tsx`:

```tsx
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { ProposalDetail } from './ProposalDetail'

const accept = vi.fn(); const reject = vi.fn(); const overlay = vi.fn().mockResolvedValue({})
vi.mock('@/api/hooks', () => ({
  useAcceptProposal: () => ({ mutate: accept, isPending: false }),
  useRejectProposal: () => ({ mutate: reject, isPending: false }),
  useUpdateOverlay: () => ({ mutateAsync: overlay, isPending: false }),
}))
const proposal = { id: 'p1', workItemId: 'w1', projectSlug: 'kindash', repo: 'cairnea/KinDash', number: 7, kind: 'issue', title: 'Bulk edit',
  url: 'https://x/7', proposedFeatureId: 'f-tasks', proposedFeatureSlug: 'tasks', noFitSuggestion: null, reach: 3, impact: 3, effort: 2,
  confidence: 80, rationale: 'Touches the task list.', confidenceLevel: 'high', status: 'pending', createdAt: '2026-09-08T10:00:00Z' }
const caps = [{ id: 'f-tasks', slug: 'tasks', title: 'Tasks', depth: 0 }, { id: 'f-billing', slug: 'billing', title: 'Billing', depth: 0 }]
const mount = (over = {}) => { const onDone = vi.fn(); render(<MemoryRouter><ProposalDetail proposal={proposal} capabilities={caps} autoFocusScores={false} onDone={onDone} {...over} /></MemoryRouter>); return { onDone } }

describe('ProposalDetail', () => {
  beforeEach(() => { accept.mockReset(); reject.mockReset(); overlay.mockClear() })
  it('shows the proposal, scores and computed RICE', () => {
    mount()
    expect(screen.getByRole('link', { name: 'Bulk edit' })).toHaveAttribute('href', 'https://x/7')
    expect(screen.getByText('Touches the task list.')).toBeInTheDocument()
    expect(screen.getByLabelText('reach')).toHaveValue(3)
    expect(screen.getByText('360')).toBeInTheDocument()
  })
  it('Accept accepts as proposed and calls onDone', () => {
    const { onDone } = mount()
    fireEvent.click(screen.getByRole('button', { name: 'Accept' }))
    expect(accept).toHaveBeenCalledWith({ id: 'p1' }); expect(onDone).toHaveBeenCalled()
  })
  it('Save edits & accept patches the overlay then accepts with the edits', async () => {
    const { onDone } = mount()
    fireEvent.change(screen.getByLabelText('impact'), { target: { value: '9' } })
    fireEvent.change(screen.getByRole('combobox', { name: 'Capability' }), { target: { value: 'f-billing' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save edits & accept' }))
    await waitFor(() => expect(overlay).toHaveBeenCalledWith({ itemId: 'w1', edit: expect.objectContaining({ impact: 9, featureId: 'f-billing' }) }))
    expect(accept).toHaveBeenCalledWith({ id: 'p1', edits: expect.objectContaining({ impact: 9, featureId: 'f-billing' }) })
    expect(onDone).toHaveBeenCalled()
  })
  it('Reject rejects and calls onDone', () => {
    const { onDone } = mount()
    fireEvent.click(screen.getByRole('button', { name: 'Reject' }))
    expect(reject).toHaveBeenCalledWith({ id: 'p1' }); expect(onDone).toHaveBeenCalled()
  })
  it('autoFocusScores focuses the first score field', () => {
    mount({ autoFocusScores: true })
    expect(screen.getByLabelText('reach')).toHaveFocus()
  })
})
```

`RoadmapPage.test.tsx` additions: at `/roadmap?scope=inbox` with two mocked proposals the h1 is "Inbox" and `InboxWelcome` shows the count; at `/roadmap?scope=inbox&proposal=p1` the detail renders "Bulk edit"; after Accept the URL becomes `/roadmap?scope=inbox&proposal=p2`; an unknown `proposal` id shows "Proposal not found". `pageContract.test.tsx`: add `{ nav: 'roadmap', path: '/roadmap?scope=inbox', variant: 'standard', title: 'Inbox' }` (add `/pm/triage` → `[]` to the mock if absent).

- [ ] **Step 2: Run to fail**, then implement.

`rice.ts`:

```ts
/** Mirrors `server/pm/score.py:rice_score` — keep both in step. */
export function riceScore(reach: number | null | undefined, impact: number | null | undefined,
                          effort: number | null | undefined, confidence: number | null | undefined): number | null {
  if (reach == null || impact == null || effort == null || confidence == null || effort === 0) return null
  return Math.round((reach * impact * confidence / effort) * 100) / 100
}
```

`ProposalDetail.tsx` (structure; use `ui/Field`, `ui/Input`, `ui/Select`, `ui/Button`, `ui/Badge`; token classes):

```tsx
import { useEffect, useRef, useState } from 'react'
import type { OverlayEdits, TriageProposal } from '@/api/triage'
import { useAcceptProposal, useRejectProposal, useUpdateOverlay } from '@/api/hooks'
import { Badge } from '@/ui/Badge'
import { Button } from '@/ui/Button'
import { Field } from '@/ui/Field'
import { Input } from '@/ui/Input'
import { Select } from '@/ui/Select'
import { CAPABILITY } from '@/features/products/vocab'
import { scoreInputValue } from './inboxKeys'
import { riceScore } from './rice'

export interface CapabilityOption { id: string; slug: string; title: string | null; depth: number }
export interface ProposalDetailProps {
  proposal: TriageProposal
  capabilities: CapabilityOption[]
  autoFocusScores: boolean
  onDone: () => void
}

const FIELDS = ['reach', 'impact', 'effort', 'confidence'] as const

export function ProposalDetail({ proposal: p, capabilities, autoFocusScores, onDone }: ProposalDetailProps) {
  const accept = useAcceptProposal(p.projectSlug ?? undefined)
  const reject = useRejectProposal(p.projectSlug ?? undefined)
  const overlay = useUpdateOverlay()
  const [edits, setEdits] = useState<OverlayEdits>({ reach: p.reach ?? undefined, impact: p.impact ?? undefined, effort: p.effort ?? undefined, confidence: p.confidence ?? undefined, featureId: p.proposedFeatureId })
  const firstRef = useRef<HTMLInputElement>(null)
  useEffect(() => { setEdits({ reach: p.reach ?? undefined, impact: p.impact ?? undefined, effort: p.effort ?? undefined, confidence: p.confidence ?? undefined, featureId: p.proposedFeatureId }) }, [p])
  useEffect(() => { if (autoFocusScores) firstRef.current?.focus() }, [autoFocusScores, p.id])

  const changed = FIELDS.some((f) => (edits[f] ?? null) !== (p[f] ?? null)) || (edits.featureId ?? null) !== (p.proposedFeatureId ?? null)
  const score = riceScore(edits.reach, edits.impact, edits.effort, edits.confidence)

  const saveAndAccept = async () => {
    if (changed && p.workItemId) {
      try { await overlay.mutateAsync({ itemId: p.workItemId, edit: edits }) } catch { /* the hook toasts */ }
    }
    accept.mutate({ id: p.id, edits })
    onDone()
  }

  return (
    <div className="flex flex-col gap-4">
      <header className="flex flex-col gap-1">
        {p.url ? <a href={p.url} target="_blank" rel="noreferrer" className="text-lg font-semibold text-ink hover:underline">{p.title ?? p.id}</a>
               : <span className="text-lg font-semibold text-ink">{p.title ?? p.id}</span>}
        <div className="flex flex-wrap items-center gap-2 text-xs text-ink-muted">
          <span>{p.repo}#{p.number}</span>
          {p.projectSlug && <span>· {p.projectSlug}</span>}
          {p.proposedFeatureSlug ? <Badge tone="info">{p.proposedFeatureSlug}</Badge> : <Badge tone="warning">no fit → {p.noFitSuggestion ?? '?'}</Badge>}
          <Badge tone={p.confidenceLevel === 'high' ? 'success' : p.confidenceLevel === 'medium' ? 'info' : 'neutral'}>{p.confidenceLevel ?? 'low'} confidence</Badge>
          {p.createdAt && <span>· proposed {new Date(p.createdAt).toLocaleString()}</span>}
        </div>
      </header>
      {p.rationale && <p className="text-sm text-ink">{p.rationale}</p>}
      <form className="flex flex-col gap-3" onSubmit={(e) => { e.preventDefault(); void saveAndAccept() }}>
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          {FIELDS.map((f, i) => (
            <Field key={f} label={f} htmlFor={`score-${f}`}>
              <Input id={`score-${f}`} type="number" aria-label={f} ref={i === 0 ? firstRef : undefined}
                     value={edits[f] ?? ''} onChange={(e) => setEdits((s) => ({ ...s, [f]: scoreInputValue(e.target.value) }))} />
            </Field>
          ))}
        </div>
        <p className="text-sm text-ink-muted">Score: <span className="font-semibold tabular-nums text-ink">{score ?? '—'}</span></p>
        <Field label={CAPABILITY.singular} htmlFor="proposal-capability">
          <Select id="proposal-capability" aria-label={CAPABILITY.singular} value={edits.featureId ?? ''} onChange={(e) => setEdits((s) => ({ ...s, featureId: e.target.value || null }))}>
            <option value="">{p.proposedFeatureId ? '— keep proposal —' : '— no fit —'}</option>
            {capabilities.map((c) => <option key={c.id} value={c.id}>{'  '.repeat(c.depth)}{c.title ?? c.slug}</option>)}
          </Select>
        </Field>
        <div className="flex flex-wrap justify-end gap-2">
          <Button type="button" variant="ghost" size="lg" onClick={() => { reject.mutate({ id: p.id }); onDone() }} disabled={reject.isPending}>Reject</Button>
          <Button type="button" variant="secondary" size="lg" onClick={() => { accept.mutate({ id: p.id }); onDone() }} disabled={accept.isPending}>Accept</Button>
          <Button type="submit" size="lg" disabled={!changed || accept.isPending || overlay.isPending}>Save edits &amp; accept</Button>
        </div>
      </form>
    </div>
  )
}
```

`capabilities` come from the map: in `RoadmapPage`, flatten `findProduct(map, proposal.projectSlug)?.capabilities` depth-first (reuse the `flatten` helper pattern in `NewInitiativeDialog.tsx` — extract it to `web/src/features/roadmap/lookup.ts` as `flattenCapabilities(nodes, depth = 0)` and have the dialog use it too, constitution VI).

`RoadmapPage` inbox branch: `const scope = roadmapScopeForSearch(search)`; when `scope === 'inbox'`: `const product = inboxProductForSearch(search)`, `const triage = useTriage(product ?? undefined)`, `const proposal = item?.kind === 'proposal' ? triage.data?.find((x) => x.id === item.proposalId) : undefined`, title `ROADMAP.inbox`, crumbs `[Roadmap → roadmapPath(), Inbox]` (+ product), `edit = new URLSearchParams(search).get('edit') === '1'`, `onDone = () => { const list = triage.data ?? []; const i = list.findIndex((x) => x.id === item.proposalId); const next = list[i + 1] ?? list[i - 1]; navigate(next ? inboxPath({ product, proposal: next.id }) : inboxPath({ product }), { replace: true }) }`. Render order: Banner on `triage.isError`; `proposal` → `<ProposalDetail …/>`; `item?.kind === 'proposal' && triage.data && !proposal` → not-found `EmptyState`; else `<InboxWelcome count={triage.data?.length ?? 0} product={product} />`. No product actions in this scope.

- [ ] **Step 3: Gate + commit** — `(cd web && npx vitest run src/features/roadmap src/features/settings/pageContract.test.tsx && npm run typecheck && npm run lint && npm run test)`

```bash
printf 'feat(roadmap): proposal editor as the Inbox detail pane; RoadmapPage inbox branch\n\nRefs #1467\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c5.txt
git add -A web/src/features/roadmap web/src/features/settings && git commit -F /tmp/c5.txt
```

---

### Task 6: Delete the Triage section — nav row, badge, redirects, links, docs, gates

**Files:**
- Modify: `web/src/shell/navDefs.ts` (+ `.test.ts`), `web/src/shell/useNavBadges.ts` (+ test), `web/src/shell/routes.tsx` (+ `.test.tsx`), `web/src/features/assistant/starters.ts`, `web/src/shell/roadmapWorkspace.test.tsx`, `web/src/features/settings/pageContract.test.tsx`, `web/src/features/roadmap/detail/NeedsYouList.tsx` (+ `PortfolioDetail.test.tsx`), `web/src/projects/OnboardingWizard.tsx` (+ test), `web/src/features/roadmap/useRoadmapWorkspace.ts`
- Delete: `web/src/triage/TriageInbox.tsx`, `web/src/triage/triage.css`, `web/src/triage/__tests__/TriageInbox.test.tsx` (its reducer cases moved in Task 4), the now-empty `web/src/triage/` directory
- Docs: `docs/product/features/product-management-layer.md`, `docs/superpowers/specs/2026-09-07-roadmap-drilldown-design.md` (status line), `docs/superpowers/specs/2026-09-08-finish-line-persona-modes-design.md` (§5 row 3 → "landed in #<PR>")

**Interfaces:**
- `NavId` loses `'triage'`; `NavBadgeKey` gains `'roadmap'`; the `roadmap` nav row gets `badge: 'roadmap'`; `useNavBadges` computes `roadmap: pending.length || undefined` from `useTriage()`.
- Routes: `/triage` → `<RedirectTriage />` which reads `?projectSlug=` and navigates (replace) to `inboxPath({ product: projectSlug })`.
- Links: `NeedsYouList` and `OnboardingWizard` "awaiting triage" links → `inboxPath({ product: slug })`; tests updated to `/roadmap?scope=inbox&product=<slug>`.

- [ ] **Step 1: Update the pinned tests first**

`navDefs.test.ts`: order/labels loses `['triage','Triage']`; badge-keys set gains `'roadmap'`; add `expect(NAV_BY_ID.roadmap.badge).toBe('roadmap')`. `useNavBadges` test (find it: `grep -rl useNavBadges web/src/shell/*.test.*`): add a case that two pending proposals yield `badges.roadmap === 2` and zero yields `undefined`. `routes.test.tsx`: drop the `TriageInbox` mock and `['/triage','page-triage']` row; update the "17 non-redirect paths" / "68 cells" counts (16 / 64) and the kiosk-refusal array; add redirect rows `['/triage', '/roadmap?scope=inbox']` and `['/triage?projectSlug=kindash', '/roadmap?scope=inbox&product=kindash']`. `pageContract.test.tsx`: delete the `triage` row. `roadmapWorkspace.test.tsx`: drop `'/triage'` from the "map not funded" list. `PortfolioDetail.test.tsx:55` → `/roadmap?scope=inbox&product=kindash`; `OnboardingWizard.test.tsx:135` → `/roadmap?scope=inbox&product=ccswitch`.

- [ ] **Step 2: Run to fail** — `(cd web && npx vitest run src/shell src/features/settings src/features/roadmap/detail src/projects)`

- [ ] **Step 3: Implement**

`navDefs.ts`: delete the `triage` row and the now-unused `Inbox` import (if `Inbox` is used by nothing else there); `NavBadgeKey = 'sessions' | 'pipeline' | 'automations' | 'roadmap' | 'settings'`; roadmap row gains `badge: 'roadmap'`; update the `help` if it mentions Triage as a separate section.
`useNavBadges.ts`: `const { data: pending = [] } = useTriage()`; `roadmap: pending.length || undefined`.
`starters.ts`: delete the `triage:` entry; move its two strings under `roadmap:` if the corpus test (`starters.coverage.test.ts`) still passes with them there (they are corpus-covered); otherwise drop the first and keep the second.
`routes.tsx`: remove the `TriageInbox` import, `TriagePage`, and its route row; add

```tsx
/** `/triage[?projectSlug=x]` → the Inbox scope of Roadmap. Bookmarks only. */
function RedirectTriage() {
  const [params] = useSearchParams()
  return <Navigate to={inboxPath({ product: params.get('projectSlug') })} replace />
}
```

and `{ path: '/triage', element: <RedirectTriage /> }` in the legacy block (import `inboxPath` from `@/features/roadmap/roadmapNavState`).
`NeedsYouList.tsx` / `OnboardingWizard.tsx`: `to={inboxPath({ product: p.slug })}` (drop the `NAV_BY_ID.triage` import).
`useRoadmapWorkspace.ts`: confirm `onOpenInbox` uses `inboxPath` (Task 4) and nothing references `NAV_BY_ID.triage`.
Delete the files: `git rm web/src/triage/TriageInbox.tsx web/src/triage/triage.css web/src/triage/__tests__/TriageInbox.test.tsx`; `rmdir` what is left. `grep -rn "triage/\|TriageInbox\|triage.css\|NAV_BY_ID.triage\|'triage'" web/src` must return only the unrelated `session/triageTier.ts`, `listFilters.ts`, `/sdlc/triage` incident references and `QK.triage`.

- [ ] **Step 4: Docs**

`docs/product/features/product-management-layer.md`: replace every mention of the Triage page/section with "the Inbox scope of the Roadmap section (`/roadmap?scope=inbox`)"; keep the frontmatter parseable. Roadmap spec header: append "Phase 3 landed in #1478; §12 (Inbox scope + undo) landed in #<PR>; remaining: Phases 4–6." Finish-line spec §5 row 3: mark landed. Feature doc `code:` list: replace `web/src/triage/*` with `web/src/features/roadmap/inbox/*`.

- [ ] **Step 5: Full gates**

```bash
(cd web && npm run typecheck && npm run lint && npm run test && npm run build) && rm -rf web/dist
.venv/bin/ruff check src/ server/ && .venv/bin/mypy server/ --ignore-missing-imports
env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 CCSWITCH_DEV=1 .venv/bin/python -m pytest tests/ -q -p no:randomly
# full server suite with the slot acquire in the SAME shell (see CLAUDE.md), tee'd to /tmp/roadmap-inbox-server-tests.log
```

Confirm by hand: nav has six entries; `/triage?projectSlug=kindash` lands on the Inbox scoped to kindash; the Roadmap nav badge equals the Inbox scope count; accept → toast with Undo → undo restores the row.

- [ ] **Step 6: Commit**

```bash
printf 'feat(roadmap): Triage becomes the Inbox scope — nav row deleted, badge on Roadmap, /triage redirects\n\nFixes #1467\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c6.txt
git add -A web/src docs && git commit -F /tmp/c6.txt
```

---

## Self-review notes

- Spec coverage: finish-line D3 (Inbox scope + badge on Roadmap) → Tasks 4, 6; §4.2 route/redirect table (`/roadmap?scope=inbox`, `&proposal=`, `/triage` redirect) → Tasks 3, 5, 6; §4.3 four scopes minus Queue → Task 4; roadmap §12 list column (grouped by product, scope chip, confidence grouping instead of a separate filter, bulk accept) → Task 4; §12 detail pane (excerpt = title/rationale, capability select, four labelled fields with computed score, rationale/confidence/when, Accept / Save edits & accept / Reject) → Task 5; §12 undo (six-second, server-backed) → Tasks 1, 2; §12 "Needs a home" Reclassify → deferred (Rulings). §10 undo endpoint → Task 1.
- Type consistency: `inboxPath({ product?, proposal? })` used in Tasks 3–6; `roadmapOpenItem(params, search: URLSearchParams)` in Tasks 3–5 and the shell; `InboxScopeBodyProps.onOpen(p, { edit })` matches `useRoadmapWorkspace.onOpenProposal(p, opts)` and the `e` key; `ProposalDetailProps` fixed in Task 5 and consumed by `RoadmapPage` there; `proposalKey` from Task 3 used by Task 4's `selected`.
- Judgement calls an implementer may hit: `GroupedList` may wrap rows in `<li>` (then `ProposalCard` is a `<div>`); `SessionListControls` generic prop names; whether `ui/Kbd` is named `Kbd`; `useTriage(undefined)` vs `useTriage()` key equality (`[...QK.triage, null]` in both — fine).
