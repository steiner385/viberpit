# Capability management + inline triage creation — design

Status: approved by user, ready for implementation plan.

## Problem

The PM layer has no way to retire or merge a capability (`Feature` row), and no way
to move initiatives/issues between capabilities in bulk. Triage can only classify a
work item onto a capability that already exists — a proposal with no good fit has to
be rejected or left pending, rather than spinning up the right capability on the
spot. This spec adds: capability retire, capability merge, a re-map action for
initiatives/issues, a Products → Capabilities tab to drive all three, and inline
capability creation during triage resolution.

## Existing system this builds on

- **Capability = `Feature` row** (`server/db/models.py:1708`), one per
  `docs/product/features/<slug>.md`. The markdown doc is truth; the DB row is an
  index. `FEATURE_LIFECYCLE_STATES = (proposed, discovery, delivering, live,
  sunset)` (`models.py:1681`) — `sunset` already exists as the retired state.
- **Every capability doc edit is a git commit on the product's long-lived
  `pm/map-changes-<slug>` branch**, PR opened on first edit
  (`server/pm/map_changes.py::MapChangesService.edit`). Pure op validation/application
  lives in `server/pm/map_changes_ops.py` (`MAP_OPS = (create, rename, reparent,
  summary, links)`). One op = one commit; the API route is `POST
  /api/pm/projects/{slug}/map-changes` (`server/api/pm.py:1116`).
- **`InitiativeFeature`** (`models.py:2102`) is the initiative↔capability
  many-to-many join, `is_primary: bool`, one row per pair. **`WorkItem.feature_id`**
  (`models.py:1843`) is a direct single-capability FK for issues — no join table.
  **`TriageProposal.proposed_feature_id`** (`models.py:1982`) is the AI's suggested
  capability for a pending proposal.
- **A new capability's DB `Feature` row does not exist until the next background
  pm-sync pass** (`server/automations/pm_sync.py`, `PmSyncLoop`, default interval
  300s). `MapChangesService.edit()` only stamps a watermark
  (`server/pm/sync_request.py::request_project_sync`); nothing in the request path
  runs the indexer. Confirmed by `NewCapabilityDialog.tsx`'s own comments and its
  toast copy ("appears on the map at the next sync"). The indexer itself
  (`plan_feature_index` in `server/pm/feature_index.py`, applied via
  `SessionOrchestrator._pm_sync_apply_feature_plan`) is a plain async method taking
  `project_id`/`plan` — not coupled to `PmSyncLoop`'s bookkeeping — so it can be
  called directly from a request handler. It does a full re-read of every capability
  doc in the repo via local `git show` (no GitHub API calls), cost linear in doc
  count.
- **Triage accept** (`POST /api/pm/triage/{proposal_id}/accept` →
  `server/pm/triage_accept.py::apply_accept`) already accepts `edits.featureId` to
  classify the resulting work item onto an existing capability in the same call.
- **Initiative capability re-map already exists** for a single initiative: `PATCH
  /api/sdlc/initiatives/{slug}` (`server/api/sdlc.py:643`) takes
  `capabilityIds`/`primaryCapabilityId`, replacing the full touched set.
  **Work-item re-map already exists** for a single issue: `PATCH
  /api/pm/work-items/{item_id}/overlay` (`pm.py:1698`) takes `featureId`.
- **Products UI** (`web/src/features/roadmap/detail/ProductDetail.tsx`) has tabs
  `overview | in-flight | needs-you | ranked | dependencies | automation` — no
  Capabilities tab. Capability create/rename/reparent/summary/links already have
  dialogs (`NewCapabilityDialog.tsx`, `EditCapabilityDialog.tsx`,
  `CapabilityLinksEditor.tsx`) reachable from the roadmap tree, each a single
  map-changes op, one commit.

## Decisions

1. **Retire is a new `retire` doc op**, uniform for merged and proposed
   capabilities alike — sets frontmatter `state: sunset`, goes through the same
   commit+PR flow as every existing op. Reversible via a later edit, reviewable
   like any other capability change. (Rejected: hard-deleting the doc file for
   never-merged capabilities, mirroring `cleanup_after_abandon`'s per-PR-abandon
   delete-vs-orphan branching — adds a second code path for a single-capability
   action that the uniform sunset-op avoids.)
2. **Retiring a capability with live children does not block.** Each child is
   auto-reparented to the retiring capability's own parent (the grandparent) via
   one `reparent` commit per child, reusing the existing op. The guard only checks
   for `WorkItem`/`InitiativeFeature` dependents, not children.
3. **New-capability-during-triage materializes synchronously.** After committing
   the `create` doc op, the handler runs the pm-sync indexer inline (read docs →
   plan → apply, scoped to this one product) before returning, so the `Feature` row
   exists and the work item can be classified onto it in the same request. This is
   new latency scoped to the new endpoints only (retire, merge, triage-inline-create)
   — existing create/rename/reparent/summary/links dialogs keep their current async
   lag; no behavior change requested or made there.
4. **No new bulk re-map endpoint.** The re-map action UI fires the existing
   single-item `PATCH /api/sdlc/initiatives/{slug}` and `PATCH
   /api/pm/work-items/{item_id}/overlay` once per selected item.

## Architecture

### 1. `server/pm/map_changes_ops.py` — new `retire` op

- `MAP_OPS = ("create", "rename", "reparent", "summary", "links", "retire")`.
- `validate_op`: `retire` requires the slug to exist (same `not_found` branch as
  every non-create op); no other fields required.
- `apply_op`: `retire` → `replace(doc, state="sunset")` (mirrors the existing
  `rename`/`reparent`/`summary`/`links` `replace()` calls — every other field
  carried forward unchanged).
- `commit_message`: verb `"retire capability"`.

### 2. `server/pm/map_changes.py` — `MapChangesService.retire()` and `.merge()`

```python
async def retire(self, db, project: Project, slug: str, identity: str) -> Pointer:
    """Guard on live dependents, auto-reparent children to the grandparent,
    commit `retire`, then resync this product inline so the tree and state
    are consistent before the response returns."""
```

- Guard (409 `has_dependents`): query `WorkItem.feature_id == target.id` and
  `InitiativeFeature.feature_id == target.id`; any row refuses before any commit is
  made. Error shape matches the existing `_map_changes_http` convention (`detail:
  {error, message}`).
- Resolve target's children: `Feature.parent_feature_id == target.id,
  Feature.orphaned.isnot(True)`. For each, commit a `reparent` op via
  `MapOp(op="reparent", slug=child.slug, parent_slug=target.parent_slug)` through
  the same `_read_and_apply`/`commit_file` path `edit()` uses (factored into a
  shared `_reparent_children(gh, branch, base_sha, children, new_parent_slug,
  project, identity)` helper — used by both `retire` and `merge`).
- Commit `retire` (`MapOp(op="retire", slug=target.slug)`) for the target itself.
- Call the new `_resync_now(db, project)` helper (below).
- `record_audit(type="pm.map_changes.retire", detail={slug, capability, childrenReparented})`.

```python
async def merge(self, db, project: Project, from_slug: str, into_slug: str,
                 identity: str) -> Pointer:
    """Re-point every WorkItem/InitiativeFeature/FeatureLink/TriageProposal
    reference from `from_slug` to `into_slug`, reparent from_slug's children
    onto into_slug, retire from_slug, resync inline."""
```

- Validate: `from_slug != into_slug`; both exist; `into_slug` is not a descendant
  of `from_slug` (reuses `map_changes_ops._is_ancestor` — a merge target below the
  source would be a cycle once children reparent onto it).
- DB re-pointing, one transaction, direct FK writes (no doc involved — these
  relationships aren't doc fields):
  - `UPDATE work_items SET feature_id = into.id WHERE feature_id = from.id`
  - `InitiativeFeature`: for each row with `feature_id = from.id`, check for an
    existing `(initiative_id, into.id)` row. If present: `is_primary` becomes
    `existing.is_primary OR moving.is_primary`; delete the `from.id` row. If
    absent: update `feature_id` to `into.id` in place.
  - `FeatureLink`: for each row with `src_feature_id = from.id` (or
    `dst_feature_id = from.id`), check for an existing row with the same
    `(other_side, kind)` pointed at `into.id`; if present, drop the `from`-side
    row (avoid duplicate edges), else rewrite the FK in place.
  - `UPDATE triage_proposals SET proposed_feature_id = into.id WHERE
    proposed_feature_id = from.id`
- Children of `from_slug` reparent to `into_slug` (not the grandparent) via the
  shared `_reparent_children` helper.
- Commit `retire` for `from_slug`.
- `_resync_now(db, project)`.
- `record_audit(type="pm.map_changes.merge", detail={from, into, workItems: n,
  initiatives: n, links: n, childrenReparented: n})`.

### 3. `server/pm/map_changes.py` — `_resync_now(db, project)`

New private helper, used only by `retire`/`merge`/triage-inline-create (item 4):
calls the same three seams `PmSyncLoop._feature_index` uses —
`orch._pm_sync_read_feature_docs(repo)` → `orch._pm_sync_list_feature_rows(project.id)`
→ `plan_feature_index(...)` → `orch._pm_sync_apply_feature_plan(project.id, plan)` —
against this one project, synchronously, in the same request. `MapChangesService`
gains an `orch` reference (or an injected callable bundle, matching how
`gh_factory`/`default_branch` are already injected) so it can reach these methods
without importing `SessionOrchestrator` directly — mirrors how `pm_map_changes()`
already wires the service with orchestrator-owned seams (`orchestrator.py:10122`).

### 4. `server/api/pm.py` — new routes

- `POST /api/pm/projects/{slug}/map-changes/retire` — body `{slug: str}`. Calls
  `svc.retire`; maps `MapOpError`/dependents-guard to `_map_changes_http` codes
  (`has_dependents` → 409, `not_found` → 404).
- `POST /api/pm/projects/{slug}/map-changes/merge` — body `{from: str, into:
  str}`. Calls `svc.merge`; same error mapping plus `parent_cycle` → 409.

Both follow the existing route shape: `identity: str = Depends(current_identity)`,
`db=Depends(get_db)`, hand-built camelCase response dict (`pointer`, plus repointed
counts for merge).

### 5. `server/pm/triage_accept.py` + `server/api/pm.py` — inline capability creation

- `accept_triage`'s `edits` body gains an alternative field:
  `newCapability: {title: str, slug: str} | None`. Mutually exclusive with
  `featureId` (400 `bad_edit` if both given).
- When `newCapability` is present: before calling `apply_accept`, the route calls
  `svc.edit(db, project, MapOp(op="create", slug=..., title=...), identity)` (the
  existing `create` op, unchanged), then `svc._resync_now(db, project)`, then looks
  up the newly-indexed `Feature.id` by slug and passes it through as
  `edits["featureId"]` into the existing `apply_accept` path — no change to
  `apply_accept` itself. Project is resolved from the `TriageProposal`'s product,
  same as today.
- 409 `slug_taken` if the slug collides with an existing/branch-proposed capability
  (surfaced from the underlying `create` op, same as `NewCapabilityDialog`'s
  existing error handling).

### 6. Web — `web/src/features/products/CapabilitiesTab.tsx` (new)

New tab in `ProductDetail.tsx`'s tab list (`overview | in-flight | needs-you |
ranked | dependencies | automation | capabilities`), per `docs/PAGE-TABS.md`
conventions. Contents:
- Capability list (reuse the existing tree data from `GET /api/pm/map`), each row
  showing dependent counts (initiatives/issues mapped).
- **Retire** action per capability → confirmation dialog (states child
  auto-reparent + shows the blocking dependent count if guard would refuse) → new
  retire endpoint.
- **Merge** action → picker for the target capability → new merge endpoint,
  confirmation shows repointed counts from the response.
- **Re-map** action: multi-select initiatives/issues (scoped to one capability's
  dependents), a target-capability picker, and a primary toggle. On submit, fires
  the existing single-item PATCH endpoints once per selected item (parallel
  `Promise.all`, matching the existing bulk-accept UI pattern in the triage inbox
  for progress/error reporting per item).
- Rename reuses the existing `EditCapabilityDialog`.

New React Query hooks in `web/src/api/hooks.ts`: `useRetireCapability`,
`useMergeCapabilities`, following the existing `useEditMapChanges` shape
(`onSuccess` invalidates `QK.roadmapMap` and `QK.project(slug)`).

### 7. Web — `ProposalDetail.tsx` inline capability creation

The existing capability `<Select>` (line ~211) gains a "+ New capability" option.
Selecting it reveals inline `title`/`slug` fields (slug auto-derived from title,
editable, same validation as `NewCapabilityDialog`) in place of the select. Submit
sends `edits.newCapability` instead of `edits.featureId` via the existing
`useAcceptProposal` hook (extend its body type). No parent picker in v1 — a
triage-created capability lands at root and can be reparented later from the
Capabilities tab.

## Error handling

- All new guard/validation failures use the existing `MapOpError` →
  `_map_changes_http` translation (`server/api/pm.py:1088`), so error shape is
  consistent with every other map-changes endpoint: `{"error": code, "message":
  ...}`.
- `_resync_now` failures (e.g. a transient local-git read error) propagate as a 500
  — the doc commit has already landed at that point, so the operation is not
  silently lost; a subsequent background pm-sync tick will still catch it up. This
  matches the existing posture that doc commits are the durable side effect and
  DB indexing is a projection of them.

## Testing

- `server/tests/` — `test_map_changes_ops.py` (or wherever `retire`'s
  validate/apply cases join `rename`/`reparent`): retire sets state=sunset,
  refuses on missing slug, leaves other fields untouched (labels included, same
  regression class as the existing rename/reparent/summary/links tests).
- New/extended `test_map_changes_service.py` (or equivalent): `retire()` refuses
  with dependents present (both `WorkItem` and `InitiativeFeature` cases), auto-
  reparents children and leaves no dangling `parent_feature_id`, commits land on
  the branch, `_resync_now` result reflected in `Feature.state`. `merge()`:
  `WorkItem`/`InitiativeFeature`/`FeatureLink`/`TriageProposal` all repointed;
  `InitiativeFeature` dedup preserves `is_primary` correctly in both directions;
  cycle rejected; children land under `into_slug`.
- `server/tests/test_pm_api.py` (or equivalent) — new retire/merge routes: success
  shape, 404/409 mappings, audit row written.
- `server/tests/test_triage_accept.py` — `newCapability` + `featureId` together is
  400; `newCapability` creates the doc, resyncs, and classifies the work item onto
  the new `Feature.id` in one call; slug collision surfaces 409.
- Web: `CapabilitiesTab.test.tsx` (retire/merge/re-map flows against mocked
  hooks, matching the existing `@/api/hooks` mock convention — **run the full
  `npm run test`**, not just changed files, since shared components are widely
  mocked). `ProposalDetail.test.tsx` extended for the inline-create path.

## Out of scope

- No change to the async lag of existing create/rename/reparent/summary/links
  dialogs.
- No parent picker for triage-inline-created capabilities (root-level only; can be
  reparented afterward from the Capabilities tab).
- No bulk re-map API endpoint — the UI drives the existing single-item PATCHes.
- No change to `MAP_OPS`' existing five ops beyond adding `retire`.
- No un-retire / restore-from-sunset action (a capability can be un-sunset via the
  ordinary `rename`/`summary`-style edit machinery if ever needed — not built here).

## PR series

1. `retire` op + `MapChangesService.retire()` (guard, auto-reparent children,
   `_resync_now` helper) + API route + tests.
2. `MapChangesService.merge()` (reuses PR1's reparent/resync helpers) + API route
   + tests.
3. Products → Capabilities tab UI (list, retire, merge, re-map action).
4. Triage inline capability creation (backend `newCapability` edit + `ProposalDetail.tsx`).
