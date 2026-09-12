# Finish-line row 8c — Map-changes PRs, web Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put the capability-document edits (add, rename, reparent, summary, links) and the map-changes PR affordance (open PR with its age, Rebase when behind, Abandon, conflict banner, "proposed in PR #N" on rows) into the roadmap UI over the endpoints 8b shipped — closing finish-line row 8 (#1472).

**Architecture:** Three fetchers and three hooks in the existing `api/pm.ts` + `api/hooks.ts` shape (invalidate the map; toast on error, with the 409 `conflict` and 502 `github` shapes given their own copy). Edits are small dialogs opened from the capability page's Overview (the idiom of `EditInitiativeDialog`), each sending one op per request (one commit each — the server's contract). The PR card is a `Banner`-based `MapChangesCard` on the product page's Overview reading `product.mapChanges` from the map payload; the list column's tree rows badge a capability's `prNumber`.

**Tech Stack:** React 19, react-query, Tailwind v4 design tokens, vitest + testing-library, Playwright.

**Spec:** `docs/superpowers/specs/2026-09-07-roadmap-drilldown-design.md` §6 ("row shows **proposed** + PR number until merge"), §8 rows "Add / rename capability, reparent, edit summary — Inline row input (`a`, `e`)" and "Capability links — Chip editor on Overview", §9 ("The product header shows the open PR, its age, and offers Rebase when it is behind. Abandoning closes the PR and drops the pointer."; "Conflicts … surface as a `Banner` on the product with a link to the PR"), §14 (page-contract rows unchanged; Playwright). Backend contract: `docs/superpowers/plans/2026-09-12-finish-line-row-8b-map-changes-backend.md` (R8b-4 amended, R8b-7/8/10/11/12) and `server/api/pm.py` `MapChangeBody`, `_map_changes_http`, `Pointer.to_dict()`.

## Design rulings (R8c-1..6)

- **R8c-1 Edits are dialogs, not inline row inputs.** §8 says "inline row input (`a`, `e`)" for the list column; the list column's keyboard layer is separate work and the detail pane is where every other edit lives (R8a-3 precedent). The capability Overview gets **Edit** (title + summary + parent in one dialog, sent as up to three ops in sequence: `rename`, `reparent`, `summary` — only the changed ones) and **+ Sub-capability** (`create` with `parentSlug` preset); the product Overview gets **+ Capability** (`create`, root). Cost if wrong: a keyboard-first PM waits for the list-column keys.
- **R8c-2 Links are a chip editor on the Overview** (§8): the current chips with a remove each, and one add row (kind, product, capability) — every change sends the full replaced list as one `links` op.
- **R8c-3 The PR card lives on the product page's Overview** (above the onboarding wizard) and a one-line note on a capability's Overview ("Changes pending in PR #N") — the product page IS the header §9 names (the shell's `PageHeader` carries breadcrumbs and `+ Initiative`, not status).
- **R8c-4 Abandon confirms with a typed slug** (`ConfirmDialog` `typed=`): it closes a PR and deletes a branch and every proposed-only capability — the roadmap's one destructive action.
- **R8c-5 A 409 `conflict` reads "The map changed under you; try again"** and re-enables the form with the user's values kept; a 502 `github` says which step failed.
- **R8c-6 The slug is derived from the title exactly as the initiative dialog does** — extract `sanitize`/`slugify` from `NewInitiativeDialog.tsx` into `features/roadmap/slug.ts` and use them in both (constitution VI).

## Global Constraints

- Design tokens only (`ccswitch/no-raw-palette` covers the tree); a primary confirm in a dialog is `<Button size="lg">`; a dialog is named by `title` alone; every icon-only control has an accessible name; vocabulary from `web/src/features/products/vocab.ts` (`CAPABILITY`, `PRODUCT`, `INITIATIVE`).
- Every mutation invalidates the map (`QK.roadmapMap`) and the product (`QK.project(slug)`) and toasts on error (`PM_MUTATION_META`, `extractMutationErrorMessage`), as the 8a hooks do.
- Web gate on every task, every exit code checked, never piped through `tail`: `(cd web && npm run typecheck; echo rc=$?; npm run lint; echo rc=$?; npm run test > /tmp/8c-tN-vitest.log 2>&1; echo rc=$?; grep -E "Test Files|Tests " /tmp/8c-tN-vitest.log; grep -c Unhandled /tmp/8c-tN-vitest.log; npm run build > /tmp/8c-tN-build.log 2>&1; echo rc=$?); rm -rf web/dist`. Playwright on the last task.
- Commits: one per task, `feat(roadmap): 8c task N — <what>` / blank / paragraph / blank / `Refs #1472` / blank / `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`; via `git commit -F /tmp/row8c-tN.msg`. Never push, never open a PR, never rebase.
- Run from the worktree root with absolute paths or `(cd web && …)`.

## File structure

| File | Responsibility |
|---|---|
| `web/src/api/pm.ts` | `MapChangeInput`, `MapChangeResult`, `editMapChanges`, `abandonMapChanges`, `rebaseMapChanges` |
| `web/src/api/hooks.ts` | `useEditMapChanges(slug)`, `useAbandonMapChanges(slug)`, `useRebaseMapChanges(slug)`, `mapChangesErrorMessage(e)` |
| `web/src/features/roadmap/slug.ts` | `sanitize`, `slugify` (moved from `NewInitiativeDialog.tsx`) |
| `web/src/features/roadmap/detail/EditCapabilityDialog.tsx` | title / summary / parent → `rename` / `summary` / `reparent` ops |
| `web/src/features/roadmap/detail/NewCapabilityDialog.tsx` | title → slug, summary, parent preset → `create` |
| `web/src/features/roadmap/detail/CapabilityLinksEditor.tsx` | chips + add row → `links` op |
| `web/src/features/roadmap/detail/MapChangesCard.tsx` | the open PR: number/link, age, behind → Rebase, conflicting → danger banner, Abandon (typed confirm) |
| `web/src/features/roadmap/detail/CapabilityDetail.tsx`, `ProductDetail.tsx` | mounts |
| `web/src/features/roadmap/rows/TreeRowCard.tsx` | `PR #N` badge on a capability row with `prNumber` |
| `docs/product/features/product-management-layer.md` | "UI landed" note |

Task order: 1 → 2 → 3 → 4 (4 is rows + docs + Playwright; 2 and 3 both need 1 and share `CapabilityDetail`/`ProductDetail` test mocks, so sequential).

---

### Task 1: Fetchers and hooks

**Files:**
- Modify: `web/src/api/pm.ts` (after `deleteEdge`)
- Modify: `web/src/api/hooks.ts` (after `useDeleteEdge`)
- Test: `web/src/api/__tests__/mapChangesHooks.test.tsx` (the shape of `edgeHooks.test.tsx` from 8a)

**Interfaces:**
- Consumes: `POST /api/pm/projects/{slug}/map-changes` body `{op, slug, title?, parentSlug?, summary?, links?: {kind, project, feature}[]}` → `{pointer: MapChangesPointer, path: string, commitSha: string}`; `DELETE …/map-changes` → `{pointer, cleanup: {deleted, orphaned}}`; `POST …/map-changes/rebase` → `{pointer}`; errors `422 {error: bad_slug|slug_taken|not_found|parent_not_found|parent_cycle|bad_link|bad_title|bad_summary|bad_op|no_primary_repo}`, `404 no_open_map_changes`, `409 conflict {step}` / `409 not_behind`, `502 github {step}` (read `server/api/pm.py:703-741` and `_map_changes_http`). `ApiRequestError` carries `status` and the parsed `detail` (check `web/src/api/client.ts`).
- Produces:

```ts
export type MapChangeOp = 'create' | 'rename' | 'reparent' | 'summary' | 'links'
export interface MapChangeLink { kind: 'powered_by' | 'integrates_with'; project: string; feature: string }
export interface MapChangeInput {
  op: MapChangeOp; slug: string; title?: string; parentSlug?: string | null; summary?: string; links?: MapChangeLink[]
}
export interface MapChangeResult { pointer: MapChangesPointer; path: string; commitSha: string }
export interface AbandonResult { pointer: MapChangesPointer; cleanup: { deleted: number; orphaned: number } }
export function editMapChanges(slug: string, input: MapChangeInput): Promise<MapChangeResult>
export function abandonMapChanges(slug: string): Promise<AbandonResult>
export function rebaseMapChanges(slug: string): Promise<{ pointer: MapChangesPointer }>
// hooks.ts
export function mapChangesErrorMessage(e: unknown, fallback: string): string   // R8c-5: 409 conflict → "The map changed under you — try again."; 502 github → `GitHub refused the ${step} step: ${message}`; 422 → the server's message; else fallback
export function useEditMapChanges(slug: string)     // useMutation<MapChangeResult, ApiRequestError, MapChangeInput>
export function useAbandonMapChanges(slug: string)  // useMutation<AbandonResult, ApiRequestError, void>
export function useRebaseMapChanges(slug: string)   // useMutation<{pointer}, ApiRequestError, void>
```

- [ ] **Step 1: Write the failing hook tests** (`mapChangesHooks.test.tsx`): with a mocked `api` (the pattern in `web/src/api/__tests__/edgeHooks.test.tsx`), `useEditMapChanges('kindash').mutate({op:'rename', slug:'dictation', title:'X'})` POSTs the right path/body and invalidates `['pm','map']` and `['pm','project','kindash']` on success; on a 409 `conflict` error the toast text is the R8c-5 sentence; on 502 the step is named; `useAbandonMapChanges` DELETEs; `useRebaseMapChanges` POSTs `/rebase`. Run → FAIL.
- [ ] **Step 2: Implement** per the Interfaces block; `mapChangesErrorMessage` reads `e instanceof ApiRequestError ? e.status/e.detail` (read `client.ts` for the field names). Run → PASS.
- [ ] **Step 3: Gate and commit** (`git add web/src/api/pm.ts web/src/api/hooks.ts web/src/api/__tests__/mapChangesHooks.test.tsx`; `feat(roadmap): 8c task 1 — map-changes fetchers and hooks`).

---

### Task 2: Capability edits — Edit, + Sub-capability, + Capability, links chip editor

**Files:**
- Create: `web/src/features/roadmap/slug.ts` (+ `slug.test.ts`); modify `detail/NewInitiativeDialog.tsx` to import from it (its tests unchanged)
- Create: `detail/EditCapabilityDialog.tsx` (+ test), `detail/NewCapabilityDialog.tsx` (+ test), `detail/CapabilityLinksEditor.tsx` (+ test)
- Modify: `detail/CapabilityDetail.tsx` (Overview: **Edit** and **+ Sub-capability** buttons beside `+ Initiative here`; the links `<ul>` replaced by `CapabilityLinksEditor`; the "Changes pending in PR #N" note when `product.mapChanges?.status === 'open' && capability.prNumber != null`), `detail/ProductDetail.tsx` (Overview: **+ Capability** button), their tests' hook mocks (`useEditMapChanges`), and `RoadmapPage.tsx` if the product page's `+` actions are built there (read `:150-165` — keep `+ Capability` on the ProductDetail Overview, not in the header, per R8c-3)

**Interfaces:**
- Consumes: Task 1's hooks; `flattenCapabilities` (`features/roadmap/lookup.ts`), `useRoadmapMap({ poll: false })` for other products' trees (links across products); `Dialog`, `Field`, `Input`, `Textarea`, `Select`, `Button`, `IconButton` + lucide `X`; `CapabilityNode` (`id`, `slug`, `title`, `summary`, `parentSlug`, `links: {kind, projectSlug, featureSlug}[]`, `children`).
- Produces: `EditCapabilityDialog({ product, capability, open, onClose })` — seeds title/summary/parent; Save sends the changed ops in order `rename` → `reparent` → `summary`, one `mutateAsync` each (stop at the first failure, keep the form open with the error); the parent select lists the product's flattened tree minus the capability itself and its descendants (a cycle the server would 422 anyway); `— root —` is a valid choice (`parentSlug: null`). `NewCapabilityDialog({ product, open, onClose, parentSlug? })` — title (required), slug (derived, editable, `slugify`), summary (optional `Textarea`), parent select (preset when given); Create sends `create`; on success closes and navigates to the new capability (`capabilityPath(product.slug, slug)`) — the row appears on the next map refetch (invalidated). `CapabilityLinksEditor({ product, capability })` — chips `${kind} · ${projectSlug}/${featureSlug}` each with `IconButton aria-label="Remove link: …"`; add row: kind `Select` (`powered_by` "powered by", `integrates_with` "integrates with"), product `Select` (every product on the map), capability `Select` (that product's flattened tree, excluding this capability itself); Add → `links` op with the full list; remove → `links` op with the list minus one; `disabled` while pending; an inline `<p role="alert">` with `mapChangesErrorMessage`.
- Every dialog's primary button is `size="lg"`; `Textarea` for summaries with `autoGrow`.

- [ ] **Step 1: `slug.ts`** — move `sanitize`/`slugify` (verbatim) with their comments out of `NewInitiativeDialog.tsx`; `slug.test.ts` pins `sanitize('Voice Notes ')` → `voice-notes-`, `slugify('Voice Notes ')` → `voice-notes`, `slugify('!!!')` → `''`. Run `NewInitiativeDialog.test.tsx` → unchanged, passing.
- [ ] **Step 2: Failing component tests** — `EditCapabilityDialog.test.tsx`: seeded from the capability; Save disabled until a change; a title change sends exactly `[{op:'rename', slug, title}]`; title + parent + summary changed → three `mutateAsync` calls in order with the right bodies; the parent select does not offer the capability or its descendants; a rejected first op keeps the dialog open with the error text and does not send the second. `NewCapabilityDialog.test.tsx`: slug follows the title until edited; Create sends `{op:'create', slug, title, parentSlug, summary?}` (summary omitted when blank); navigates on success. `CapabilityLinksEditor.test.tsx`: renders the chips; remove sends the list minus one; add sends the list plus one; the capability select excludes self; Add disabled until all three chosen. Mock `@/api/hooks` (`useEditMapChanges` → `{ mutate, mutateAsync, isPending, isError, error }`, `useRoadmapMap`). Run → FAIL.
- [ ] **Step 3: Implement the three components** per the Interfaces block. Run → PASS.
- [ ] **Step 4: Mount** in `CapabilityDetail` (Overview) and `ProductDetail` (Overview, a `+ {CAPABILITY.singular}` ghost button opening `NewCapabilityDialog` with no parent); add `useEditMapChanges` to both pages' hook mocks; one assertion per page that the buttons render and open their dialog. Run `(cd web && npx vitest run src/features/roadmap)` → PASS.
- [ ] **Step 5: Gate and commit** (`feat(roadmap): 8c task 2 — capability edits: edit, add, links chip editor`).

---

### Task 3: The map-changes PR card

**Files:**
- Create: `web/src/features/roadmap/detail/MapChangesCard.tsx` (+ test)
- Modify: `detail/ProductDetail.tsx` (Overview, above `<OnboardingWizard>`), its test's hook mocks

**Interfaces:**
- Consumes: `product.mapChanges: MapChangesPointer | null` (`branch, prNumber, prUrl, status, openedAt, headSha, behind, conflicting`); Task 1's `useAbandonMapChanges`, `useRebaseMapChanges`; `Banner` (`tone`, `title`, `actions`, `role`), `ConfirmDialog` (`open, onClose, onConfirm, title, description, confirmLabel, tone, typed, busy`), `relTime`/`isValidTimestamp` (`@/lib/relativeTime`), `prUrl` helper if `@/lib/githubUrl` has one (grep `export function prUrl`).
- Produces: `MapChangesCard({ product })` — renders nothing unless `mapChanges && mapChanges.status === 'open'`. Otherwise a `Banner`: `tone="danger"` when `conflicting` with title `Capability map changes conflict with the default branch` and body "Resolve the conflict on GitHub; the roadmap will not force-push." + the PR link; `tone="warning"` when `behind` with title `Capability map changes are behind` and actions **Rebase** (`useRebaseMapChanges`, `loading` while pending); else `tone="info"` with title `Capability map changes open in PR #N` (a link to `prUrl` when present, `target="_blank" rel="noreferrer"`) and "opened {relTime(openedAt)}" when `isValidTimestamp(openedAt)`; every variant carries **Abandon** (ghost, danger text) opening `ConfirmDialog` (`tone="danger"`, `typed={product.slug}`, description "Closes PR #N, deletes the branch and removes every capability that only exists there. Type the product slug to confirm.", `busy` while pending); the banner `role="status"` (not the danger default `alert` — it is persistent state, not an event; pass `role` explicitly per `Banner`'s docstring).

- [ ] **Step 1: Failing tests** (`MapChangesCard.test.tsx`): nothing when `mapChanges` is null or status `merged`; info variant with the link text `PR #12` and "opened … ago"; behind → Rebase calls `rebase.mutate()`; conflicting → danger copy and NO Rebase button; Abandon → confirm dialog requires the typed slug, then `abandon.mutate()`. Run → FAIL.
- [ ] **Step 2: Implement**; mount in `ProductDetail` Overview above the wizard; add the hooks to `ProductDetail.test.tsx`'s mock and one assertion that a product with an open pointer shows the card. Run → PASS.
- [ ] **Step 3: Gate and commit** (`feat(roadmap): 8c task 3 — the map-changes PR card: open PR, age, rebase, abandon, conflict banner`).

---

### Task 4: Rows, docs, Playwright

**Files:**
- Modify: `web/src/features/roadmap/rows/TreeRowCard.tsx` (+ its test or `RoadmapListColumn.test.tsx`)
- Modify: `docs/product/features/product-management-layer.md`
- Modify: `web/e2e/roadmap.spec.ts` (only if its fixture layer can carry `mapChanges`/`prNumber` — read how the spec seeds the map; if it drives the real edge with no seam, add nothing and say so in the report)

- [ ] **Step 1: Row badge** — a capability row whose `capability.prNumber != null` renders `<Badge tone="info">PR #{n}</Badge>` after the tier badge, with an `sr-only` "proposed in pull request N"; test in the list-column test with a fixture capability carrying `prNumber: 12`.
- [ ] **Step 2: Docs** — `product-management-layer.md`: the map-changes UI has landed (where the edits live, the PR card, the row badge); row 8 of `docs/superpowers/specs/2026-09-08-finish-line-persona-modes-design.md` status cell → `landed 2026-09-12 (Closes #1472; 8a #1588, 8b #1589, 8c)`.
- [ ] **Step 3: Playwright** — `(cd web && npm run build > /dev/null 2>&1 && npx playwright test e2e/roadmap.spec.ts 2>&1 | grep -E "passed|failed"); rm -rf web/dist` → passed.
- [ ] **Step 4: Gate and commit** (`feat(roadmap): 8c task 4 — PR badge on capability rows; docs; e2e`).

---

## Self-review

**Spec coverage:** §6 row "proposed + PR number until merge" — Task 4 badge (the tier already reads `proposed` for a `create`d capability's `state`); §8 add/rename/reparent/summary — Task 2 dialogs; §8 links chip editor — Task 2 `CapabilityLinksEditor`; §9 header shows the open PR, its age, Rebase when behind; Abandon — Task 3 card (on the product Overview, R8c-3); §9 conflict `Banner` with a link to the PR — Task 3; §14 page-contract rows unchanged (no new page), Playwright — Task 4. Not covered by design: list-column keyboard `a`/`e` (R8c-1).

**Placeholder scan:** Tasks specify components by interface and tests by named cases rather than full code blocks (the 8a/8b dialogs and hooks are the exact templates: `EditInitiativeDialog.tsx`, `ProductEdgesEditor.tsx`, `useDeleteEdge`); every name used is defined here or in the cited files.

**Type consistency:** `MapChangesPointer` (8b, `roadmap.ts`) is what `MapChangeResult.pointer`, `AbandonResult.pointer` and `product.mapChanges` carry; `MapChangeInput.links` matches `MapChangeBody.links` `[{kind, project, feature}]`; `CapabilityNode.links` uses `projectSlug`/`featureSlug` (map payload) — the editor converts to `{project, feature}` when sending.
