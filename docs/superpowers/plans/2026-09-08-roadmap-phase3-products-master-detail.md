# Roadmap Phase 3: Products master/detail at `/roadmap` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Products and Initiatives pages with one Roadmap section on the Sessions chassis: a list column with Map and Initiatives scopes over the Phase 2 tree endpoint, and a detail pane with portfolio, product, capability and initiative levels.

**Architecture:** The list column flattens `GET /api/pm/map` into rows with a pure reducer (`treeRows.ts`) and renders them on `CardFrame`; the Initiatives scope reuses `useItemOrdering` + `GroupedList`. Selection is the URL: `/roadmap/:productSlug`, `/roadmap/:productSlug/capabilities/:capSlug`, `/initiatives/:initiativeSlug`. The shell learns a second list section the same way it learned Sessions (`LIST_SECTIONS`, `OPEN_ITEM_RESOLVERS`, a workspace hook). Tiers are computed server-side; the web maps names to tones.

**Tech Stack:** React 19 + react-router + TanStack Query + Tailwind tokens (`web/`), FastAPI + SQLAlchemy async (`server/`), vitest + Testing Library, pytest.

**Spec:** `docs/superpowers/specs/2026-09-08-finish-line-persona-modes-design.md` (§4.1 nav, §4.2 routes, §4.3 list scopes, §4.5 advance tier) and `docs/superpowers/specs/2026-09-07-roadmap-drilldown-design.md` §5–§7, §11, §13 phase 3. Read both first. Phase 1 (`2026-09-07-roadmap-phase1-shell-and-dead-ends.md`, #1439) and Phase 2 (`2026-09-07-roadmap-phase2-tree-backend.md`, #1447) are on master.

## Global Constraints

- Nav ids after this plan: `sessions`, `pipeline`, `automations`, `roadmap`, `triage`, `insights`, `settings`. The `initiatives` and `projects` rows are deleted; `/projects*` and `/initiatives` redirect (finish-line spec §4.2). Triage keeps its section until finish-line row 3.
- User-facing words come from `web/src/features/products/vocab.ts` only: Product, Capability, Initiative, Epic, Issue; nav label `Roadmap`. Code identifiers keep `project`/`feature`.
- Tier vocabulary is `needs-you > blocked > in-flight > proposed > live > idle`, computed once in `server/pm/tree.py`; `features/roadmap/roadmapTier.ts` only maps names to tones (constitution VI).
- New web files live under `web/src/features/roadmap/`, `web/src/ui/`, `web/src/shell/`, `web/src/api/`; all are under the `ccswitch/no-raw-palette` ESLint rule: token classes only (`bg-surface`, `text-ink-muted`, `border-edge`, `min-h-target`…), no `dark:`, no hex, no `z-<n>`, no arbitrary px. No new `.css` files.
- Every deleted component's test file is deleted in the same task. `projects.css`, `initiatives.css`, `ProjectsTab.tsx`, `InitiativesTab.tsx` are gone at the end.
- Every new routed page adds a row to `web/src/features/settings/pageContract.test.tsx` and any new `api.get` path to that test's mock.
- Server: nullable columns only (none needed here), `db_utc_now()` never `func.now()`, four-step gate before push: `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/`, `pytest server/tests/`.
- Web gate on every task that touches `web/`: `(cd web && npm run typecheck && npm run lint && npm run test && npm run build)`. Run the FULL vitest suite: ~47 files mock `@/api/hooks` and a shell change breaks tests in files you did not touch.
- Bash hook: use `(cd web && …)` subshells, never a persisted `cd`; commit messages via `git commit -F <file>`; never pair deploy/release/rollout/promote with prod/production in a command's text.
- Commit trailer: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Branch: `git fetch && git checkout -b roadmap-phase3 origin/master`. One PR for the phase; mark ready and `gh pr merge --squash --auto` when the gates are green.

## File map

| File | Responsibility |
|------|----------------|
| `server/pm/tree.py`, `server/tests/test_pm_tree.py` | Advance-ready initiatives roll up to `needs-you` with `reason: "advance"`. |
| `web/src/api/roadmap.ts` | Types + fetchers for `/pm/map`, `/pm/leaves`, `/pm/queue`, `/pm/projects/{slug}`. |
| `web/src/api/sdlc.ts` | `Initiative` gains `projectSlug`, `primaryCapability`, `capabilities`; create input gains capability ids. |
| `web/src/api/hooks.ts` | `useRoadmapMap`, `useLeaves`, `useQueue`, `useInitiative`, `useProject`, QK entries. |
| `web/src/features/products/vocab.ts` | `TIER_LABEL`, `ROADMAP` nav words, `INBOX` reserved. |
| `web/src/features/roadmap/roadmapTier.ts` | Tier → tone / rail / dot / rank. |
| `web/src/ui/Breadcrumb.tsx` | The breadcrumb primitive (first consumer: detail headers). |
| `web/src/features/roadmap/treeRows.ts` | Pure: map → flat rows; expand/collapse; search. |
| `web/src/features/roadmap/roadmapNavState.ts` | Paths, route patterns, scope-from-search, builders. |
| `web/src/features/roadmap/rows/*.tsx` | `ProductRow`, `CapabilityRow`, `InitiativeRow`, `PseudoRow` on `CardFrame`. |
| `web/src/features/roadmap/RoadmapScopeSegment.tsx` | Map · Initiatives links. |
| `web/src/features/roadmap/RoadmapListColumn.tsx` | The `app.list` column for the section. |
| `web/src/features/roadmap/useRoadmapWorkspace.ts` | The section's workspace hook (list props + navigation). |
| `web/src/shell/navDefs.ts`, `listColumns.tsx`, `openItems.ts`, `routes.tsx`, `AppShell.tsx` | Wiring. |
| `web/src/features/roadmap/RoadmapPage.tsx` | Routed page: picks portfolio / product / capability by params. |
| `web/src/features/roadmap/detail/PortfolioDetail.tsx`, `ProductDetail.tsx`, `CapabilityDetail.tsx` | The three detail levels. |
| `web/src/features/roadmap/InitiativePage.tsx` | `/initiatives/:initiativeSlug`. |
| `web/src/features/roadmap/detail/RankedList.tsx`, `NeedsYouList.tsx`, `InitiativeList.tsx`, `StatTiles.tsx` | Shared detail widgets. |
| `web/src/initiatives/IncidentQueue.tsx` | Gains `repos?: readonly string[]`. |
| deleted | `web/src/projects/ProjectsTab.tsx` (+ test), `projects.css`, `web/src/initiatives/InitiativesTab.tsx` (+ test), `initiatives.css`. |

---

### Task 1: Advance-ready initiatives are `needs-you` (server, finish-line spec §4.5)

**Files:**
- Modify: `server/pm/tree.py`
- Test: `server/tests/test_pm_tree.py`

**Interfaces:**
- Produces: `initiative_tier(status, stage, epic_blocked, epic_needs_human, *, epic_total=0, epic_done=0) -> str`; `initiative_reason(status, stage, epic_total, epic_done) -> str | None`; every initiative node in the map payload carries `"reason": "advance" | None`.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_pm_tree.py`:

```python
def test_advance_ready_initiative_is_needs_you_with_reason():
    from server.pm.tree import initiative_reason
    # build stage, every epic child closed → the human has to press Advance
    assert initiative_tier("active", "build", 0, 0, epic_total=3, epic_done=3) == "needs-you"
    assert initiative_reason("active", "build", 3, 3) == "advance"
    # not ready: still open children, or no epic at all
    assert initiative_tier("active", "build", 0, 0, epic_total=3, epic_done=2) == "in-flight"
    assert initiative_reason("active", "build", 3, 2) is None
    assert initiative_tier("active", "build", 0, 0, epic_total=0, epic_done=0) == "in-flight"
    assert initiative_reason("active", "build", 0, 0) is None
    # other stages never say advance, even with a finished epic
    assert initiative_reason("active", "deploy", 3, 3) is None
    assert initiative_reason("done", "done", 3, 3) is None


def test_map_node_carries_reason():
    ini = InitiativeIn(slug="s", title="S", stage="build", status="active",
                       primary_slug="cap", capability_slugs=("cap",),
                       epic={"ref": "o/r#1", "url": "u", "total": 2, "done": 2,
                             "blocked": 0, "needsHuman": 0})
    cap = CapabilityIn(id="f1", slug="cap", title="Cap", state="live", summary=None,
                       doc_path=None, commit_sha=None, pr_number=None, parent_slug=None,
                       issue_tiers=())
    tree = build_product_tree(ProductIn(slug="p", name="P", capabilities=(cap,),
                                        initiatives=(ini,), awaiting_triage=0, needs_home=0))
    node = tree["initiatives"][0]
    assert node["tier"] == "needs-you"
    assert node["reason"] == "advance"
    assert tree["capabilities"][0]["tier"] == "needs-you"
    assert tree["counts"]["needsYou"] == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest server/tests/test_pm_tree.py -q -k "advance or carries_reason"`
Expected: FAIL — `ImportError: cannot import name 'initiative_reason'` and `TypeError: unexpected keyword argument 'epic_total'`.

- [ ] **Step 3: Implement**

In `server/pm/tree.py` replace `initiative_tier` and `_initiative_node`:

```python
def initiative_reason(status: str | None, stage: str | None,
                      epic_total: int, epic_done: int) -> str | None:
    """Why an initiative needs a human beyond its status: today only "advance" —
    the build epic is fully closed but nobody has moved the gate (finish-line
    spec §4.5). No auto-advance: SDLC governance keeps the human as the gate."""
    if status == "active" and stage == "build" and epic_total > 0 and epic_done >= epic_total:
        return "advance"
    return None


def initiative_tier(status: str | None, stage: str | None,
                    epic_blocked: int, epic_needs_human: int, *,
                    epic_total: int = 0, epic_done: int = 0) -> str:
    if status == "awaiting_human" or epic_needs_human > 0:
        return "needs-you"
    if initiative_reason(status, stage, epic_total, epic_done) is not None:
        return "needs-you"
    if status == "blocked" or epic_blocked > 0:
        return "blocked"
    if status == "done" or stage == "done":
        return "live"
    if status == "proposed":
        return "proposed"
    if status == "active" and stage in _IN_FLIGHT_STAGES:
        return "in-flight"
    return "idle"
```

and in `_initiative_node`:

```python
    epic = ini.epic or {}
    total, done = int(epic.get("total", 0)), int(epic.get("done", 0))
    return {
        "slug": ini.slug, "title": ini.title, "stage": ini.stage, "status": ini.status,
        "projectSlug": project_slug, "primaryCapability": ini.primary_slug,
        "capabilities": list(ini.capability_slugs),
        "tier": initiative_tier(ini.status, ini.stage,
                                int(epic.get("blocked", 0)), int(epic.get("needsHuman", 0)),
                                epic_total=total, epic_done=done),
        "reason": initiative_reason(ini.status, ini.stage, total, done),
        "epic": ini.epic,
    }
```

- [ ] **Step 4: Run the whole tree test file and the map API test**

Run: `.venv/bin/python -m pytest server/tests/test_pm_tree.py server/tests/test_pm_map_api.py -q`
Expected: all PASS. If `test_pm_map_api.py` asserts exact node keys, add `"reason": None` to its expected dicts.

- [ ] **Step 5: Gate and commit**

Run: `ruff check src/ server/ && mypy server/ --ignore-missing-imports`
Expected: clean.

```bash
printf 'feat(pm): advance-ready initiatives roll up to needs-you with reason "advance"\n\nFinish-line spec §4.5: an initiative whose build epic is fully closed but\nwhich nobody has advanced was reading as calm "Active". It is now a\nneeds-you row with reason "advance"; no auto-advance.\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c1.txt
git add server/pm/tree.py server/tests/test_pm_tree.py server/tests/test_pm_map_api.py && git commit -F /tmp/c1.txt
```

---

### Task 2: Web API layer for the roadmap

**Files:**
- Create: `web/src/api/roadmap.ts`
- Modify: `web/src/api/sdlc.ts` (`Initiative`, `CreateInitiativeInput`), `web/src/api/hooks.ts` (QK + hooks)
- Test: `web/src/api/__tests__/roadmap.test.ts`

**Interfaces:**
- Produces (from `@/api/roadmap`): `ROADMAP_TIERS`, `RoadmapTier`, `CapabilityNode`, `InitiativeNode`, `ProductNode`, `RoadmapMap`, `Leaf`, `LeavesScope`, `QueueEntry`, `QueueView`, `fetchRoadmapMap()`, `fetchLeaves(scope)`, `fetchQueue()`, `fetchProject(slug)`.
- Produces (from `@/api/hooks`): `QK.roadmapMap`, `QK.leaves(scope)`, `QK.queue`, `QK.project(slug)`; `useRoadmapMap()`, `useLeaves(scope | null)`, `useQueue()`, `useInitiative(slug | null)`, `useProject(slug | null)`.
- Produces (from `@/api/sdlc`): `Initiative.projectSlug: string | null`, `Initiative.primaryCapability: InitiativeCapabilityRef | null`, `Initiative.capabilities: InitiativeCapabilityRef[]`, `Initiative.buildSessions?: { id: string; status: string | null; branch: string | null }[]`, `Initiative.nextAction?: InitiativeNextAction`; `CreateInitiativeInput.primaryCapabilityId?: string`, `CreateInitiativeInput.capabilityIds?: string[]`.

- [ ] **Step 1: Write the failing test**

`web/src/api/__tests__/roadmap.test.ts`:

```ts
import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('@/api/client', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), del: vi.fn() },
}))

import { api } from '@/api/client'
import { fetchRoadmapMap, fetchLeaves, fetchQueue, fetchProject, ROADMAP_TIERS } from '@/api/roadmap'

const get = vi.mocked(api.get)

describe('roadmap fetchers', () => {
  beforeEach(() => get.mockReset())

  it('pins the tier vocabulary to the server order', () => {
    expect(ROADMAP_TIERS).toEqual(['needs-you', 'blocked', 'in-flight', 'proposed', 'live', 'idle'])
  })

  it('reads the map', async () => {
    get.mockResolvedValueOnce({ products: [], generatedAt: 'now' })
    await expect(fetchRoadmapMap()).resolves.toEqual({ products: [], generatedAt: 'now' })
    expect(get).toHaveBeenCalledWith('/pm/map')
  })

  it('reads leaves by exactly one scope and unwraps items', async () => {
    get.mockResolvedValueOnce({ items: [{ id: 'w1' }] })
    await expect(fetchLeaves({ capability: 'f 1' })).resolves.toEqual([{ id: 'w1' }])
    expect(get).toHaveBeenCalledWith('/pm/leaves?capability=f%201')
    get.mockResolvedValueOnce({ items: [] })
    await fetchLeaves({ initiative: 'slug' })
    expect(get).toHaveBeenLastCalledWith('/pm/leaves?initiative=slug')
  })

  it('reads the queue and one project', async () => {
    get.mockResolvedValueOnce({ entries: [], skipped: 0, generatedAt: 'now' })
    await fetchQueue()
    expect(get).toHaveBeenCalledWith('/pm/queue')
    get.mockResolvedValueOnce({ id: 'p', slug: 'a/b' })
    await fetchProject('a/b')
    expect(get).toHaveBeenLastCalledWith('/pm/projects/a%2Fb')
  })
})
```

- [ ] **Step 2: Run it to verify it fails**

Run: `(cd web && npx vitest run src/api/__tests__/roadmap.test.ts)`
Expected: FAIL — cannot resolve `@/api/roadmap`.

- [ ] **Step 3: Create `web/src/api/roadmap.ts`**

```ts
/**
 * Roadmap tree API client (spec 2026-09-07 §10): the map every product renders
 * from, the issue leaves under one node, the ranked queue, and one project.
 * Plain fetchers here, React Query hooks in `api/hooks.ts`. Field names are the
 * server's camelCase JSON verbatim (`server/pm/tree.py`, `server/api/pm_tree.py`).
 */
import { api } from './client'
import type { InitiativeEpic } from './sdlc'
import type { Project } from './pm'

/** Attention tiers, highest first — mirrors `server/pm/tree.py:TIERS`. */
export const ROADMAP_TIERS = ['needs-you', 'blocked', 'in-flight', 'proposed', 'live', 'idle'] as const
export type RoadmapTier = (typeof ROADMAP_TIERS)[number]

export interface CapabilityCounts {
  openIssues: number
  inFlight: number
  blocked: number
  needsYou: number
}

export interface CapabilityLink {
  kind: 'powered_by' | 'integrates_with' | string
  projectSlug: string
  featureSlug: string
}

export interface CapabilityNode {
  id: string
  slug: string
  title: string | null
  state: string | null
  summary: string | null
  docPath: string | null
  commitSha: string | null
  prNumber: number | null
  parentSlug: string | null
  tier: RoadmapTier
  counts: CapabilityCounts
  links: CapabilityLink[]
  /** Slugs of initiatives that TOUCH this capability (not primary). */
  initiatives: string[]
  children: CapabilityNode[]
}

export interface InitiativeNode {
  slug: string
  title: string | null
  stage: string | null
  status: string | null
  projectSlug: string
  /** Primary capability SLUG (the detail endpoint returns an object instead). */
  primaryCapability: string | null
  capabilities: string[]
  tier: RoadmapTier
  /** Why it needs a human beyond its status; today only `'advance'`. */
  reason: 'advance' | string | null
  epic: InitiativeEpic | null
}

export interface ProductCounts {
  inFlight: number
  blocked: number
  needsYou: number
  capabilities: number
  awaitingTriage: number
  needsHome: number
}

export interface ProductNode {
  slug: string
  name: string | null
  tier: RoadmapTier
  counts: ProductCounts
  capabilities: CapabilityNode[]
  /** Every initiative of the product, flat; nesting is the web's job. */
  initiatives: InitiativeNode[]
}

export interface RoadmapMap {
  products: ProductNode[]
  generatedAt: string
}

export interface Leaf {
  id: string
  ref: string
  repo: string
  number: number
  kind: 'issue' | 'pr' | string
  title: string | null
  url: string | null
  state: string | null
  triageState: string | null
  score: number | null
  reach: number | null
  impact: number | null
  effort: number | null
  confidence: number | null
  tier: RoadmapTier
  blockedBy: string[]
  parentRef: string | null
  sessionStatus: string | null
  ghCreatedAt: string | null
}

/** Exactly one of the two, enforced by the server (422 `bad_scope`). */
export type LeavesScope = { capability: string; initiative?: never } | { initiative: string; capability?: never }

export interface QueueEntry {
  ref: string
  repo: string
  number: number
  product: string | null
  title: string | null
  url: string | null
  score: number | null
}

export interface QueueView {
  entries: QueueEntry[]
  skipped: unknown
  generatedAt: string
}

export function fetchRoadmapMap(): Promise<RoadmapMap> {
  return api.get<RoadmapMap>('/pm/map')
}

export async function fetchLeaves(scope: LeavesScope): Promise<Leaf[]> {
  const q = scope.capability !== undefined
    ? `capability=${encodeURIComponent(scope.capability)}`
    : `initiative=${encodeURIComponent(scope.initiative)}`
  const res = await api.get<{ items: Leaf[] }>(`/pm/leaves?${q}`)
  return res.items
}

export function fetchQueue(): Promise<QueueView> {
  return api.get<QueueView>('/pm/queue')
}

export function fetchProject(slug: string): Promise<Project> {
  return api.get<Project>(`/pm/projects/${encodeURIComponent(slug)}`)
}
```

- [ ] **Step 4: Widen the initiative types in `web/src/api/sdlc.ts`**

Add after `InitiativeEpic`:

```ts
/** A capability an initiative is linked to (`initiative_features`). */
export interface InitiativeCapabilityRef {
  id: string
  slug: string
  title: string | null
  isPrimary?: boolean
}

/** What the stage engine says should happen next (detail endpoint only). */
export interface InitiativeNextAction {
  type: 'propose' | 'await_approval' | 'handoff_to_build' | 'done' | 'needs_human' | 'unknown' | string
  kind?: string
  issue?: string
  reason?: string
}
```

Add to `Initiative` (after `epic`):

```ts
  /** Phase 2 fields — every initiative endpoint emits them. */
  projectSlug: string | null
  primaryCapability: InitiativeCapabilityRef | null
  capabilities: InitiativeCapabilityRef[]
  /** Detail endpoint only. */
  buildSessions?: { id: string; status: string | null; branch: string | null }[]
  nextAction?: InitiativeNextAction
```

Add to `CreateInitiativeInput`:

```ts
  primaryCapabilityId?: string
  capabilityIds?: string[]
```

Run `(cd web && npm run typecheck)`; fix any test factory that builds an `Initiative` literal by adding `projectSlug: null, primaryCapability: null, capabilities: []` (search `web/src/test/factories.ts` and `web/src/initiatives/__tests__/`).

- [ ] **Step 5: Add QK entries and hooks in `web/src/api/hooks.ts`**

In `QK`, after `onboarding`:

```ts
  roadmapMap: ['pm', 'map'] as const,
  leaves: (scope: string) => ['pm', 'leaves', scope] as const,
  queue: ['pm', 'queue'] as const,
  project: (slug: string) => ['pm', 'project', slug] as const,
```

Imports at the top of `hooks.ts`:

```ts
import {
  fetchRoadmapMap, fetchLeaves, fetchQueue, fetchProject,
  type RoadmapMap, type Leaf, type LeavesScope, type QueueView,
} from './roadmap'
import { fetchInitiative } from './sdlc'   // add to the existing sdlc import line
import type { Project } from './pm'        // already imported; keep one import
```

Hooks, placed after `useOnboarding`:

```ts
// ── Roadmap (spec 2026-09-07 §10) ────────────────────────────────────────────

/** The whole product → capability → initiative map with server-computed tiers. */
export function useRoadmapMap() {
  return useQuery<RoadmapMap>({
    queryKey: QK.roadmapMap,
    queryFn: fetchRoadmapMap,
    staleTime: 5_000,
    refetchInterval: 15_000,
  })
}

/** Issues under one capability or initiative; `null` scope = not mounted yet. */
export function useLeaves(scope: LeavesScope | null) {
  const key = scope === null ? 'none'
    : scope.capability !== undefined ? `capability:${scope.capability}` : `initiative:${scope.initiative}`
  return useQuery<Leaf[]>({
    queryKey: QK.leaves(key),
    queryFn: () => fetchLeaves(scope as LeavesScope),
    enabled: scope !== null,
    staleTime: 5_000,
    refetchInterval: 30_000,
  })
}

/** The ranked cross-product queue — the same query the runner will read (spec 003 C1). */
export function useQueue() {
  return useQuery<QueueView>({
    queryKey: QK.queue,
    queryFn: fetchQueue,
    staleTime: 5_000,
    refetchInterval: 30_000,
  })
}

/** One initiative with children, sessions and next action. */
export function useInitiative(slug: string | null) {
  return useQuery<Initiative>({
    queryKey: QK.initiative(slug ?? ''),
    queryFn: () => fetchInitiative(slug as string),
    enabled: slug !== null && slug !== '',
    staleTime: 5_000,
    refetchInterval: 15_000,
  })
}

/** One project: repos and edges (the list payload's counts are not here). */
export function useProject(slug: string | null) {
  return useQuery<Project>({
    queryKey: QK.project(slug ?? ''),
    queryFn: () => fetchProject(slug as string),
    enabled: slug !== null && slug !== '',
    staleTime: 5_000,
    refetchInterval: 15_000,
  })
}
```

Also make `useAdvanceInitiative` and `useCreateInitiative` invalidate `QK.roadmapMap` in their `onSuccess` (the map's tiers move when a gate does).

- [ ] **Step 6: Run the test and the web gate**

Run: `(cd web && npx vitest run src/api/__tests__/roadmap.test.ts && npm run typecheck && npm run lint)`
Expected: PASS, clean.

- [ ] **Step 7: Commit**

```bash
printf 'feat(web): roadmap API client — map, leaves, queue, project fetchers and hooks\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c2.txt
git add web/src/api && git commit -F /tmp/c2.txt
```

---

### Task 3: Tier presentation, vocabulary, and the Breadcrumb primitive

**Files:**
- Create: `web/src/features/roadmap/roadmapTier.ts`, `web/src/features/roadmap/roadmapTier.test.ts`
- Create: `web/src/ui/Breadcrumb.tsx`, `web/src/ui/Breadcrumb.test.tsx`
- Modify: `web/src/features/products/vocab.ts`, `web/src/features/products/vocab.test.ts`

**Interfaces:**
- Produces: `TIER_TONE: Record<RoadmapTier, Tone>`, `TIER_ACCENT: Record<RoadmapTier, { rail: string; dot: string; pulse: boolean }>`, `TIER_RANK`, `tierLabel(tier)`, `maxTier(...tiers)`; vocab `TIER_LABEL`, `ROADMAP = { label: 'Roadmap', map: 'Map', initiatives: 'Initiatives', queue: 'Queue', inbox: 'Inbox' }`, `REASON_LABEL = { advance: 'Ready to advance' }`, `reasonLabel(reason)`; `Breadcrumb({ items: BreadcrumbItem[]; className? })` where `BreadcrumbItem = { label: string; to?: string }`.

- [ ] **Step 1: Write the failing tests**

`web/src/features/roadmap/roadmapTier.test.ts`:

```ts
// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { ROADMAP_TIERS } from '@/api/roadmap'
import { TIER_TONE, TIER_ACCENT, TIER_RANK, tierLabel, maxTier } from './roadmapTier'

describe('roadmapTier', () => {
  it('covers every server tier exactly once', () => {
    for (const t of ROADMAP_TIERS) {
      expect(TIER_TONE[t]).toBeDefined()
      expect(TIER_ACCENT[t].rail).toMatch(/^bg-/)
      expect(TIER_RANK[t]).toBe(ROADMAP_TIERS.indexOf(t))
      expect(tierLabel(t).length).toBeGreaterThan(2)
    }
  })
  it('only needs-you pulses', () => {
    expect(ROADMAP_TIERS.filter((t) => TIER_ACCENT[t].pulse)).toEqual(['needs-you'])
  })
  it('maxTier picks the most urgent and treats unknown as idle', () => {
    expect(maxTier('idle', 'live', 'blocked', 'proposed')).toBe('blocked')
    expect(maxTier()).toBe('idle')
    expect(maxTier('bogus' as never, 'live')).toBe('live')
  })
})
```

Append to `web/src/features/products/vocab.test.ts`:

```ts
import { ROADMAP, TIER_LABEL, reasonLabel } from './vocab'

it('names the roadmap surfaces and every tier', () => {
  expect(ROADMAP.label).toBe('Roadmap')
  expect(Object.keys(TIER_LABEL)).toEqual(['needs-you', 'blocked', 'in-flight', 'proposed', 'live', 'idle'])
  expect(reasonLabel('advance')).toBe('Ready to advance')
  expect(reasonLabel(null)).toBe('')
})
```

`web/src/ui/Breadcrumb.test.tsx`:

```tsx
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { Breadcrumb } from './Breadcrumb'

describe('Breadcrumb', () => {
  it('links every item but the last and marks the last as current', () => {
    render(
      <MemoryRouter>
        <Breadcrumb items={[{ label: 'Roadmap', to: '/roadmap' }, { label: 'KinDash', to: '/roadmap/kindash' }, { label: 'Tasks' }]} />
      </MemoryRouter>,
    )
    const nav = screen.getByRole('navigation', { name: 'Breadcrumb' })
    expect(nav).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Roadmap' })).toHaveAttribute('href', '/roadmap')
    expect(screen.getByRole('link', { name: 'KinDash' })).toHaveAttribute('href', '/roadmap/kindash')
    expect(screen.queryByRole('link', { name: 'Tasks' })).toBeNull()
    expect(screen.getByText('Tasks')).toHaveAttribute('aria-current', 'page')
  })
})
```

- [ ] **Step 2: Run them to verify they fail**

Run: `(cd web && npx vitest run src/features/roadmap src/ui/Breadcrumb.test.tsx src/features/products)`
Expected: FAIL — modules not found / exports missing.

- [ ] **Step 3: Extend `vocab.ts`**

Append:

```ts
import type { RoadmapTier } from '@/api/roadmap'

/** The Roadmap section and its list scopes (finish-line spec §4.1, §4.3). */
export const ROADMAP = {
  label: 'Roadmap',
  map: 'Map',
  initiatives: 'Initiatives',
  queue: 'Queue',
  inbox: 'Inbox',
  help: 'The product roadmap: every product, its capabilities, the initiatives changing them and the issues underneath. Drill down, trace back up, and see what needs you.',
} as const

export const INITIATIVE = { singular: 'Initiative', plural: 'Initiatives', new: 'New initiative' } as const
export const ISSUE = { singular: 'Issue', plural: 'Issues' } as const
export const EPIC = { singular: 'Epic' } as const

/** One attention tier per row (spec §11); the words the pill and Needs-you tab use. */
export const TIER_LABEL: Record<RoadmapTier, string> = {
  'needs-you': 'Needs you',
  blocked: 'Blocked',
  'in-flight': 'In flight',
  proposed: 'Proposed',
  live: 'Live',
  idle: 'Idle',
}

/** Why a row needs a human beyond its tier (server `reason`). */
export const REASON_LABEL: Record<string, string> = {
  advance: 'Ready to advance',
}

export const reasonLabel = (reason: string | null | undefined): string =>
  reason ? (REASON_LABEL[reason] ?? reason) : ''
```

(Keep the existing `import type` lines at the top; move the new `import type { RoadmapTier }` up beside them so imports stay first.)

- [ ] **Step 4: Create `roadmapTier.ts`**

```ts
/**
 * Tier → tone. The TIER itself is computed server-side, once, in
 * `server/pm/tree.py`; this module is the web's only knowledge of what a tier
 * looks like, shaped like `session/statusTier.ts` so rows and pills read alike.
 */
import { ROADMAP_TIERS, type RoadmapTier } from '@/api/roadmap'
import type { Tone } from '@/ui/Badge'
import { TIER_LABEL } from '@/features/products/vocab'

export const TIER_TONE: Record<RoadmapTier, Tone> = {
  'needs-you': 'warning',
  blocked: 'danger',
  'in-flight': 'success',
  proposed: 'info',
  live: 'neutral',
  idle: 'neutral',
}

/** Rail + dot classes, muted where the tier is calm (same reading as sessions). */
export const TIER_ACCENT: Record<RoadmapTier, { rail: string; dot: string; pulse: boolean }> = {
  'needs-you': { rail: 'bg-warning', dot: 'bg-warning', pulse: true },
  blocked: { rail: 'bg-danger', dot: 'bg-danger', pulse: false },
  'in-flight': { rail: 'bg-success', dot: 'bg-success', pulse: false },
  proposed: { rail: 'bg-info/40', dot: 'bg-info', pulse: false },
  live: { rail: 'bg-neutral/40', dot: 'bg-neutral', pulse: false },
  idle: { rail: 'bg-neutral/20', dot: 'bg-neutral/60', pulse: false },
}

export const TIER_RANK: Record<RoadmapTier, number> = Object.fromEntries(
  ROADMAP_TIERS.map((t, i) => [t, i]),
) as Record<RoadmapTier, number>

export const tierLabel = (tier: RoadmapTier): string => TIER_LABEL[tier]

/** The most urgent of `tiers`; unknown strings count as idle (mirrors `max_tier`). */
export function maxTier(...tiers: RoadmapTier[]): RoadmapTier {
  let best: RoadmapTier = 'idle'
  for (const t of tiers) {
    const r = TIER_RANK[t]
    if (r !== undefined && r < TIER_RANK[best]) best = t
  }
  return best
}
```

- [ ] **Step 5: Create `web/src/ui/Breadcrumb.tsx`**

```tsx
import { Link } from 'react-router-dom'
import { ChevronRight } from 'lucide-react'
import { cn } from '@/lib/utils'

export interface BreadcrumbItem {
  label: string
  /** Absent on the last item, which is the current page. */
  to?: string
}

/**
 * The graph path of the open node (roadmap spec §4 "Breadcrumb"). Every item
 * but the last is a link; the last carries `aria-current="page"`. Rendered in
 * `PageHeader.meta`, above the title, so the title stays the one `h1`.
 */
export function Breadcrumb({ items, className }: { items: BreadcrumbItem[]; className?: string }) {
  return (
    <nav aria-label="Breadcrumb" className={cn('min-w-0', className)}>
      <ol className="flex min-w-0 flex-wrap items-center gap-1 text-xs text-ink-muted">
        {items.map((item, i) => {
          const last = i === items.length - 1
          return (
            <li key={`${item.label}-${i}`} className="flex min-w-0 items-center gap-1">
              {i > 0 && <ChevronRight size={12} aria-hidden="true" className="shrink-0" />}
              {last || !item.to ? (
                <span aria-current={last ? 'page' : undefined} className={cn('truncate', last && 'text-ink')}>
                  {item.label}
                </span>
              ) : (
                <Link to={item.to} className="truncate rounded-sm hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent">
                  {item.label}
                </Link>
              )}
            </li>
          )
        })}
      </ol>
    </nav>
  )
}
```

- [ ] **Step 6: Run the tests, then lint**

Run: `(cd web && npx vitest run src/features/roadmap src/ui/Breadcrumb.test.tsx src/features/products && npm run lint)`
Expected: PASS, clean.

- [ ] **Step 7: Commit**

```bash
printf 'feat(roadmap): tier presentation, roadmap vocabulary, Breadcrumb primitive\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c3.txt
git add web/src/features/roadmap web/src/features/products web/src/ui/Breadcrumb.tsx web/src/ui/Breadcrumb.test.tsx && git commit -F /tmp/c3.txt
```

---

### Task 4: `treeRows.ts` — the pure Map-scope model

**Files:**
- Create: `web/src/features/roadmap/treeRows.ts`, `web/src/features/roadmap/treeRows.test.ts`
- Create: `web/src/features/roadmap/__fixtures__/map.ts`

**Interfaces:**
- Produces:
  ```ts
  export type TreeRow =
    | { kind: 'product'; key: string; depth: 0; product: ProductNode; expanded: boolean; hasChildren: boolean }
    | { kind: 'capability'; key: string; depth: number; product: ProductNode; capability: CapabilityNode; expanded: boolean; hasChildren: boolean }
    | { kind: 'initiative'; key: string; depth: number; product: ProductNode; initiative: InitiativeNode }
    | { kind: 'pseudo'; key: string; depth: 1; product: ProductNode; pseudo: 'awaiting-triage' | 'needs-home'; count: number }
  export interface TreeState { collapsed: ReadonlySet<string> }   // keys of COLLAPSED nodes; default is expanded
  export function productKey(slug): string; capabilityKey(productSlug, capSlug): string; initiativeKey(slug): string
  export function flattenMap(map: RoadmapMap, state: TreeState, query: string): TreeRow[]
  export function toggleKey(state: TreeState, key: string): TreeState
  export function setCollapsed(state: TreeState, key: string, collapsed: boolean): TreeState
  export function rowMatches(row: TreeRow, query: string): boolean
  export function keyForOpenItem(item: RoadmapOpenItem): string   // see Task 6 for RoadmapOpenItem
  ```
- Rules: products are always rows; a product's rows are its capability tree (depth 1+), initiatives under their primary capability (depth = capability depth + 1), initiatives whose primary is unknown directly under the product (depth 1), then the two pseudo rows when their count > 0. A collapsed node hides its subtree. A non-empty `query` keeps only rows that match by title/slug and every ancestor of a match, and ignores collapse.

- [ ] **Step 1: Write the fixture and the failing tests**

`web/src/features/roadmap/__fixtures__/map.ts`:

```ts
import type { CapabilityNode, InitiativeNode, ProductNode, RoadmapMap } from '@/api/roadmap'

export function cap(over: Partial<CapabilityNode> & { slug: string }): CapabilityNode {
  return {
    id: `f-${over.slug}`, title: over.slug, state: 'live', summary: null, docPath: null,
    commitSha: null, prNumber: null, parentSlug: null, tier: 'idle',
    counts: { openIssues: 0, inFlight: 0, blocked: 0, needsYou: 0 },
    links: [], initiatives: [], children: [], ...over,
  }
}

export function ini(over: Partial<InitiativeNode> & { slug: string; projectSlug: string }): InitiativeNode {
  return {
    title: over.slug, stage: 'build', status: 'active', primaryCapability: null,
    capabilities: [], tier: 'in-flight', reason: null, epic: null, ...over,
  }
}

export function product(over: Partial<ProductNode> & { slug: string }): ProductNode {
  return {
    name: over.slug, tier: 'idle',
    counts: { inFlight: 0, blocked: 0, needsYou: 0, capabilities: 0, awaitingTriage: 0, needsHome: 0 },
    capabilities: [], initiatives: [], ...over,
  }
}

/** kindash: tasks ▸ (task-views) ; billing.  Initiative "views" primary=task-views. */
export const MAP: RoadmapMap = {
  generatedAt: '2026-09-08T00:00:00Z',
  products: [
    product({
      slug: 'kindash', name: 'KinDash', tier: 'needs-you',
      counts: { inFlight: 1, blocked: 0, needsYou: 1, capabilities: 3, awaitingTriage: 2, needsHome: 0 },
      capabilities: [
        cap({ slug: 'billing', title: 'Billing' }),
        cap({ slug: 'tasks', title: 'Tasks', tier: 'in-flight', children: [
          cap({ slug: 'task-views', title: 'Task views', parentSlug: 'tasks', tier: 'in-flight', initiatives: ['views'] }),
        ] }),
      ],
      initiatives: [
        ini({ slug: 'views', title: 'Use-case-first task views', projectSlug: 'kindash', primaryCapability: 'task-views', capabilities: ['task-views'] }),
        ini({ slug: 'orphan', title: 'No home yet', projectSlug: 'kindash', tier: 'needs-you', reason: 'advance' }),
      ],
    }),
    product({ slug: 'viberpit', name: 'ViberPit', capabilities: [cap({ slug: 'sessions', title: 'Sessions' })] }),
  ],
}
```

`web/src/features/roadmap/treeRows.test.ts`:

```ts
// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { MAP } from './__fixtures__/map'
import { flattenMap, toggleKey, capabilityKey, productKey, initiativeKey, setCollapsed } from './treeRows'

const EXPANDED = { collapsed: new Set<string>() }
const keys = (rows: ReturnType<typeof flattenMap>) => rows.map((r) => r.key)

describe('flattenMap', () => {
  it('walks product → capabilities (sorted as given) → initiatives under primary → pseudo rows', () => {
    expect(keys(flattenMap(MAP, EXPANDED, ''))).toEqual([
      productKey('kindash'),
      capabilityKey('kindash', 'billing'),
      capabilityKey('kindash', 'tasks'),
      capabilityKey('kindash', 'task-views'),
      initiativeKey('views'),
      initiativeKey('orphan'),
      'pseudo:kindash:awaiting-triage',
      productKey('viberpit'),
      capabilityKey('viberpit', 'sessions'),
    ])
  })

  it('assigns depth from the tree and marks expandable rows', () => {
    const rows = flattenMap(MAP, EXPANDED, '')
    const byKey = Object.fromEntries(rows.map((r) => [r.key, r]))
    expect(byKey[productKey('kindash')]).toMatchObject({ kind: 'product', depth: 0, hasChildren: true, expanded: true })
    expect(byKey[capabilityKey('kindash', 'tasks')]).toMatchObject({ depth: 1, hasChildren: true })
    expect(byKey[capabilityKey('kindash', 'task-views')]).toMatchObject({ depth: 2, hasChildren: true })
    expect(byKey[initiativeKey('views')]).toMatchObject({ kind: 'initiative', depth: 3 })
    expect(byKey[initiativeKey('orphan')]).toMatchObject({ kind: 'initiative', depth: 1 })
    expect(byKey['pseudo:kindash:awaiting-triage']).toMatchObject({ kind: 'pseudo', count: 2 })
    expect(byKey[capabilityKey('kindash', 'billing')]).toMatchObject({ hasChildren: false })
  })

  it('collapsing hides the subtree and nothing else', () => {
    const state = toggleKey(EXPANDED, capabilityKey('kindash', 'tasks'))
    expect(keys(flattenMap(MAP, state, ''))).toEqual([
      productKey('kindash'),
      capabilityKey('kindash', 'billing'),
      capabilityKey('kindash', 'tasks'),
      initiativeKey('orphan'),
      'pseudo:kindash:awaiting-triage',
      productKey('viberpit'),
      capabilityKey('viberpit', 'sessions'),
    ])
    expect(toggleKey(state, capabilityKey('kindash', 'tasks'))).toEqual(EXPANDED)
    expect(setCollapsed(EXPANDED, 'x', true).collapsed.has('x')).toBe(true)
    expect(setCollapsed(setCollapsed(EXPANDED, 'x', true), 'x', false).collapsed.has('x')).toBe(false)
  })

  it('a query keeps matches and their ancestors and ignores collapse', () => {
    const collapsed = toggleKey(EXPANDED, productKey('kindash'))
    expect(keys(flattenMap(MAP, collapsed, 'task view'))).toEqual([
      productKey('kindash'),
      capabilityKey('kindash', 'tasks'),
      capabilityKey('kindash', 'task-views'),
      initiativeKey('views'),
    ])
    expect(keys(flattenMap(MAP, EXPANDED, 'zzz'))).toEqual([])
  })
})
```

- [ ] **Step 2: Run to verify failure**

Run: `(cd web && npx vitest run src/features/roadmap/treeRows.test.ts)`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `treeRows.ts`**

```ts
/**
 * The Map scope's row model (roadmap spec §6): a pure flattening of the map
 * payload into the rows the list column renders, with collapse and search.
 * No React, no I/O — `RoadmapListColumn` calls it on every render.
 */
import type { CapabilityNode, InitiativeNode, ProductNode, RoadmapMap } from '@/api/roadmap'

export type PseudoKind = 'awaiting-triage' | 'needs-home'

export type TreeRow =
  | { kind: 'product'; key: string; depth: 0; product: ProductNode; expanded: boolean; hasChildren: boolean }
  | { kind: 'capability'; key: string; depth: number; product: ProductNode; capability: CapabilityNode; expanded: boolean; hasChildren: boolean }
  | { kind: 'initiative'; key: string; depth: number; product: ProductNode; initiative: InitiativeNode }
  | { kind: 'pseudo'; key: string; depth: 1; product: ProductNode; pseudo: PseudoKind; count: number }

export interface TreeState {
  /** Keys of COLLAPSED nodes. Everything is expanded by default so a new
   *  product is fully visible the first time it appears. */
  collapsed: ReadonlySet<string>
}

export const EMPTY_TREE_STATE: TreeState = { collapsed: new Set() }

export const productKey = (slug: string) => `product:${slug}`
export const capabilityKey = (productSlug: string, capSlug: string) => `capability:${productSlug}:${capSlug}`
export const initiativeKey = (slug: string) => `initiative:${slug}`
const pseudoKey = (productSlug: string, kind: PseudoKind) => `pseudo:${productSlug}:${kind}`

export function toggleKey(state: TreeState, key: string): TreeState {
  return setCollapsed(state, key, !state.collapsed.has(key))
}

export function setCollapsed(state: TreeState, key: string, collapsed: boolean): TreeState {
  if (state.collapsed.has(key) === collapsed) return state
  const next = new Set(state.collapsed)
  if (collapsed) next.add(key)
  else next.delete(key)
  return { collapsed: next }
}

function norm(s: string | null | undefined): string {
  return (s ?? '').toLowerCase()
}

/** Title or slug contains every whitespace-separated term of `query`. */
export function rowMatches(row: TreeRow, query: string): boolean {
  const terms = query.toLowerCase().split(/\s+/).filter(Boolean)
  if (terms.length === 0) return true
  const hay = row.kind === 'product' ? `${norm(row.product.name)} ${norm(row.product.slug)}`
    : row.kind === 'capability' ? `${norm(row.capability.title)} ${norm(row.capability.slug)}`
    : row.kind === 'initiative' ? `${norm(row.initiative.title)} ${norm(row.initiative.slug)}`
    : row.pseudo
  return terms.every((t) => hay.includes(t))
}

/**
 * Flatten one product. Returns rows in display order. With a query, a row is
 * kept when it or any descendant matches; collapse is ignored so a search can
 * never be hidden by an earlier fold.
 */
function flattenProduct(product: ProductNode, state: TreeState, query: string): TreeRow[] {
  const searching = query.trim() !== ''
  const byPrimary = new Map<string, InitiativeNode[]>()
  const orphans: InitiativeNode[] = []
  const known = new Set<string>()
  const collect = (c: CapabilityNode) => { known.add(c.slug); c.children.forEach(collect) }
  product.capabilities.forEach(collect)
  for (const i of product.initiatives) {
    if (i.primaryCapability && known.has(i.primaryCapability)) {
      const list = byPrimary.get(i.primaryCapability) ?? []
      list.push(i)
      byPrimary.set(i.primaryCapability, list)
    } else {
      orphans.push(i)
    }
  }

  const initiativeRows = (list: InitiativeNode[], depth: number): TreeRow[] =>
    list.map((initiative) => ({ kind: 'initiative', key: initiativeKey(initiative.slug), depth, product, initiative }))

  const capRows = (c: CapabilityNode, depth: number): TreeRow[] => {
    const key = capabilityKey(product.slug, c.slug)
    const kids: TreeRow[] = [
      ...c.children.flatMap((k) => capRows(k, depth + 1)),
      ...initiativeRows(byPrimary.get(c.slug) ?? [], depth + 1),
    ]
    const self: TreeRow = { kind: 'capability', key, depth, product, capability: c, expanded: !state.collapsed.has(key), hasChildren: c.children.length > 0 || (byPrimary.get(c.slug)?.length ?? 0) > 0 }
    if (searching) {
      const keptKids = kids   // already filtered by the recursive call below
      return rowMatches(self, query) || keptKids.length > 0 ? [self, ...keptKids] : []
    }
    return self.expanded ? [self, ...kids] : [self]
  }

  const children: TreeRow[] = [
    ...product.capabilities.flatMap((c) => capRows(c, 1)),
    ...initiativeRows(orphans, 1),
    ...(product.counts.awaitingTriage > 0
      ? [{ kind: 'pseudo', key: pseudoKey(product.slug, 'awaiting-triage'), depth: 1, product, pseudo: 'awaiting-triage', count: product.counts.awaitingTriage } as TreeRow]
      : []),
    ...(product.counts.needsHome > 0
      ? [{ kind: 'pseudo', key: pseudoKey(product.slug, 'needs-home'), depth: 1, product, pseudo: 'needs-home', count: product.counts.needsHome } as TreeRow]
      : []),
  ]
  const key = productKey(product.slug)
  const self: TreeRow = { kind: 'product', key, depth: 0, product, expanded: !state.collapsed.has(key), hasChildren: children.length > 0 }
  if (searching) {
    // Leaf rows (initiatives, pseudo) were not filtered by capRows; filter here.
    const kept = children.filter((r) => r.kind === 'capability' || rowMatches(r, query))
    return rowMatches(self, query) || kept.length > 0 ? [self, ...kept] : []
  }
  return self.expanded ? [self, ...children] : [self]
}

export function flattenMap(map: RoadmapMap, state: TreeState, query: string): TreeRow[] {
  return map.products.flatMap((p) => flattenProduct(p, state, query))
}
```

Note on the search branch inside `capRows`: when `searching`, the recursive `capRows` calls already return `[]` for non-matching subtrees, but `initiativeRows(...)` are unfiltered — replace the `kids` construction under `searching` with:

```ts
    const kids: TreeRow[] = searching
      ? [
          ...c.children.flatMap((k) => capRows(k, depth + 1)),
          ...initiativeRows(byPrimary.get(c.slug) ?? [], depth + 1).filter((r) => rowMatches(r, query)),
        ]
      : [
          ...c.children.flatMap((k) => capRows(k, depth + 1)),
          ...initiativeRows(byPrimary.get(c.slug) ?? [], depth + 1),
        ]
```

and drop the `keptKids` alias.

- [ ] **Step 4: Run the tests**

Run: `(cd web && npx vitest run src/features/roadmap/treeRows.test.ts && npm run lint)`
Expected: PASS, clean. If the "query keeps matches" case differs on `initiativeKey('views')`, check that the initiative filter runs on `title` ("Use-case-first task views" contains "task" and "view").

- [ ] **Step 5: Commit**

```bash
printf 'feat(roadmap): pure tree-row model for the Map scope (flatten, collapse, search)\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c4.txt
git add web/src/features/roadmap && git commit -F /tmp/c4.txt
```

---

### Task 5: Navigation state, rows, scope segment, and the list column

**Files:**
- Create: `web/src/features/roadmap/roadmapNavState.ts`, `roadmapNavState.test.ts`
- Create: `web/src/features/roadmap/rows/TreeRowCard.tsx`, `web/src/features/roadmap/rows/InitiativeCard.tsx`
- Create: `web/src/features/roadmap/RoadmapScopeSegment.tsx`
- Create: `web/src/features/roadmap/RoadmapListColumn.tsx`, `RoadmapListColumn.test.tsx`

**Interfaces:**
- Produces (`roadmapNavState.ts`):
  ```ts
  export const ROADMAP_PATH = '/roadmap'                       // == NAV_BY_ID.roadmap.path after Task 6; literal here, asserted equal in Task 6's test
  export const PRODUCT_ROUTE = `${ROADMAP_PATH}/:productSlug`
  export const CAPABILITY_ROUTE = `${ROADMAP_PATH}/:productSlug/capabilities/:capSlug`
  export const INITIATIVES_PATH = '/initiatives'
  export const INITIATIVE_ROUTE = `${INITIATIVES_PATH}/:initiativeSlug`
  export type RoadmapScope = 'map' | 'initiatives'
  export const ROADMAP_SCOPES: readonly RoadmapScope[]
  export function roadmapScopeForSearch(search: string): RoadmapScope       // `?scope=initiatives` → 'initiatives', else 'map'
  export function roadmapPath(scope?: RoadmapScope): string                 // '/roadmap' | '/roadmap?scope=initiatives'
  export function productPath(productSlug: string): string
  export function capabilityPath(productSlug: string, capSlug: string): string
  export function initiativePath(slug: string): string
  export type RoadmapOpenItem = { kind: 'product'; productSlug: string } | { kind: 'capability'; productSlug: string; capSlug: string } | { kind: 'initiative'; initiativeSlug: string }
  export function roadmapOpenItem(params: Readonly<Record<string, string | undefined>>): RoadmapOpenItem | null
  export function keyForOpenItem(item: RoadmapOpenItem | null): string | null   // productKey / capabilityKey / initiativeKey
  ```
- Produces (`RoadmapListColumn.tsx`):
  ```ts
  export interface RoadmapListColumnProps {
    scope: RoadmapScope
    map: RoadmapMap | undefined
    isLoading: boolean
    isError: boolean
    /** Key of the open row (`keyForOpenItem`), or null. */
    selectedKey: string | null
    onOpen: (row: TreeRow) => void
    onOpenInbox: (productSlug: string) => void
  }
  export function RoadmapListColumn(props: RoadmapListColumnProps): JSX.Element
  ```
  Tree collapse state persists under `localStorage['ccswitch.roadmap.collapsed']` (a JSON string array); search state is component-local.

- [ ] **Step 1: Write the failing tests**

`web/src/features/roadmap/roadmapNavState.test.ts`:

```ts
// @vitest-environment node
import { describe, it, expect } from 'vitest'
import {
  roadmapScopeForSearch, roadmapPath, productPath, capabilityPath, initiativePath,
  roadmapOpenItem, keyForOpenItem, PRODUCT_ROUTE, CAPABILITY_ROUTE, INITIATIVE_ROUTE,
} from './roadmapNavState'

describe('roadmapNavState', () => {
  it('derives the scope from ?scope= and defaults to map', () => {
    expect(roadmapScopeForSearch('')).toBe('map')
    expect(roadmapScopeForSearch('?scope=initiatives')).toBe('initiatives')
    expect(roadmapScopeForSearch('?scope=bogus&x=1')).toBe('map')
  })
  it('builds every path from the section path', () => {
    expect(roadmapPath()).toBe('/roadmap')
    expect(roadmapPath('initiatives')).toBe('/roadmap?scope=initiatives')
    expect(productPath('kindash')).toBe('/roadmap/kindash')
    expect(capabilityPath('kindash', 'task views')).toBe('/roadmap/kindash/capabilities/task%20views')
    expect(initiativePath('views')).toBe('/initiatives/views')
    expect(PRODUCT_ROUTE).toBe('/roadmap/:productSlug')
    expect(CAPABILITY_ROUTE).toBe('/roadmap/:productSlug/capabilities/:capSlug')
    expect(INITIATIVE_ROUTE).toBe('/initiatives/:initiativeSlug')
  })
  it('resolves the open item from params by NAME, most specific first', () => {
    expect(roadmapOpenItem({})).toBeNull()
    expect(roadmapOpenItem({ productSlug: 'k' })).toEqual({ kind: 'product', productSlug: 'k' })
    expect(roadmapOpenItem({ productSlug: 'k', capSlug: 'c' })).toEqual({ kind: 'capability', productSlug: 'k', capSlug: 'c' })
    expect(roadmapOpenItem({ initiativeSlug: 'i' })).toEqual({ kind: 'initiative', initiativeSlug: 'i' })
    expect(keyForOpenItem({ kind: 'capability', productSlug: 'k', capSlug: 'c' })).toBe('capability:k:c')
    expect(keyForOpenItem(null)).toBeNull()
  })
})
```

`web/src/features/roadmap/RoadmapListColumn.test.tsx`:

```tsx
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { MAP } from './__fixtures__/map'
import { RoadmapListColumn } from './RoadmapListColumn'
import { capabilityKey } from './treeRows'

function mount(over: Partial<React.ComponentProps<typeof RoadmapListColumn>> = {}) {
  const onOpen = vi.fn()
  const onOpenInbox = vi.fn()
  render(
    <MemoryRouter>
      <RoadmapListColumn scope="map" map={MAP} isLoading={false} isError={false}
        selectedKey={null} onOpen={onOpen} onOpenInbox={onOpenInbox} {...over} />
    </MemoryRouter>,
  )
  return { onOpen, onOpenInbox }
}

describe('RoadmapListColumn', () => {
  beforeEach(() => localStorage.clear())

  it('renders the Map scope as a tree with indented rows and opens a row on click', () => {
    const { onOpen } = mount()
    expect(screen.getByRole('navigation', { name: 'Roadmap scope' })).toBeInTheDocument()
    const tasks = screen.getByRole('button', { name: 'Open capability Tasks' })
    fireEvent.click(tasks)
    expect(onOpen).toHaveBeenCalledWith(expect.objectContaining({ kind: 'capability', key: capabilityKey('kindash', 'tasks') }))
    // depth is exposed for styling and assistive tech
    expect(screen.getByTestId(`row-${capabilityKey('kindash', 'task-views')}`)).toHaveAttribute('aria-level', '3')
  })

  it('collapses a subtree from its toggle and remembers it', () => {
    mount()
    fireEvent.click(screen.getByRole('button', { name: 'Collapse Tasks' }))
    expect(screen.queryByTestId(`row-${capabilityKey('kindash', 'task-views')}`)).toBeNull()
    expect(JSON.parse(localStorage.getItem('ccswitch.roadmap.collapsed') ?? '[]')).toContain(capabilityKey('kindash', 'tasks'))
  })

  it('search narrows the tree', () => {
    mount()
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search roadmap' }), { target: { value: 'billing' } })
    expect(screen.getByTestId(`row-${capabilityKey('kindash', 'billing')}`)).toBeInTheDocument()
    expect(screen.queryByTestId(`row-${capabilityKey('viberpit', 'sessions')}`)).toBeNull()
  })

  it('the awaiting-triage pseudo row opens the inbox for that product', () => {
    const { onOpenInbox } = mount()
    fireEvent.click(screen.getByRole('button', { name: 'Open awaiting triage for KinDash' }))
    expect(onOpenInbox).toHaveBeenCalledWith('kindash')
  })

  it('the Initiatives scope lists every initiative grouped by stage', () => {
    mount({ scope: 'initiatives' })
    expect(screen.getByRole('button', { name: 'Open initiative Use-case-first task views' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Open initiative No home yet' })).toBeInTheDocument()
    expect(screen.getByText('Ready to advance')).toBeInTheDocument()
  })

  it('states: loading, error, empty', () => {
    mount({ map: undefined, isLoading: true })
    expect(screen.getByRole('status', { name: 'Loading roadmap' })).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run to verify failure**

Run: `(cd web && npx vitest run src/features/roadmap)`
Expected: FAIL — modules not found.

- [ ] **Step 3: Create `roadmapNavState.ts`**

```ts
/**
 * Where the roadmap lives and what is open — the one module that spells these
 * paths (finish-line spec §4.2), modelled on `session/sessionNavState.ts`.
 * The route PARAM NAMES are the contract: `OPEN_ITEM_RESOLVERS.roadmap`
 * discriminates on them alone.
 */
import { capabilityKey, initiativeKey, productKey } from './treeRows'

export const ROADMAP_PATH = '/roadmap'
export const PRODUCT_ROUTE = `${ROADMAP_PATH}/:productSlug`
export const CAPABILITY_ROUTE = `${ROADMAP_PATH}/:productSlug/capabilities/:capSlug`
/** Initiative detail keeps its own prefix (roadmap spec §4): `/initiatives/:slug`. */
export const INITIATIVES_PATH = '/initiatives'
export const INITIATIVE_ROUTE = `${INITIATIVES_PATH}/:initiativeSlug`

export type RoadmapScope = 'map' | 'initiatives'
export const ROADMAP_SCOPES: readonly RoadmapScope[] = ['map', 'initiatives']

export function roadmapScopeForSearch(search: string): RoadmapScope {
  const v = new URLSearchParams(search).get('scope')
  return v === 'initiatives' ? 'initiatives' : 'map'
}

export function roadmapPath(scope: RoadmapScope = 'map'): string {
  return scope === 'map' ? ROADMAP_PATH : `${ROADMAP_PATH}?scope=${scope}`
}
export const productPath = (productSlug: string) =>
  `${ROADMAP_PATH}/${encodeURIComponent(productSlug)}`
export const capabilityPath = (productSlug: string, capSlug: string) =>
  `${productPath(productSlug)}/capabilities/${encodeURIComponent(capSlug)}`
export const initiativePath = (slug: string) => `${INITIATIVES_PATH}/${encodeURIComponent(slug)}`

export type RoadmapOpenItem =
  | { kind: 'product'; productSlug: string }
  | { kind: 'capability'; productSlug: string; capSlug: string }
  | { kind: 'initiative'; initiativeSlug: string }

export function roadmapOpenItem(
  params: Readonly<Record<string, string | undefined>>,
): RoadmapOpenItem | null {
  const { productSlug, capSlug, initiativeSlug } = params
  if (initiativeSlug) return { kind: 'initiative', initiativeSlug }
  if (productSlug && capSlug) return { kind: 'capability', productSlug, capSlug }
  if (productSlug) return { kind: 'product', productSlug }
  return null
}

/** The list-row key an open item highlights. */
export function keyForOpenItem(item: RoadmapOpenItem | null): string | null {
  if (!item) return null
  if (item.kind === 'product') return productKey(item.productSlug)
  if (item.kind === 'capability') return capabilityKey(item.productSlug, item.capSlug)
  return initiativeKey(item.initiativeSlug)
}
```

- [ ] **Step 4: Create the row cards**

`web/src/features/roadmap/rows/TreeRowCard.tsx` — one component for product / capability / pseudo rows; initiatives get their own card because they render a stage chip and a reason:

```tsx
import { ChevronDown, ChevronRight, Inbox, HelpCircle, Package, Layers } from 'lucide-react'
import { cn } from '@/lib/utils'
import { CardFrame } from '@/components/card/CardFrame'
import { Badge } from '@/ui/Badge'
import { badgedName } from '@/ui/badgedName'
import { CAPABILITY, PRODUCT, tierLabel as _unused } from '@/features/products/vocab'
import { TIER_ACCENT, TIER_TONE, tierLabel } from '../roadmapTier'
import type { TreeRow } from '../treeRows'

export interface TreeRowCardProps {
  row: Exclude<TreeRow, { kind: 'initiative' }>
  selected: boolean
  onOpen: (row: TreeRow) => void
  onToggle: (key: string) => void
  onOpenInbox: (productSlug: string) => void
}

const INDENT = ['pl-0', 'pl-4', 'pl-8', 'pl-12', 'pl-16', 'pl-20'] as const
const indent = (depth: number) => INDENT[Math.min(depth, INDENT.length - 1)]

function TierDot({ tier }: { tier: TreeRow extends { kind: 'pseudo' } ? never : keyof typeof TIER_ACCENT }) {
  const a = TIER_ACCENT[tier]
  return (
    <span aria-hidden="true" className={cn('inline-block size-2 shrink-0 rounded-full', a.dot, a.pulse && 'animate-pulse')} />
  )
}

export function TreeRowCard({ row, selected, onOpen, onToggle, onOpenInbox }: TreeRowCardProps) {
  if (row.kind === 'pseudo') {
    const label = row.pseudo === 'awaiting-triage' ? 'Awaiting triage' : 'Needs a home'
    const Icon = row.pseudo === 'awaiting-triage' ? Inbox : HelpCircle
    return (
      <div data-testid={`row-${row.key}`} role="treeitem" aria-level={row.depth + 1} className={indent(row.depth)}>
        <CardFrame
          id={row.key}
          openLabel={`Open ${label.toLowerCase()} for ${row.product.name ?? row.product.slug}`}
          railClass={TIER_ACCENT['needs-you'].rail}
          selected={selected}
          listMode={false}
          focusable
          swipeEnabled={false}
          onOpen={() => onOpenInbox(row.product.slug)}
          identity={
            <span className="flex min-w-0 items-center gap-2 text-sm text-ink">
              <Icon size={14} aria-hidden="true" className="shrink-0 text-ink-muted" />
              <span className="truncate">{label}</span>
              <Badge tone="warning" count={row.count} aria-hidden="true" />
              <span className="sr-only">{badgedName({ label, count: row.count })}</span>
            </span>
          }
        />
      </div>
    )
  }

  const isProduct = row.kind === 'product'
  const title = isProduct ? (row.product.name ?? row.product.slug) : (row.capability.title ?? row.capability.slug)
  const tier = isProduct ? row.product.tier : row.capability.tier
  const counts = isProduct ? row.product.counts : row.capability.counts
  const noun = isProduct ? PRODUCT.singular : CAPABILITY.singular
  const meta = isProduct
    ? `${row.product.counts.capabilities} ${CAPABILITY.plural.toLowerCase()}`
    : `${row.capability.counts.openIssues} open`

  return (
    <div data-testid={`row-${row.key}`} role="treeitem" aria-level={row.depth + 1}
         aria-expanded={row.hasChildren ? row.expanded : undefined} className={indent(row.depth)}>
      <CardFrame
        id={row.key}
        openLabel={`Open ${noun.toLowerCase()} ${title}`}
        railClass={TIER_ACCENT[tier].rail}
        selected={selected}
        listMode={false}
        focusable
        swipeEnabled={false}
        onOpen={() => onOpen(row)}
        leading={row.hasChildren ? (
          <button type="button"
                  aria-label={`${row.expanded ? 'Collapse' : 'Expand'} ${title}`}
                  onClick={(e) => { e.stopPropagation(); onToggle(row.key) }}
                  className="flex size-6 shrink-0 items-center justify-center rounded-sm text-ink-muted hover:bg-surface-raised focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent">
            {row.expanded ? <ChevronDown size={14} aria-hidden="true" /> : <ChevronRight size={14} aria-hidden="true" />}
          </button>
        ) : <span aria-hidden="true" className="size-6 shrink-0" />}
        identity={
          <span className="flex min-w-0 items-center gap-2">
            <TierDot tier={tier} />
            {isProduct ? <Package size={14} aria-hidden="true" className="shrink-0 text-ink-muted" />
                       : <Layers size={14} aria-hidden="true" className="shrink-0 text-ink-muted" />}
            <span className={cn('truncate text-sm', isProduct ? 'font-semibold text-ink' : 'font-medium text-ink')}>{title}</span>
          </span>
        }
        trailing={
          counts.needsYou > 0 || counts.blocked > 0 ? (
            <Badge tone={TIER_TONE[tier]} count={counts.needsYou || counts.blocked} aria-label={`${counts.needsYou || counts.blocked} ${tierLabel(tier).toLowerCase()}`} />
          ) : undefined
        }
      >
        <span className="block truncate text-xs text-ink-muted">{meta} · {tierLabel(tier)}</span>
      </CardFrame>
    </div>
  )
}
```

Remove the `tierLabel as _unused` import before committing (it is there only to show the vocabulary import shape; lint rejects unused imports).

`web/src/features/roadmap/rows/InitiativeCard.tsx`:

```tsx
import { Diamond } from 'lucide-react'
import { cn } from '@/lib/utils'
import { CardFrame } from '@/components/card/CardFrame'
import { Badge } from '@/ui/Badge'
import type { InitiativeNode } from '@/api/roadmap'
import { INITIATIVE, reasonLabel, stageLabel } from '@/features/products/vocab'
import { TIER_ACCENT, TIER_TONE, tierLabel } from '../roadmapTier'

export interface InitiativeCardProps {
  initiative: InitiativeNode
  rowKey: string
  depth: number
  selected: boolean
  onOpen: () => void
}

const INDENT = ['pl-0', 'pl-4', 'pl-8', 'pl-12', 'pl-16', 'pl-20'] as const

export function InitiativeCard({ initiative, rowKey, depth, selected, onOpen }: InitiativeCardProps) {
  const title = initiative.title ?? initiative.slug
  const a = TIER_ACCENT[initiative.tier]
  const reason = reasonLabel(initiative.reason)
  const epic = initiative.epic
  return (
    <div data-testid={`row-${rowKey}`} role="treeitem" aria-level={depth + 1}
         className={INDENT[Math.min(depth, INDENT.length - 1)]}>
      <CardFrame
        id={rowKey}
        openLabel={`Open ${INITIATIVE.singular.toLowerCase()} ${title}`}
        railClass={a.rail}
        selected={selected}
        listMode={false}
        focusable
        swipeEnabled={false}
        onOpen={onOpen}
        leading={<span aria-hidden="true" className="size-6 shrink-0" />}
        identity={
          <span className="flex min-w-0 items-center gap-2">
            <span aria-hidden="true" className={cn('inline-block size-2 shrink-0 rounded-full', a.dot, a.pulse && 'animate-pulse')} />
            <Diamond size={14} aria-hidden="true" className="shrink-0 text-ink-muted" />
            <span className="truncate text-sm font-medium text-ink">{title}</span>
          </span>
        }
        trailing={<Badge tone={TIER_TONE[initiative.tier]}>{stageLabel(initiative.stage)}</Badge>}
      >
        <span className="block truncate text-xs text-ink-muted">
          {reason ? <span className="text-warning">{reason}</span> : tierLabel(initiative.tier)}
          {epic ? ` · ${epic.done}/${epic.total} done` : ''}
        </span>
      </CardFrame>
    </div>
  )
}
```

- [ ] **Step 5: Create `RoadmapScopeSegment.tsx`**

```tsx
// Map | Initiatives — the roadmap list's scope switch. Like the sessions
// `ScopeSegment`: links, no state, `aria-current` marks the scope in the URL.
import { Link } from 'react-router-dom'
import { cn } from '@/lib/utils'
import { Badge } from '@/ui/Badge'
import { badgedName } from '@/ui/badgedName'
import { ROADMAP } from '@/features/products/vocab'
import { roadmapPath, type RoadmapScope } from './roadmapNavState'

export interface RoadmapScopeSegmentProps {
  scope: RoadmapScope
  productCount: number
  initiativeCount: number
}

const item = cn(
  'flex-1 inline-flex min-h-target items-center justify-center gap-1.5 rounded-md px-3 text-sm font-medium',
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
)
const here = 'bg-accent-wash text-accent'
const elsewhere = 'text-ink-muted hover:bg-surface-raised hover:text-ink'

function ScopeLink({ to, label, count, current }: { to: string; label: string; count: number; current: boolean }) {
  return (
    <Link to={to} aria-current={current ? 'page' : undefined} className={cn(item, current ? here : elsewhere)}>
      <span aria-hidden="true">{label}</span>
      <span className="sr-only">{badgedName({ label, count })}</span>
      {count > 0 && <Badge tone={current ? 'accent' : 'neutral'} count={count} aria-hidden="true" />}
    </Link>
  )
}

export function RoadmapScopeSegment({ scope, productCount, initiativeCount }: RoadmapScopeSegmentProps) {
  return (
    <nav aria-label="Roadmap scope" className="flex gap-1 rounded-md border border-edge bg-surface p-1">
      <ScopeLink to={roadmapPath('map')} label={ROADMAP.map} count={productCount} current={scope === 'map'} />
      <ScopeLink to={roadmapPath('initiatives')} label={ROADMAP.initiatives} count={initiativeCount} current={scope === 'initiatives'} />
    </nav>
  )
}
```

- [ ] **Step 6: Create `RoadmapListColumn.tsx`**

```tsx
/**
 * `app.list` for the Roadmap section (finish-line spec §4.3, roadmap spec §6).
 * Map scope: the flattened tree from `treeRows.ts`. Initiatives scope: every
 * initiative through `useItemOrdering` + `GroupedList`, grouped by stage.
 * Region-neutral like `SessionListColumn`: fills what `ContextList` gives it.
 */
import { useEffect, useMemo, useState } from 'react'
import type { InitiativeNode, RoadmapMap } from '@/api/roadmap'
import { GroupedList } from '@/components/GroupedList'
import { SessionListControls } from '@/components/SessionListControls'
import { EmptyState } from '@/ui/EmptyState'
import { Skeleton } from '@/ui/Skeleton'
import { Banner } from '@/ui/Banner'
import { INITIATIVE_STAGES } from '@/api/sdlc'
import { useItemOrdering, type OrderingSpec } from '@/session/useSessionOrdering'
import { PRODUCT, ROADMAP, stageLabel } from '@/features/products/vocab'
import { ScopeBody } from '@/features/sessions/ScopeBody'
import { TIER_RANK } from './roadmapTier'
import { RoadmapScopeSegment } from './RoadmapScopeSegment'
import { TreeRowCard } from './rows/TreeRowCard'
import { InitiativeCard } from './rows/InitiativeCard'
import type { RoadmapScope } from './roadmapNavState'
import {
  EMPTY_TREE_STATE, flattenMap, initiativeKey, toggleKey, type TreeRow, type TreeState,
} from './treeRows'

export interface RoadmapListColumnProps {
  scope: RoadmapScope
  map: RoadmapMap | undefined
  isLoading: boolean
  isError: boolean
  selectedKey: string | null
  onOpen: (row: TreeRow) => void
  onOpenInbox: (productSlug: string) => void
}

const COLLAPSED_KEY = 'ccswitch.roadmap.collapsed'

function loadCollapsed(): TreeState {
  try {
    const raw = localStorage.getItem(COLLAPSED_KEY)
    const arr: unknown = raw ? JSON.parse(raw) : []
    return Array.isArray(arr) ? { collapsed: new Set(arr.filter((k): k is string => typeof k === 'string')) } : EMPTY_TREE_STATE
  } catch {
    return EMPTY_TREE_STATE
  }
}

export function RoadmapListColumn({ scope, map, isLoading, isError, selectedKey, onOpen, onOpenInbox }: RoadmapListColumnProps) {
  const products = map?.products ?? []
  const initiatives = useMemo(() => products.flatMap((p) => p.initiatives), [products])
  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden">
      <div className="shrink-0 p-2">
        <RoadmapScopeSegment scope={scope} productCount={products.length} initiativeCount={initiatives.length} />
      </div>
      {isError && <Banner tone="danger" className="mx-2">Could not load the roadmap.</Banner>}
      {scope === 'map' && (
        <MapScopeBody map={map} isLoading={isLoading} selectedKey={selectedKey} onOpen={onOpen} onOpenInbox={onOpenInbox} />
      )}
      {scope === 'initiatives' && (
        <InitiativesScopeBody initiatives={initiatives} isLoading={isLoading} selectedKey={selectedKey} onOpen={onOpen} />
      )}
    </div>
  )
}

function MapScopeBody({ map, isLoading, selectedKey, onOpen, onOpenInbox }: Omit<RoadmapListColumnProps, 'scope' | 'isError'>) {
  const [tree, setTree] = useState<TreeState>(loadCollapsed)
  const [query, setQuery] = useState('')
  useEffect(() => { localStorage.setItem(COLLAPSED_KEY, JSON.stringify([...tree.collapsed])) }, [tree])
  const rows = useMemo(() => (map ? flattenMap(map, tree, query) : []), [map, tree, query])

  return (
    <ScopeBody
      controls={
        <div className="p-2">
          <input type="search" role="searchbox" aria-label="Search roadmap" placeholder="Search roadmap…"
                 value={query} onChange={(e) => setQuery(e.target.value)}
                 className="w-full min-h-target rounded-md border border-edge bg-surface px-3 text-sm text-ink placeholder:text-ink-faint focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent" />
        </div>
      }
    >
      {isLoading && !map && <div role="status" aria-label="Loading roadmap" className="p-2"><Skeleton lines={6} /></div>}
      {map && rows.length === 0 && (
        <EmptyState title={query ? 'No matches' : PRODUCT.empty} />
      )}
      {rows.length > 0 && (
        <div role="tree" aria-label={ROADMAP.map} className="flex flex-col gap-1 p-2">
          {rows.map((row) => row.kind === 'initiative'
            ? <InitiativeCard key={row.key} rowKey={row.key} depth={row.depth} initiative={row.initiative}
                              selected={selectedKey === row.key} onOpen={() => onOpen(row)} />
            : <TreeRowCard key={row.key} row={row} selected={selectedKey === row.key} onOpen={onOpen}
                           onToggle={(k) => setTree((t) => toggleKey(t, k))} onOpenInbox={onOpenInbox} />)}
        </div>
      )}
    </ScopeBody>
  )
}

type InitiativeGroupBy = 'stage' | 'product' | 'none'
type InitiativeSortBy = 'tier' | 'title'

const INITIATIVE_SPEC: OrderingSpec<InitiativeGroupBy, InitiativeSortBy, InitiativeNode> = {
  namespace: 'roadmap-initiatives',
  groupByValues: ['stage', 'product', 'none'],
  sortByValues: ['tier', 'title'],
  defaultPref: { groupBy: 'stage', sortBy: 'tier' },
  config: {
    idOf: (i) => i.slug,
    groupings: {
      stage: { bucketOf: (i) => ({ key: i.stage ?? 'unknown', label: stageLabel(i.stage), order: Math.max(0, INITIATIVE_STAGES.indexOf(i.stage as never)) }) },
      product: { bucketOf: (i) => ({ key: i.projectSlug, label: i.projectSlug }) },
    },
    sorts: {
      tier: (a, b) => TIER_RANK[a.tier] - TIER_RANK[b.tier] || (a.title ?? a.slug).localeCompare(b.title ?? b.slug),
      title: (a, b) => (a.title ?? a.slug).localeCompare(b.title ?? b.slug),
    },
  },
}

function InitiativesScopeBody({ initiatives, isLoading, selectedKey, onOpen }: {
  initiatives: readonly InitiativeNode[]; isLoading: boolean; selectedKey: string | null; onOpen: (row: TreeRow) => void
}) {
  const [query, setQuery] = useState('')
  const q = query.trim().toLowerCase()
  const visible = useMemo(
    () => (q ? initiatives.filter((i) => `${i.title ?? ''} ${i.slug}`.toLowerCase().includes(q)) : initiatives),
    [initiatives, q],
  )
  const ordering = useItemOrdering(visible, INITIATIVE_SPEC)
  return (
    <ScopeBody
      controls={
        <SessionListControls<InitiativeGroupBy, InitiativeSortBy>
          groupBy={ordering.groupBy} sortBy={ordering.sortBy}
          onGroupByChange={ordering.setGroupBy} onSortByChange={ordering.setSortBy}
          searchQuery={query} onSearchChange={setQuery}
          searchPlaceholder="Search initiatives…" searchAriaLabel="Search initiatives"
          groupOptions={[{ value: 'stage', label: 'Stage' }, { value: 'product', label: PRODUCT.singular }, { value: 'none', label: 'None' }]}
          sortOptions={[{ value: 'tier', label: 'Attention' }, { value: 'title', label: 'Title' }]}
        />
      }
    >
      {isLoading && initiatives.length === 0 && <div role="status" aria-label="Loading roadmap" className="p-2"><Skeleton lines={6} /></div>}
      {!isLoading && visible.length === 0 && <EmptyState title={q ? 'No matches' : 'No initiatives yet'} />}
      <GroupedList<InitiativeNode>
        groups={ordering.groups}
        collapsedKeys={ordering.collapsedKeys}
        onToggleCollapse={ordering.toggleCollapse}
        panelRole="list"
        ariaLabel={ROADMAP.initiatives}
        renderRow={(i) => {
          const key = initiativeKey(i.slug)
          const product = { slug: i.projectSlug, name: null, tier: 'idle' as const, counts: { inFlight: 0, blocked: 0, needsYou: 0, capabilities: 0, awaitingTriage: 0, needsHome: 0 }, capabilities: [], initiatives: [] }
          return (
            <InitiativeCard key={key} rowKey={key} depth={0} initiative={i} selected={selectedKey === key}
                            onOpen={() => onOpen({ kind: 'initiative', key, depth: 0, product, initiative: i })} />
          )
        }}
      />
    </ScopeBody>
  )
}
```

If `SessionListControls`' `filters` prop is required by its type, pass `filters={undefined}`; if `GroupedList` requires `listMode`, pass `listMode={false}`. Check the real prop types rather than guessing: `web/src/components/SessionListControls.tsx`, `web/src/components/GroupedList.tsx`.

- [ ] **Step 7: Run the tests and the lint**

Run: `(cd web && npx vitest run src/features/roadmap && npm run lint && npm run typecheck)`
Expected: PASS, clean. Common failures: `Badge` children vs `count` (use `count` for numbers, children for text); `role="searchbox"` is implicit for `<input type="search">` — drop the explicit `role` if the a11y lint objects.

- [ ] **Step 8: Commit**

```bash
printf 'feat(roadmap): list column — Map tree and Initiatives scopes on the sessions chassis\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c5.txt
git add web/src/features/roadmap && git commit -F /tmp/c5.txt
```

---

### Task 6: Shell wiring — the `roadmap` section replaces `projects` and `initiatives`

**Files:**
- Modify: `web/src/shell/navDefs.ts`, `navDefs.test.ts`, `listColumns.tsx`, `openItems.ts`, `routes.tsx`, `routes.test.tsx`, `AppShell.tsx`
- Create: `web/src/features/roadmap/useRoadmapWorkspace.ts`, `web/src/features/roadmap/RoadmapPage.tsx`, `web/src/features/roadmap/InitiativePage.tsx`
- Modify: `web/src/features/settings/pageContract.test.tsx`
- Delete: `web/src/projects/ProjectsTab.tsx`, `web/src/projects/__tests__/ProjectsTab.test.tsx`, `web/src/projects/projects.css`, `web/src/initiatives/InitiativesTab.tsx`, `web/src/initiatives/__tests__/InitiativesTab.test.tsx`, `web/src/initiatives/initiatives.css`
- Keep (moved later): `OnboardingWizard.tsx`, `DependencyPanel.tsx`, `IncidentQueue.tsx`, `InitiativeTrack.tsx` and their tests. Grep them for `projects.css`/`initiatives.css` class names (`pm-*`, `init-*`, `incident-*`); those classes lose their styles when the css files go, so replace them with token classes in the SAME task (they are small: `pm-error`/`init-error` → `text-sm text-danger`, `pm-muted`/`init-empty` → `text-sm text-ink-muted`, `btn-ghost` stays — it is a `plotroom.css` class).

**Interfaces:**
- Produces: `NavId` gains `'roadmap'`, loses `'projects'` and `'initiatives'`; `NAV_BY_ID.roadmap = { path: '/roadmap', label: 'Roadmap', hasList: true, showInKiosk: false }`.
- Produces: `OpenItem` gains `{ kind: 'product' | 'capability' | 'initiative'; … }` (the `RoadmapOpenItem` members); `OPEN_ITEM_RESOLVERS.roadmap = roadmapOpenItem`.
- Produces: `ListColumnSources.roadmap: RoadmapListColumnProps`; `LIST_SECTIONS = ['sessions', 'roadmap']`.
- Produces: `useRoadmapWorkspace(): { list: RoadmapListColumnProps }`; `AppShellInner` calls it and passes `{ sessions: workspace.list, roadmap: roadmap.list }`.
- Produces: routes `/roadmap`, `/roadmap/:productSlug`, `/roadmap/:productSlug/capabilities/:capSlug` → `<RoadmapPage />`; `/initiatives/:initiativeSlug` → `<InitiativePage />`; redirects `/projects` → `/roadmap`, `/projects/:slug` → `/roadmap/:slug`, `/initiatives` → `/roadmap?scope=initiatives`.
- `RoadmapPage` in this task renders `PageHeader` (title from the open item: `Roadmap` / product name / capability title) and an `EmptyState` body; Tasks 8–11 fill the body. `InitiativePage` renders `PageHeader` with the initiative title; Task 12 fills it.

- [ ] **Step 1: Update the pinned tests first**

`navDefs.test.ts`: the order/labels expectation becomes

```ts
      ['sessions', 'Sessions'],
      ['pipeline', 'Pipeline'],
      ['automations', 'Automations'],
      ['roadmap', 'Roadmap'],
      ['triage', 'Triage'],
      ['insights', 'Insights'],
      ['settings', 'Settings'],
```

and `'only Sessions owns a list column'` becomes `'Sessions and Roadmap own list columns'` expecting `['sessions', 'roadmap']`. Add:

```ts
  it('the roadmap section path is the one roadmapNavState builds from', async () => {
    const { ROADMAP_PATH } = await import('@/features/roadmap/roadmapNavState')
    expect(NAV_BY_ID.roadmap.path).toBe(ROADMAP_PATH)
  })
```

`pageContract.test.tsx`: replace the `initiatives` and `projects` rows with

```ts
  { nav: 'roadmap', path: '/roadmap',                      variant: 'standard', title: 'Roadmap' },
  { nav: 'roadmap', path: '/roadmap/kindash',              variant: 'standard', title: 'KinDash' },
  { nav: 'roadmap', path: '/roadmap/kindash/capabilities/tasks', variant: 'standard', title: 'Tasks' },
```

and extend the `api.get` mock: `if (path === '/pm/map') return MAP` (import `MAP` from `@/features/roadmap/__fixtures__/map` inside the factory with `await import(...)`), `if (path === '/pm/queue') return { entries: [], skipped: 0, generatedAt: 'now' }`, `if (path.startsWith('/pm/projects/')) return { id: 'p', slug: 'kindash', name: 'KinDash', repos: [], edges: [], portfolioTags: [], description: null, status: null, onboardingState: 'live', createdAt: null, updatedAt: null, featureCount: 0, workItemCount: 0 }`, `if (path.startsWith('/pm/leaves')) return { items: [] }`, `if (path === '/sdlc/triage') return { items: [] }`, `if (path.startsWith('/pm/projects') && path.endsWith('/onboarding')) return { state: 'live', action: { type: 'none' }, counts: { repoCount: 0, backfillDoneCount: 0, featureCount: 0, openUnclassifiedCount: 0, pendingProposalCount: 0 }, mapPr: null }` (put the more specific `/onboarding` and `/deps` checks BEFORE the generic `/pm/projects/` one).

`routes.test.tsx`: delete the `InitiativesTab` and `ProjectsTab` mocks; add

```ts
vi.mock('@/features/roadmap/RoadmapPage', () => ({ RoadmapPage: stub('roadmap') }))
vi.mock('@/features/roadmap/InitiativePage', () => ({ InitiativePage: stub('initiative') }))
```

and to the redirect table (the `['/archived', '/sessions/archived']` block) add

```ts
    ['/projects', '/roadmap'],
    ['/projects/kindash', '/roadmap/kindash'],
    ['/initiatives', '/roadmap?scope=initiatives'],
```

and to the "which page renders where" table add `['/roadmap', 'page-roadmap']`, `['/roadmap/kindash/capabilities/tasks', 'page-roadmap']`, `['/initiatives/views', 'page-initiative']`. Update any row that expected `page-initiatives` / `page-projects`.

- [ ] **Step 2: Run the three test files to see them fail**

Run: `(cd web && npx vitest run src/shell/navDefs.test.ts src/shell/routes.test.tsx src/features/settings/pageContract.test.tsx)`
Expected: FAIL on the new expectations.

- [ ] **Step 3: `navDefs.ts`**

Replace the `initiatives` and `projects` rows with one row between `automations` and `triage` (import `Map as MapIcon` from lucide-react, drop `Target` and `Package` if unused):

```ts
  // showInKiosk:false — `RoadmapPage` advances initiatives (`advance.mutate`),
  // creates them, and syncs products; every one is a mutation.
  { id: 'roadmap',     label: ROADMAP.label, icon: MapIcon,         path: '/roadmap',     showInKiosk: false, hasList: true,
    help: ROADMAP.help },
```

Import `ROADMAP` from `@/features/products/vocab`. Update the `hasList` doc comment ("Only Sessions does today" → "Sessions and Roadmap"). Update the `triage` row's help text to drop "Triage is its own section" wording if it references Products.

- [ ] **Step 4: `openItems.ts`**

```ts
import type { Params } from 'react-router-dom'
import type { NavId } from './navDefs'
import { roadmapOpenItem, type RoadmapOpenItem } from '@/features/roadmap/roadmapNavState'

export type OpenItem =
  | { kind: 'session'; id: string }
  | { kind: 'archived'; id: string }
  | RoadmapOpenItem

export type OpenItemResolver = (params: Readonly<Params<string>>) => OpenItem | null

export const OPEN_ITEM_RESOLVERS: Partial<Record<NavId, OpenItemResolver>> = {
  sessions: ({ id, archivedId }) => {
    if (archivedId) return { kind: 'archived', id: archivedId }
    if (id) return { kind: 'session', id }
    return null
  },
  // Product / capability / initiative detail, by PARAM NAME (never `id`, so a
  // roadmap route can never be mistaken for a live session).
  roadmap: (params) => roadmapOpenItem(params),
}
```

`useOpenSessionId` is unchanged: it narrows on `kind === 'session'`. Note `/initiatives/:initiativeSlug` resolves to NO nav section (`navDefForPath('/initiatives/x')` is null after the row is deleted), so `useOpenItem` returns null there and the phone shows the page; that is correct — the initiative page is a full-width detail with a breadcrumb back to `/roadmap`. To keep the rail highlighting Roadmap on that page, extend `navDefForPath`:

```ts
/** Paths that belong to a section without being under its path. */
const SECTION_ALIASES: readonly { prefix: string; id: NavId }[] = [
  { prefix: '/initiatives', id: 'roadmap' },
]

export function navDefForPath(pathname: string): NavDef | null {
  let best: NavDef | null = null
  for (const d of NAV_DEFS) {
    if (pathname === d.path || pathname.startsWith(d.path + '/')) {
      if (!best || d.path.length > best.path.length) best = d
    }
  }
  if (best) return best
  for (const a of SECTION_ALIASES) {
    if (pathname === a.prefix || pathname.startsWith(a.prefix + '/')) return NAV_BY_ID[a.id]
  }
  return null
}
```

and add to `navDefs.test.ts`: `expect(navDefForPath('/initiatives/views')?.id).toBe('roadmap')`. With the alias, `/initiatives/:initiativeSlug` DOES resolve to `roadmap`, so `OPEN_ITEM_RESOLVERS.roadmap` sees `initiativeSlug` and reports an open initiative — the list column stays beside it on desktop and yields the band on phone. Good; that is the intended behaviour.

- [ ] **Step 5: `listColumns.tsx`**

```tsx
import type * as React from 'react'
import type { NavId } from './navDefs'
import { SessionListColumn, type SessionListColumnProps } from '@/features/sessions/SessionListColumn'
import { RoadmapListColumn, type RoadmapListColumnProps } from '@/features/roadmap/RoadmapListColumn'

export const LIST_SECTIONS = ['sessions', 'roadmap'] as const satisfies readonly NavId[]

export interface ListColumnSources {
  sessions: SessionListColumnProps
  roadmap: RoadmapListColumnProps
}

export function listColumnFor(id: NavId | null | undefined, sources: ListColumnSources): React.ReactNode {
  switch (id) {
    case 'sessions': return <SessionListColumn {...sources.sessions} />
    case 'roadmap': return <RoadmapListColumn {...sources.roadmap} />
    default: return null
  }
}
```

- [ ] **Step 6: `useRoadmapWorkspace.ts`**

```ts
/**
 * The Roadmap section's workspace: list props + navigation, the sibling of
 * `AppShell`'s `useSessionsWorkspace`. Selection is the URL (`roadmapNavState`).
 */
import { useCallback } from 'react'
import { useLocation, useNavigate, useParams } from 'react-router-dom'
import { useRoadmapMap } from '@/api/hooks'
import { NAV_BY_ID } from '@/shell/navDefs'
import type { RoadmapListColumnProps } from './RoadmapListColumn'
import {
  capabilityPath, initiativePath, keyForOpenItem, productPath, roadmapOpenItem,
  roadmapScopeForSearch,
} from './roadmapNavState'
import type { TreeRow } from './treeRows'

export function useRoadmapWorkspace(): { list: RoadmapListColumnProps } {
  const navigate = useNavigate()
  const { search } = useLocation()
  const params = useParams()
  const { data: map, isLoading, isError } = useRoadmapMap()

  const onOpen = useCallback((row: TreeRow) => {
    if (row.kind === 'product') navigate(productPath(row.product.slug))
    else if (row.kind === 'capability') navigate(capabilityPath(row.product.slug, row.capability.slug))
    else if (row.kind === 'initiative') navigate(initiativePath(row.initiative.slug))
  }, [navigate])

  // The inbox is still the Triage section until finish-line row 3 folds it in.
  const onOpenInbox = useCallback((productSlug: string) => {
    navigate(`${NAV_BY_ID.triage.path}?projectSlug=${encodeURIComponent(productSlug)}`)
  }, [navigate])

  return {
    list: {
      scope: roadmapScopeForSearch(search),
      map,
      isLoading,
      isError,
      selectedKey: keyForOpenItem(roadmapOpenItem(params)),
      onOpen,
      onOpenInbox,
    },
  }
}
```

- [ ] **Step 7: `AppShell.tsx`**

Import `useRoadmapWorkspace` and, in `AppShellInner` right after `const workspace = useSessionsWorkspace(openDrawer)`, add `const roadmap = useRoadmapWorkspace()`; change the `ContextList` line to
`<ContextList column={listColumnFor(def?.id, { sessions: workspace.list, roadmap: roadmap.list })} />`.
`useRoadmapMap` polls every 15 s while mounted; it is now mounted app-wide like `useSessions`. Acceptable (one request per 15 s), and the same query feeds the page, so no second fetch.

- [ ] **Step 8: Page skeletons**

`web/src/features/roadmap/RoadmapPage.tsx`:

```tsx
import { useParams } from 'react-router-dom'
import { useRoadmapMap } from '@/api/hooks'
import { PageHeader } from '@/ui/PageHeader'
import { PageContent } from '@/ui/PageContent'
import { EmptyState } from '@/ui/EmptyState'
import { Breadcrumb, type BreadcrumbItem } from '@/ui/Breadcrumb'
import { ROADMAP } from '@/features/products/vocab'
import { productPath, roadmapOpenItem, roadmapPath } from './roadmapNavState'
import { findCapability, findProduct } from './lookup'

/** `/roadmap`, `/roadmap/:productSlug`, `/roadmap/:productSlug/capabilities/:capSlug` —
 *  one routed page, three detail levels chosen by the params (spec §7). */
export function RoadmapPage() {
  const item = roadmapOpenItem(useParams())
  const { data: map, isLoading } = useRoadmapMap()
  const product = item && item.kind !== 'initiative' ? findProduct(map, item.productSlug) : null
  const capability = item?.kind === 'capability' && product ? findCapability(product, item.capSlug) : null

  const crumbs: BreadcrumbItem[] = [{ label: ROADMAP.label, to: roadmapPath() }]
  let title = ROADMAP.label
  if (item && item.kind !== 'initiative') {
    title = product?.name ?? item.productSlug
    crumbs.push({ label: title, to: item.kind === 'capability' ? productPath(item.productSlug) : undefined })
  }
  if (item?.kind === 'capability') {
    title = capability?.title ?? item.capSlug
    crumbs.push({ label: title })
  }

  return (
    <div className="flex h-full min-h-0 flex-col" data-page-variant="standard">
      <PageHeader title={title} meta={crumbs.length > 1 ? <Breadcrumb items={crumbs} /> : undefined} />
      <PageContent busy={isLoading && !map}>
        <EmptyState title={item ? 'Loading…' : 'Select a product from the list'} />
      </PageContent>
    </div>
  )
}
```

`web/src/features/roadmap/lookup.ts` (pure, tested in Task 8):

```ts
import type { CapabilityNode, ProductNode, RoadmapMap } from '@/api/roadmap'

export function findProduct(map: RoadmapMap | undefined, slug: string): ProductNode | null {
  return map?.products.find((p) => p.slug === slug) ?? null
}

export function findCapability(product: ProductNode, slug: string): CapabilityNode | null {
  const walk = (nodes: CapabilityNode[]): CapabilityNode | null => {
    for (const n of nodes) {
      if (n.slug === slug) return n
      const hit = walk(n.children)
      if (hit) return hit
    }
    return null
  }
  return walk(product.capabilities)
}

/** The slug path from the root to `slug`, for breadcrumbs. */
export function capabilityTrail(product: ProductNode, slug: string): CapabilityNode[] {
  const walk = (nodes: CapabilityNode[], trail: CapabilityNode[]): CapabilityNode[] | null => {
    for (const n of nodes) {
      const next = [...trail, n]
      if (n.slug === slug) return next
      const hit = walk(n.children, next)
      if (hit) return hit
    }
    return null
  }
  return walk(product.capabilities, []) ?? []
}
```

`web/src/features/roadmap/InitiativePage.tsx`:

```tsx
import { useParams } from 'react-router-dom'
import { useInitiative } from '@/api/hooks'
import { PageHeader } from '@/ui/PageHeader'
import { PageContent } from '@/ui/PageContent'
import { EmptyState } from '@/ui/EmptyState'
import { Breadcrumb } from '@/ui/Breadcrumb'
import { ROADMAP } from '@/features/products/vocab'
import { productPath, roadmapPath } from './roadmapNavState'

export function InitiativePage() {
  const { initiativeSlug = '' } = useParams()
  const { data: initiative, isLoading } = useInitiative(initiativeSlug || null)
  const title = initiative?.title ?? initiativeSlug
  const crumbs = [
    { label: ROADMAP.label, to: roadmapPath('initiatives') },
    ...(initiative?.projectSlug ? [{ label: initiative.projectSlug, to: productPath(initiative.projectSlug) }] : []),
    { label: title },
  ]
  return (
    <div className="flex h-full min-h-0 flex-col" data-page-variant="standard">
      <PageHeader title={title} meta={<Breadcrumb items={crumbs} />} />
      <PageContent busy={isLoading}>
        <EmptyState title="Loading…" />
      </PageContent>
    </div>
  )
}
```

- [ ] **Step 9: `routes.tsx`**

Remove the `InitiativesTab` / `ProjectsTab` imports and the `InitiativesPage` / `ProjectsPage` components. Import `RoadmapPage`, `InitiativePage`, and from `roadmapNavState`: `PRODUCT_ROUTE`, `CAPABILITY_ROUTE`, `INITIATIVE_ROUTE`, `INITIATIVES_PATH`, `productPath`, `roadmapPath`. Replace the two rows with:

```tsx
  { path: NAV_BY_ID.roadmap.path, element: <RoadmapPage /> },
  { path: PRODUCT_ROUTE, element: <RoadmapPage /> },
  { path: CAPABILITY_ROUTE, element: <RoadmapPage /> },
  { path: INITIATIVE_ROUTE, element: <InitiativePage /> },
```

and in the legacy block:

```tsx
  { path: '/projects', element: <Navigate to={NAV_BY_ID.roadmap.path} replace /> },
  { path: '/projects/:slug', element: <RedirectProject /> },
  { path: INITIATIVES_PATH, element: <Navigate to={roadmapPath('initiatives')} replace /> },
```

with

```tsx
function RedirectProject() {
  const { slug } = useParams()
  return <Navigate to={slug ? productPath(slug) : NAV_BY_ID.roadmap.path} replace />
}
```

- [ ] **Step 10: Delete the replaced pages and fix the survivors' classes**

```bash
git rm web/src/projects/ProjectsTab.tsx web/src/projects/__tests__/ProjectsTab.test.tsx web/src/projects/projects.css \
       web/src/initiatives/InitiativesTab.tsx web/src/initiatives/__tests__/InitiativesTab.test.tsx web/src/initiatives/initiatives.css
grep -rn "projects.css\|initiatives.css\|pm-error\|pm-muted\|init-error\|init-empty\|incident-" web/src --include=*.tsx --include=*.ts | grep -v __tests__
```

Replace each remaining `className="pm-…"` / `"init-…"` / `"incident-…"` in `OnboardingWizard.tsx`, `DependencyPanel.tsx`, `IncidentQueue.tsx` with token classes (`text-sm text-danger` for errors, `text-sm text-ink-muted` for muted copy, `flex flex-wrap gap-2` for action rows, `rounded-md border border-edge bg-surface p-3` for panels, `font-medium text-ink` for headings). Their tests assert behaviour, not classes; run them after.

- [ ] **Step 11: Full web gate**

Run: `(cd web && npm run typecheck && npm run lint && npm run test && npm run build)`
Expected: green. Expect fallout in tests that mock `@/api/hooks` without `useRoadmapMap` (the shell now calls it): add `useRoadmapMap: () => ({ data: undefined, isLoading: false, isError: false })` to those mocks — search `grep -rln "vi.mock('@/api/hooks'" web/src` and fix each that renders `AppShell`/`renderApp`.

- [ ] **Step 12: Commit**

```bash
printf 'feat(roadmap): one Roadmap section replaces Products and Initiatives — nav, routes, list column, page skeletons\n\nFinish-line spec §4.1–§4.2 / roadmap spec §5. /projects* and /initiatives redirect.\nDeletes ProjectsTab, InitiativesTab and their css.\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c6.txt
git add -A web/src && git commit -F /tmp/c6.txt
```

---

### Task 7: Shared detail plumbing — tab URL state, scoped `IncidentQueue`, lookup tests

**Files:**
- Create: `web/src/features/roadmap/useTabParam.ts`, `useTabParam.test.tsx`
- Create: `web/src/features/roadmap/lookup.test.ts`
- Modify: `web/src/initiatives/IncidentQueue.tsx`, `web/src/initiatives/__tests__/IncidentQueue.test.tsx`

**Interfaces:**
- Produces: `useTabParam<T extends string>(allowed: readonly T[], fallback: T): [T, (t: T) => void]` — reads `?tab=`, unknown values fall back, setter uses `replace` (tabs are URL state per the page-migration contract, and a tab change is not a history entry).
- Produces: `IncidentQueue({ repos?: readonly string[]; title?: string })` — when `repos` is given, only incidents whose `repo` is in it are shown; `title` defaults to "Control-band incidents".

- [ ] **Step 1: Failing tests**

`useTabParam.test.tsx`:

```tsx
import { describe, it, expect } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { useTabParam } from './useTabParam'

const TABS = ['overview', 'issues'] as const
function wrap(initial: string) {
  return ({ children }: { children: React.ReactNode }) => <MemoryRouter initialEntries={[initial]}>{children}</MemoryRouter>
}

describe('useTabParam', () => {
  it('reads ?tab= and falls back on unknown', () => {
    expect(renderHook(() => useTabParam(TABS, 'overview'), { wrapper: wrap('/x?tab=issues') }).result.current[0]).toBe('issues')
    expect(renderHook(() => useTabParam(TABS, 'overview'), { wrapper: wrap('/x?tab=nope') }).result.current[0]).toBe('overview')
  })
  it('sets the param with replace and keeps other params', () => {
    const { result } = renderHook(() => ({ tab: useTabParam(TABS, 'overview'), loc: useLocation() }), { wrapper: wrap('/x?scope=map') })
    act(() => result.current.tab[1]('issues'))
    expect(result.current.tab[0]).toBe('issues')
    expect(result.current.loc.search).toBe('?scope=map&tab=issues')
  })
})
```

`lookup.test.ts`:

```ts
// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { MAP } from './__fixtures__/map'
import { findProduct, findCapability, capabilityTrail } from './lookup'

describe('lookup', () => {
  it('finds products and nested capabilities', () => {
    const p = findProduct(MAP, 'kindash')!
    expect(p.name).toBe('KinDash')
    expect(findProduct(MAP, 'nope')).toBeNull()
    expect(findCapability(p, 'task-views')?.title).toBe('Task views')
    expect(findCapability(p, 'nope')).toBeNull()
    expect(capabilityTrail(p, 'task-views').map((c) => c.slug)).toEqual(['tasks', 'task-views'])
    expect(capabilityTrail(p, 'nope')).toEqual([])
  })
})
```

Add to `IncidentQueue.test.tsx` (it already mocks `useIncidents`; extend the fixture so two incidents have `repo: 'a/b'` and `repo: 'c/d'`):

```tsx
  it('scopes to the given repos and takes a title', () => {
    render(<IncidentQueue repos={['a/b']} title="Needs you" />)
    expect(screen.getByRole('region', { name: 'Needs you' })).toBeInTheDocument()
    expect(screen.getAllByRole('listitem')).toHaveLength(1)
  })
```

- [ ] **Step 2: Run to fail**

Run: `(cd web && npx vitest run src/features/roadmap/useTabParam.test.tsx src/features/roadmap/lookup.test.ts src/initiatives/__tests__/IncidentQueue.test.tsx)`
Expected: FAIL.

- [ ] **Step 3: Implement**

`useTabParam.ts`:

```ts
import { useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'

/** `?tab=` as state (page-migration §2: tabs are URL state). Replace, not push. */
export function useTabParam<T extends string>(allowed: readonly T[], fallback: T): [T, (t: T) => void] {
  const [params, setParams] = useSearchParams()
  const raw = params.get('tab')
  const tab = (allowed as readonly string[]).includes(raw ?? '') ? (raw as T) : fallback
  const setTab = useCallback((t: T) => {
    setParams((prev) => { const next = new URLSearchParams(prev); next.set('tab', t); return next }, { replace: true })
  }, [setParams])
  return [tab, setTab]
}
```

`IncidentQueue.tsx`: change the signature to `export function IncidentQueue({ repos, title = 'Control-band incidents' }: { repos?: readonly string[]; title?: string } = {})`, filter `const all = (incidents ?? []).filter((i) => !repos || (i.repo !== null && repos.includes(i.repo)))`, use `title` in the `<section aria-label>` and the `<h3>`.

- [ ] **Step 4: Run, lint, commit**

Run: `(cd web && npx vitest run src/features/roadmap src/initiatives && npm run lint)`

```bash
printf 'feat(roadmap): tab URL state, repo-scoped IncidentQueue, lookup helpers\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c7.txt
git add web/src/features/roadmap web/src/initiatives && git commit -F /tmp/c7.txt
```

---

### Task 8: Portfolio detail — Needs you · Ranked · All initiatives

**Files:**
- Create: `web/src/features/roadmap/detail/StatTiles.tsx`, `NeedsYouList.tsx`, `RankedList.tsx`, `InitiativeList.tsx`, `PortfolioDetail.tsx`, `PortfolioDetail.test.tsx`
- Modify: `web/src/features/roadmap/RoadmapPage.tsx` (render `PortfolioDetail` when nothing is open)

**Interfaces:**
- `StatTiles({ tiles: { label: string; value: number; tone?: Tone }[] })`
- `NeedsYouList({ products: ProductNode[]; repos?: readonly string[] })` — rows for every initiative with tier `needs-you` (title, product, reason or status), pseudo counts as links to the inbox, then `<IncidentQueue repos title="Incidents" />`.
- `RankedList({ productSlug?: string })` — `useQueue()`, filtered by `entry.product === productSlug` when given; each row: score, `#number title` link (external), repo.
- `InitiativeList({ initiatives: InitiativeNode[] })` — `InitiativeCard` rows (depth 0) navigating to `initiativePath`.
- `PortfolioDetail({ map: RoadmapMap })`.

- [ ] **Step 1: Failing test** — `PortfolioDetail.test.tsx`

```tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { MAP } from '../__fixtures__/map'
import { PortfolioDetail } from './PortfolioDetail'

vi.mock('@/api/hooks', () => ({
  useQueue: () => ({ data: { entries: [
    { ref: 'cairnea/KinDash#1', repo: 'cairnea/KinDash', number: 1, product: 'kindash', title: 'Top', url: 'https://x/1', score: 9.5 },
    { ref: 'steiner385/viberpit#2', repo: 'steiner385/viberpit', number: 2, product: 'viberpit', title: 'Second', url: 'https://x/2', score: 4 },
  ], skipped: 0, generatedAt: 'now' }, isLoading: false }),
  useIncidents: () => ({ data: [], isError: false }),
  useIncidentAction: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
}))

function mount(initial = '/roadmap') {
  render(<MemoryRouter initialEntries={[initial]}><PortfolioDetail map={MAP} /></MemoryRouter>)
}

describe('PortfolioDetail', () => {
  it('shows stat tiles and the Needs-you tab by default', () => {
    mount()
    expect(screen.getByText('Products')).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: /Needs you/ })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByText('No home yet')).toBeInTheDocument()          // needs-you initiative
    expect(screen.getByText('Ready to advance')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /2 awaiting triage/ })).toHaveAttribute('href', '/triage?projectSlug=kindash')
  })
  it('Ranked tab lists the queue across products in score order', () => {
    mount('/roadmap?tab=ranked')
    const rows = screen.getAllByRole('listitem')
    expect(rows[0]).toHaveTextContent('Top')
    expect(rows[1]).toHaveTextContent('Second')
  })
  it('All initiatives tab lists every initiative', () => {
    mount()
    fireEvent.click(screen.getByRole('tab', { name: /All initiatives/ }))
    expect(screen.getByRole('button', { name: 'Open initiative Use-case-first task views' })).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run to fail**, then implement.

`StatTiles.tsx`:

```tsx
import type { Tone } from '@/ui/Badge'
import { cn } from '@/lib/utils'

const TONE_TEXT: Record<Tone, string> = {
  success: 'text-success', warning: 'text-warning', danger: 'text-danger',
  info: 'text-info', neutral: 'text-ink', accent: 'text-accent',
}

export function StatTiles({ tiles }: { tiles: { label: string; value: number; tone?: Tone }[] }) {
  return (
    <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4">
      {tiles.map((t) => (
        <div key={t.label} className="rounded-md border border-edge bg-surface p-3">
          <dt className="text-xs text-ink-muted">{t.label}</dt>
          <dd className={cn('text-2xl font-semibold tabular-nums', TONE_TEXT[t.tone ?? 'neutral'])}>{t.value}</dd>
        </div>
      ))}
    </dl>
  )
}
```

`NeedsYouList.tsx`:

```tsx
import { Link } from 'react-router-dom'
import type { ProductNode } from '@/api/roadmap'
import { IncidentQueue } from '@/initiatives/IncidentQueue'
import { EmptyState } from '@/ui/EmptyState'
import { NAV_BY_ID } from '@/shell/navDefs'
import { initiativeStatusLabel, reasonLabel } from '@/features/products/vocab'
import { initiativePath } from '../roadmapNavState'

export function NeedsYouList({ products, repos }: { products: ProductNode[]; repos?: readonly string[] }) {
  const initiatives = products.flatMap((p) => p.initiatives.filter((i) => i.tier === 'needs-you').map((i) => ({ product: p, initiative: i })))
  const pseudo = products.filter((p) => p.counts.awaitingTriage > 0 || p.counts.needsHome > 0)
  const empty = initiatives.length === 0 && pseudo.length === 0
  return (
    <div className="flex flex-col gap-3">
      {empty && <EmptyState title="Nothing needs you" description="Every initiative and proposal is moving on its own." />}
      {initiatives.length > 0 && (
        <ul className="flex flex-col gap-1" aria-label="Initiatives that need you">
          {initiatives.map(({ product, initiative }) => (
            <li key={initiative.slug} className="rounded-md border border-edge bg-surface p-3">
              <Link to={initiativePath(initiative.slug)} className="font-medium text-ink hover:underline">{initiative.title ?? initiative.slug}</Link>
              <div className="text-xs text-ink-muted">
                {product.name ?? product.slug} · <span className="text-warning">{reasonLabel(initiative.reason) || initiativeStatusLabel(initiative.status)}</span>
              </div>
            </li>
          ))}
        </ul>
      )}
      {pseudo.length > 0 && (
        <ul className="flex flex-col gap-1" aria-label="Proposals awaiting a decision">
          {pseudo.map((p) => (
            <li key={p.slug} className="rounded-md border border-edge bg-surface p-3 text-sm">
              <span className="font-medium text-ink">{p.name ?? p.slug}</span>{' · '}
              {p.counts.awaitingTriage > 0 && (
                <Link className="text-accent hover:underline" to={`${NAV_BY_ID.triage.path}?projectSlug=${encodeURIComponent(p.slug)}`}>
                  {p.counts.awaitingTriage} awaiting triage
                </Link>
              )}
              {p.counts.awaitingTriage > 0 && p.counts.needsHome > 0 && ' · '}
              {p.counts.needsHome > 0 && <span className="text-ink-muted">{p.counts.needsHome} need a home</span>}
            </li>
          ))}
        </ul>
      )}
      <IncidentQueue repos={repos} title="Incidents" />
    </div>
  )
}
```

`RankedList.tsx`:

```tsx
import { useQueue } from '@/api/hooks'
import { EmptyState } from '@/ui/EmptyState'
import { Skeleton } from '@/ui/Skeleton'

export function RankedList({ productSlug }: { productSlug?: string }) {
  const { data, isLoading } = useQueue()
  const entries = (data?.entries ?? []).filter((e) => !productSlug || e.product === productSlug)
  if (isLoading && !data) return <Skeleton lines={5} />
  if (entries.length === 0) return <EmptyState title="Nothing ranked yet" description="Scores come from triage. Accept a few proposals and the queue fills in." />
  return (
    <ol className="flex flex-col gap-1" aria-label="Ranked queue">
      {entries.map((e, i) => (
        <li key={e.ref} className="flex items-baseline gap-3 rounded-md border border-edge bg-surface p-3 text-sm">
          <span className="w-6 shrink-0 text-right tabular-nums text-ink-faint">{i + 1}</span>
          <span className="w-10 shrink-0 tabular-nums text-ink-muted">{e.score?.toFixed(1) ?? '—'}</span>
          <span className="min-w-0 flex-1 truncate">
            <a href={e.url ?? undefined} target="_blank" rel="noreferrer" className="text-ink hover:underline">#{e.number} {e.title ?? e.ref}</a>
            <span className="ml-2 text-xs text-ink-muted">{e.repo}</span>
          </span>
        </li>
      ))}
    </ol>
  )
}
```

`InitiativeList.tsx`:

```tsx
import { useNavigate } from 'react-router-dom'
import type { InitiativeNode } from '@/api/roadmap'
import { EmptyState } from '@/ui/EmptyState'
import { InitiativeCard } from '../rows/InitiativeCard'
import { initiativeKey } from '../treeRows'
import { initiativePath } from '../roadmapNavState'

export function InitiativeList({ initiatives }: { initiatives: InitiativeNode[] }) {
  const navigate = useNavigate()
  if (initiatives.length === 0) return <EmptyState title="No initiatives" />
  return (
    <div className="flex flex-col gap-1">
      {initiatives.map((i) => (
        <InitiativeCard key={i.slug} rowKey={initiativeKey(i.slug)} depth={0} initiative={i} selected={false}
                        onOpen={() => navigate(initiativePath(i.slug))} />
      ))}
    </div>
  )
}
```

`PortfolioDetail.tsx`:

```tsx
import type { RoadmapMap } from '@/api/roadmap'
import { Tabs } from '@/ui/Tabs'
import { PRODUCT } from '@/features/products/vocab'
import { useTabParam } from '../useTabParam'
import { StatTiles } from './StatTiles'
import { NeedsYouList } from './NeedsYouList'
import { RankedList } from './RankedList'
import { InitiativeList } from './InitiativeList'

const TABS = ['needs-you', 'ranked', 'initiatives'] as const

export function PortfolioDetail({ map }: { map: RoadmapMap }) {
  const [tab, setTab] = useTabParam(TABS, 'needs-you')
  const products = map.products
  const sum = (k: 'inFlight' | 'blocked' | 'needsYou') => products.reduce((n, p) => n + p.counts[k], 0)
  const needsYou = sum('needsYou')
  const initiatives = products.flatMap((p) => p.initiatives)
  return (
    <div className="flex flex-col gap-4">
      <StatTiles tiles={[
        { label: PRODUCT.plural, value: products.length },
        { label: 'In flight', value: sum('inFlight'), tone: 'success' },
        { label: 'Blocked', value: sum('blocked'), tone: 'danger' },
        { label: 'Need you', value: needsYou, tone: 'warning' },
      ]} />
      <Tabs label="Portfolio" value={tab} onChange={(id) => setTab(id as typeof TABS[number])} panelId={(id) => `portfolio-panel-${id}`}
            items={[
              { id: 'needs-you', label: 'Needs you', count: needsYou, attention: needsYou > 0 },
              { id: 'ranked', label: 'Ranked' },
              { id: 'initiatives', label: 'All initiatives', count: initiatives.length },
            ]} />
      <div role="tabpanel" id={`portfolio-panel-${tab}`} aria-labelledby={`tab-${tab}`}>
        {tab === 'needs-you' && <NeedsYouList products={products} />}
        {tab === 'ranked' && <RankedList />}
        {tab === 'initiatives' && <InitiativeList initiatives={initiatives} />}
      </div>
    </div>
  )
}
```

`RoadmapPage.tsx`: replace the `EmptyState` with `{!item && map && <PortfolioDetail map={map} />}` and keep an `EmptyState` only for `item && !product` ("Not found" with a link back to `/roadmap`).

- [ ] **Step 3: Run tests, full web gate, commit**

```bash
printf 'feat(roadmap): portfolio detail — Needs you, Ranked, All initiatives\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c8.txt
git add web/src/features/roadmap && git commit -F /tmp/c8.txt
```

---

### Task 9: Product detail — Overview · In flight · Needs you · Ranked · Dependencies

**Files:**
- Create: `web/src/features/roadmap/detail/ProductDetail.tsx`, `ProductDetail.test.tsx`, `NewInitiativeDialog.tsx`
- Modify: `web/src/features/roadmap/RoadmapPage.tsx` (render `ProductDetail` for `kind === 'product'`, header actions)

**Interfaces:**
- `ProductDetail({ product: ProductNode })`; `NewInitiativeDialog({ product: ProductNode; open: boolean; onClose: () => void })` — fields: title, slug (auto from title, editable), repo (select over the project's repos from `useProject`), primary capability (select over the tree, optional); submits `useCreateInitiative({ slug, title, repo, origin: 'human', primaryCapabilityId, capabilityIds: [primaryCapabilityId] })`, navigates to `initiativePath(slug)` on success.
- `RoadmapPage` header actions for a product: `Sync` (`useSyncProject`), `+ Initiative` (opens the dialog). Header `meta` is the breadcrumb plus `StatusPill` of the onboarding state when not `live`.

- [ ] **Step 1: Failing test** — `ProductDetail.test.tsx`

```tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { MAP } from '../__fixtures__/map'
import { ProductDetail } from './ProductDetail'

vi.mock('@/api/hooks', () => ({
  useQueue: () => ({ data: { entries: [
    { ref: 'cairnea/KinDash#1', repo: 'cairnea/KinDash', number: 1, product: 'kindash', title: 'Top', url: 'https://x/1', score: 9.5 },
    { ref: 'steiner385/viberpit#2', repo: 'steiner385/viberpit', number: 2, product: 'viberpit', title: 'Other', url: 'https://x/2', score: 4 },
  ], skipped: 0, generatedAt: 'now' }, isLoading: false }),
  useIncidents: () => ({ data: [{ id: 'i1', repo: 'cairnea/KinDash', status: 'open', metric: 'ci_fail', summary: 'CI failing', key: null, sigma: 3, occurrences: 1, value: 1, baselineMean: 0, baselineStddev: 0, baselineN: 5, observedAt: null, rule: 'we1', dismissedReason: null, scheduledFor: null, initiativeSlug: null, sessionId: null, evidence: [], source: null, side: null, tier: null, response: null, createdAt: null, updatedAt: null }], isError: false }),
  useIncidentAction: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useProject: () => ({ data: { id: 'p', slug: 'kindash', name: 'KinDash', description: 'Family dashboard', status: null, portfolioTags: [], onboardingState: 'live', createdAt: null, updatedAt: null, repos: [{ id: 'r1', repo: 'cairnea/KinDash', isPrimary: true, backfillDone: true, lastSyncedAt: null }], edges: [], featureCount: 3, workItemCount: 40 }, isLoading: false }),
  useOnboarding: () => ({ data: { state: 'live', action: { type: 'none' }, counts: { repoCount: 1, backfillDoneCount: 1, featureCount: 3, openUnclassifiedCount: 0, pendingProposalCount: 2 }, mapPr: null }, isLoading: false }),
}))
vi.mock('@/api/pm', async (orig) => ({ ...(await orig<typeof import('@/api/pm')>()), useProjectDeps: () => ({ data: { project: null, edges: [], drift: { proseOnly: [], unresolvedTargets: [], missingFromProject: [], checklistEpics: [] } }, isLoading: false, isError: false }) }))

const product = MAP.products[0]!
const mount = (initial = '/roadmap/kindash') =>
  render(<MemoryRouter initialEntries={[initial]}><ProductDetail product={product} /></MemoryRouter>)

describe('ProductDetail', () => {
  it('Overview: tiles, description, repos, in-flight initiatives with tracks', () => {
    mount()
    expect(screen.getByText('Family dashboard')).toBeInTheDocument()
    expect(screen.getByText('cairnea/KinDash')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Use-case-first task views' })).toHaveAttribute('href', '/initiatives/views')
  })
  it('Needs you: product-scoped initiatives and incidents', () => {
    mount('/roadmap/kindash?tab=needs-you')
    expect(screen.getByText('No home yet')).toBeInTheDocument()
    expect(screen.getByText('CI failing')).toBeInTheDocument()
  })
  it('Ranked: only this product', () => {
    mount('/roadmap/kindash?tab=ranked')
    expect(screen.getByText(/Top/)).toBeInTheDocument()
    expect(screen.queryByText(/Other/)).toBeNull()
  })
  it('Dependencies tab renders the panel', () => {
    mount()
    fireEvent.click(screen.getByRole('tab', { name: /Dependencies/ }))
    expect(screen.getByRole('tabpanel')).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Implement `ProductDetail.tsx`**

```tsx
import { Link } from 'react-router-dom'
import type { ProductNode } from '@/api/roadmap'
import { useProject } from '@/api/hooks'
import { Tabs } from '@/ui/Tabs'
import { Skeleton } from '@/ui/Skeleton'
import { InitiativeTrack } from '@/initiatives/InitiativeTrack'
import { OnboardingWizard } from '@/projects/OnboardingWizard'
import { DependencyPanel } from '@/projects/DependencyPanel'
import { CAPABILITY, INITIATIVE, ISSUE } from '@/features/products/vocab'
import { useTabParam } from '../useTabParam'
import { initiativePath } from '../roadmapNavState'
import { StatTiles } from './StatTiles'
import { NeedsYouList } from './NeedsYouList'
import { RankedList } from './RankedList'
import { InitiativeList } from './InitiativeList'

const TABS = ['overview', 'in-flight', 'needs-you', 'ranked', 'dependencies'] as const

export function ProductDetail({ product }: { product: ProductNode }) {
  const [tab, setTab] = useTabParam(TABS, 'overview')
  const { data: project, isLoading } = useProject(product.slug)
  const repos = project?.repos.map((r) => r.repo).filter((r): r is string => !!r) ?? []
  const inFlight = product.initiatives.filter((i) => i.tier === 'in-flight' || i.tier === 'blocked' || i.tier === 'needs-you')
  const c = product.counts
  return (
    <div className="flex flex-col gap-4">
      <StatTiles tiles={[
        { label: CAPABILITY.plural, value: c.capabilities },
        { label: 'In flight', value: c.inFlight, tone: 'success' },
        { label: 'Blocked', value: c.blocked, tone: 'danger' },
        { label: 'Need you', value: c.needsYou, tone: 'warning' },
      ]} />
      <Tabs label={product.name ?? product.slug} value={tab} onChange={(id) => setTab(id as typeof TABS[number])} panelId={(id) => `product-panel-${id}`}
            items={[
              { id: 'overview', label: 'Overview' },
              { id: 'in-flight', label: 'In flight', count: inFlight.length },
              { id: 'needs-you', label: 'Needs you', count: c.needsYou, attention: c.needsYou > 0 },
              { id: 'ranked', label: 'Ranked' },
              { id: 'dependencies', label: 'Dependencies' },
            ]} />
      <div role="tabpanel" id={`product-panel-${tab}`} aria-labelledby={`tab-${tab}`} className="flex flex-col gap-4">
        {tab === 'overview' && (
          <>
            {isLoading && !project ? <Skeleton lines={3} /> : (
              <section className="flex flex-col gap-2 text-sm">
                {project?.description && <p className="text-ink">{project.description}</p>}
                <ul className="flex flex-wrap gap-2" aria-label="Repositories">
                  {project?.repos.map((r) => (
                    <li key={r.id} className="rounded-pill border border-edge px-2 py-0.5 text-xs text-ink-muted">
                      {r.repo}{r.isPrimary ? ' · primary' : ''}{!r.backfillDone ? ' · backfilling' : ''}
                    </li>
                  ))}
                </ul>
                <p className="text-xs text-ink-muted">{project?.workItemCount ?? 0} {ISSUE.plural.toLowerCase()} mirrored</p>
              </section>
            )}
            <OnboardingWizard slug={product.slug} />
            <section aria-label={`${INITIATIVE.plural} in flight`} className="flex flex-col gap-3">
              {inFlight.length === 0 && <p className="text-sm text-ink-muted">Nothing in flight.</p>}
              {inFlight.map((i) => (
                <div key={i.slug} className="rounded-md border border-edge bg-surface p-3 prdash-root">
                  <Link to={initiativePath(i.slug)} className="font-medium text-ink hover:underline">{i.title ?? i.slug}</Link>
                  <InitiativeTrack stage={i.stage} status={i.status} epic={i.epic} />
                </div>
              ))}
            </section>
          </>
        )}
        {tab === 'in-flight' && <InitiativeList initiatives={inFlight} />}
        {tab === 'needs-you' && <NeedsYouList products={[product]} repos={repos} />}
        {tab === 'ranked' && <RankedList productSlug={product.slug} />}
        {tab === 'dependencies' && <DependencyPanel slug={product.slug} />}
      </div>
    </div>
  )
}
```

(`prdash-root` scopes `plotroom.css` for `InitiativeTrack`; it goes when finish-line row 5 moves the track onto tokens.)

- [ ] **Step 3: `NewInitiativeDialog.tsx`**

```tsx
import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import type { CapabilityNode, ProductNode } from '@/api/roadmap'
import { useCreateInitiative, useProject } from '@/api/hooks'
import { extractMutationErrorMessage } from '@/api/queryClient'
import { Dialog } from '@/ui/Dialog'
import { Field } from '@/ui/Field'
import { Input } from '@/ui/Input'
import { Select } from '@/ui/Select'
import { Button } from '@/ui/Button'
import { CAPABILITY, INITIATIVE } from '@/features/products/vocab'
import { initiativePath } from '../roadmapNavState'

const slugify = (s: string) => s.toLowerCase().trim().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, '')

function flatten(nodes: CapabilityNode[], depth = 0): { node: CapabilityNode; depth: number }[] {
  return nodes.flatMap((n) => [{ node: n, depth }, ...flatten(n.children, depth + 1)])
}

export function NewInitiativeDialog({ product, open, onClose, defaultCapabilityId }: {
  product: ProductNode; open: boolean; onClose: () => void; defaultCapabilityId?: string
}) {
  const navigate = useNavigate()
  const create = useCreateInitiative()
  const { data: project } = useProject(product.slug)
  const repos = project?.repos.map((r) => r.repo).filter((r): r is string => !!r) ?? []
  const caps = useMemo(() => flatten(product.capabilities), [product])
  const [title, setTitle] = useState('')
  const [slug, setSlug] = useState('')
  const [slugTouched, setSlugTouched] = useState(false)
  const [repo, setRepo] = useState('')
  const [capId, setCapId] = useState(defaultCapabilityId ?? '')
  const effectiveRepo = repo || repos[0] || ''
  const effectiveSlug = slugTouched ? slug : slugify(title)
  const canSubmit = title.trim() !== '' && effectiveSlug !== '' && effectiveRepo !== '' && !create.isPending

  return (
    <Dialog open={open} onClose={onClose} title={INITIATIVE.new} size="md">
      <form className="flex flex-col gap-3" onSubmit={(e) => {
        e.preventDefault()
        if (!canSubmit) return
        create.mutate(
          { slug: effectiveSlug, title: title.trim(), repo: effectiveRepo, origin: 'human',
            ...(capId ? { primaryCapabilityId: capId, capabilityIds: [capId] } : {}) },
          { onSuccess: (created) => { onClose(); navigate(initiativePath(created.slug)) } },
        )
      }}>
        <Field label="Title" htmlFor="ni-title" required>
          <Input id="ni-title" value={title} onChange={(e) => setTitle(e.target.value)} autoFocus />
        </Field>
        <Field label="Slug" htmlFor="ni-slug" help="Used in URLs and branch names">
          <Input id="ni-slug" value={effectiveSlug} onChange={(e) => { setSlugTouched(true); setSlug(slugify(e.target.value)) }} />
        </Field>
        <Field label="Repository" htmlFor="ni-repo" required>
          <Select id="ni-repo" value={effectiveRepo} onChange={(e) => setRepo(e.target.value)}>
            {repos.map((r) => <option key={r} value={r}>{r}</option>)}
          </Select>
        </Field>
        <Field label={`Primary ${CAPABILITY.singular.toLowerCase()}`} htmlFor="ni-cap">
          <Select id="ni-cap" value={capId} onChange={(e) => setCapId(e.target.value)}>
            <option value="">— none yet —</option>
            {caps.map(({ node, depth }) => <option key={node.id} value={node.id}>{'  '.repeat(depth)}{node.title ?? node.slug}</option>)}
          </Select>
        </Field>
        {create.isError && <p role="alert" className="text-sm text-danger">{extractMutationErrorMessage(create.error, 'Could not create initiative')}</p>}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit" disabled={!canSubmit}>{create.isPending ? 'Creating…' : 'Create'}</Button>
        </div>
      </form>
    </Dialog>
  )
}
```

Check `Field`'s real prop names (`label`, `htmlFor`, `help`, `error`, `required`) and `Button`'s `variant` values in `web/src/ui/Button.tsx` before using them; adjust to the real API.

- [ ] **Step 4: `RoadmapPage.tsx` — product branch and header actions**

Add state `const [creating, setCreating] = useState(false)`; for `item?.kind === 'product' && product`:
`actions={<><Button size="sm" variant="ghost" onClick={() => sync.mutate(product.slug)} disabled={sync.isPending}>Sync</Button><Button size="sm" onClick={() => setCreating(true)}>+ {INITIATIVE.singular}</Button></>}` (import `useSyncProject`), body `<ProductDetail product={product} />`, and `<NewInitiativeDialog product={product} open={creating} onClose={() => setCreating(false)} />` after `PageContent`.

- [ ] **Step 5: Run tests + full web gate + commit**

```bash
printf 'feat(roadmap): product detail — overview, in flight, needs you, ranked, dependencies; new-initiative dialog\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c9.txt
git add web/src/features/roadmap && git commit -F /tmp/c9.txt
```

---

### Task 10: Capability detail — Overview · Sub-capabilities · In flight · Issues · Needs you

**Files:**
- Create: `web/src/features/roadmap/detail/CapabilityDetail.tsx`, `CapabilityDetail.test.tsx`, `LeafList.tsx`
- Modify: `web/src/features/roadmap/RoadmapPage.tsx` (capability branch; breadcrumb uses `capabilityTrail`)

**Interfaces:**
- `LeafList({ scope: LeavesScope })` — `useLeaves(scope)`; rows grouped Open / Closed; each row: tier dot, `#number title` external link, score, blockers count, live-session pill when `sessionStatus` is set.
- `CapabilityDetail({ product: ProductNode; capability: CapabilityNode; onNewInitiative: () => void })`.

- [ ] **Step 1: Failing test** — `CapabilityDetail.test.tsx`

```tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { MAP } from '../__fixtures__/map'
import { findCapability } from '../lookup'
import { CapabilityDetail } from './CapabilityDetail'

vi.mock('@/api/hooks', () => ({
  useLeaves: () => ({ data: [
    { id: 'w1', ref: 'cairnea/KinDash#7', repo: 'cairnea/KinDash', number: 7, kind: 'issue', title: 'Bulk edit', url: 'https://x/7', state: 'open', triageState: 'classified', score: 6.5, reach: 3, impact: 3, effort: 2, confidence: 80, tier: 'in-flight', blockedBy: [], parentRef: null, sessionStatus: 'in_progress', ghCreatedAt: null },
    { id: 'w2', ref: 'cairnea/KinDash#3', repo: 'cairnea/KinDash', number: 3, kind: 'issue', title: 'Old', url: 'https://x/3', state: 'closed', triageState: 'classified', score: null, reach: null, impact: null, effort: null, confidence: null, tier: 'live', blockedBy: [], parentRef: null, sessionStatus: null, ghCreatedAt: null },
  ], isLoading: false }),
  useIncidents: () => ({ data: [], isError: false }),
  useIncidentAction: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
}))

const product = MAP.products[0]!
const tasks = findCapability(product, 'tasks')!

describe('CapabilityDetail', () => {
  it('Overview shows state, doc link and sub-capabilities count', () => {
    render(<MemoryRouter><CapabilityDetail product={product} capability={tasks} onNewInitiative={() => {}} /></MemoryRouter>)
    expect(screen.getByText('Live')).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: /Sub-capabilities/ })).toHaveTextContent('1')
  })
  it('Issues tab lists open leaves first with a live-session marker', () => {
    render(<MemoryRouter initialEntries={['/x?tab=issues']}><CapabilityDetail product={product} capability={tasks} onNewInitiative={() => {}} /></MemoryRouter>)
    const items = screen.getAllByRole('listitem')
    expect(items[0]).toHaveTextContent('Bulk edit')
    expect(items[0]).toHaveTextContent('session')
    expect(screen.getByRole('heading', { name: /Closed/ })).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Implement `LeafList.tsx`**

```tsx
import type { Leaf, LeavesScope } from '@/api/roadmap'
import { useLeaves } from '@/api/hooks'
import { Skeleton } from '@/ui/Skeleton'
import { EmptyState } from '@/ui/EmptyState'
import { Badge } from '@/ui/Badge'
import { cn } from '@/lib/utils'
import { ISSUE } from '@/features/products/vocab'
import { TIER_ACCENT, TIER_TONE, tierLabel } from '../roadmapTier'

function LeafRow({ leaf }: { leaf: Leaf }) {
  const a = TIER_ACCENT[leaf.tier]
  return (
    <li className="flex items-center gap-3 rounded-md border border-edge bg-surface p-2 text-sm">
      <span aria-hidden="true" className={cn('inline-block size-2 shrink-0 rounded-full', a.dot)} />
      <a href={leaf.url ?? undefined} target="_blank" rel="noreferrer" className="min-w-0 flex-1 truncate text-ink hover:underline">
        #{leaf.number} {leaf.title ?? leaf.ref}
      </a>
      {leaf.sessionStatus && <Badge tone="success">session</Badge>}
      {leaf.blockedBy.length > 0 && <Badge tone="danger" count={leaf.blockedBy.length} aria-label={`${leaf.blockedBy.length} blockers`} />}
      <span className="w-10 shrink-0 text-right tabular-nums text-xs text-ink-muted" title={tierLabel(leaf.tier)}>{leaf.score?.toFixed(1) ?? '—'}</span>
      <span className="sr-only">{tierLabel(leaf.tier)}</span>
      <Badge tone={TIER_TONE[leaf.tier]} dot aria-hidden="true" />
    </li>
  )
}

export function LeafList({ scope }: { scope: LeavesScope }) {
  const { data, isLoading } = useLeaves(scope)
  if (isLoading && !data) return <Skeleton lines={5} />
  const leaves = data ?? []
  if (leaves.length === 0) return <EmptyState title={`No ${ISSUE.plural.toLowerCase()} here yet`} />
  const open = leaves.filter((l) => l.state === 'open')
  const closed = leaves.filter((l) => l.state !== 'open')
  return (
    <div className="flex flex-col gap-4">
      <section>
        <h3 className="mb-1 text-xs font-medium uppercase text-ink-muted">Open · {open.length}</h3>
        <ul className="flex flex-col gap-1">{open.map((l) => <LeafRow key={l.id} leaf={l} />)}</ul>
      </section>
      {closed.length > 0 && (
        <section>
          <h3 className="mb-1 text-xs font-medium uppercase text-ink-muted">Closed · {closed.length}</h3>
          <ul className="flex flex-col gap-1">{closed.map((l) => <LeafRow key={l.id} leaf={l} />)}</ul>
        </section>
      )}
    </div>
  )
}
```

- [ ] **Step 3: Implement `CapabilityDetail.tsx`**

```tsx
import { Link } from 'react-router-dom'
import type { CapabilityNode, ProductNode } from '@/api/roadmap'
import { Tabs } from '@/ui/Tabs'
import { Badge } from '@/ui/Badge'
import { Button } from '@/ui/Button'
import { EmptyState } from '@/ui/EmptyState'
import { fileUrl } from '@/lib/githubUrl'
import { CAPABILITY, INITIATIVE, ISSUE } from '@/features/products/vocab'
import { useTabParam } from '../useTabParam'
import { capabilityPath } from '../roadmapNavState'
import { TIER_TONE, tierLabel } from '../roadmapTier'
import { StatTiles } from './StatTiles'
import { NeedsYouList } from './NeedsYouList'
import { InitiativeList } from './InitiativeList'
import { LeafList } from './LeafList'

const TABS = ['overview', 'children', 'in-flight', 'issues', 'needs-you'] as const

const STATE_LABEL: Record<string, string> = { proposed: 'Proposed', discovery: 'Discovery', delivering: 'Delivering', live: 'Live', sunset: 'Sunset' }

export function CapabilityDetail({ product, capability, onNewInitiative, primaryRepo }: {
  product: ProductNode; capability: CapabilityNode; onNewInitiative: () => void; primaryRepo?: string
}) {
  const [tab, setTab] = useTabParam(TABS, 'overview')
  const touching = new Set([...capability.initiatives])
  const mine = product.initiatives.filter((i) => i.primaryCapability === capability.slug || touching.has(i.slug))
  const inFlight = mine.filter((i) => i.tier !== 'live' && i.tier !== 'idle')
  const needsYou = mine.filter((i) => i.tier === 'needs-you')
  const c = capability.counts
  const docHref = primaryRepo && capability.docPath && capability.commitSha ? fileUrl(primaryRepo, capability.commitSha, capability.docPath) : null
  return (
    <div className="flex flex-col gap-4">
      <StatTiles tiles={[
        { label: `Open ${ISSUE.plural.toLowerCase()}`, value: c.openIssues },
        { label: 'In flight', value: c.inFlight, tone: 'success' },
        { label: 'Blocked', value: c.blocked, tone: 'danger' },
        { label: 'Need you', value: c.needsYou, tone: 'warning' },
      ]} />
      <Tabs label={capability.title ?? capability.slug} value={tab} onChange={(id) => setTab(id as typeof TABS[number])} panelId={(id) => `cap-panel-${id}`}
            items={[
              { id: 'overview', label: 'Overview' },
              { id: 'children', label: 'Sub-capabilities', count: capability.children.length },
              { id: 'in-flight', label: 'In flight', count: inFlight.length },
              { id: 'issues', label: ISSUE.plural, count: c.openIssues },
              { id: 'needs-you', label: 'Needs you', count: needsYou.length, attention: needsYou.length > 0 },
            ]} />
      <div role="tabpanel" id={`cap-panel-${tab}`} aria-labelledby={`tab-${tab}`} className="flex flex-col gap-4">
        {tab === 'overview' && (
          <section className="flex flex-col gap-2 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone={capability.state === 'live' ? 'success' : 'info'}>{STATE_LABEL[capability.state ?? ''] ?? capability.state ?? '—'}</Badge>
              <Badge tone={TIER_TONE[capability.tier]}>{tierLabel(capability.tier)}</Badge>
              {capability.prNumber && <Badge tone="info">proposed in PR #{capability.prNumber}</Badge>}
              {docHref && <a href={docHref} target="_blank" rel="noreferrer" className="text-accent hover:underline">feature doc ↗</a>}
            </div>
            {capability.summary ? <p className="text-ink">{capability.summary}</p> : <p className="text-ink-muted">No summary in the feature doc yet.</p>}
            {capability.parentSlug && (
              <p className="text-xs text-ink-muted">Part of <Link className="text-accent hover:underline" to={capabilityPath(product.slug, capability.parentSlug)}>{capability.parentSlug}</Link></p>
            )}
            {capability.links.length > 0 && (
              <ul className="flex flex-wrap gap-2" aria-label="Links">
                {capability.links.map((l) => (
                  <li key={`${l.kind}:${l.projectSlug}:${l.featureSlug}`}>
                    <Link to={capabilityPath(l.projectSlug, l.featureSlug)} className="rounded-pill border border-edge px-2 py-0.5 text-xs text-ink-muted hover:text-ink">
                      {l.kind.replace('_', ' ')} · {l.projectSlug}/{l.featureSlug}
                    </Link>
                  </li>
                ))}
              </ul>
            )}
            <div><Button size="sm" variant="ghost" onClick={onNewInitiative}>+ {INITIATIVE.singular} here</Button></div>
          </section>
        )}
        {tab === 'children' && (capability.children.length === 0
          ? <EmptyState title={`No sub-${CAPABILITY.plural.toLowerCase()}`} />
          : <ul className="flex flex-col gap-1">
              {capability.children.map((ch) => (
                <li key={ch.id} className="rounded-md border border-edge bg-surface p-3 text-sm">
                  <Link to={capabilityPath(product.slug, ch.slug)} className="font-medium text-ink hover:underline">{ch.title ?? ch.slug}</Link>
                  <span className="ml-2 text-xs text-ink-muted">{ch.counts.openIssues} open · {tierLabel(ch.tier)}</span>
                </li>
              ))}
            </ul>)}
        {tab === 'in-flight' && <InitiativeList initiatives={inFlight} />}
        {tab === 'issues' && <LeafList scope={{ capability: capability.id }} />}
        {tab === 'needs-you' && <NeedsYouList products={[{ ...product, initiatives: needsYou, counts: { ...product.counts, awaitingTriage: 0, needsHome: 0 } }]} repos={[]} />}
      </div>
    </div>
  )
}
```

- [ ] **Step 4: `RoadmapPage.tsx` capability branch**

For `item?.kind === 'capability' && product && capability`: breadcrumb = Roadmap › product › each `capabilityTrail` ancestor (linked) › capability; body `<CapabilityDetail product={product} capability={capability} primaryRepo={project?.repos.find((r) => r.isPrimary)?.repo ?? undefined} onNewInitiative={() => setCreating(true)} />` and pass `defaultCapabilityId={capability.id}` to `NewInitiativeDialog`. Use `useProject(product?.slug ?? null)` in the page for the primary repo.

- [ ] **Step 5: Tests, gate, commit**

```bash
printf 'feat(roadmap): capability detail — overview, sub-capabilities, in flight, issues, needs you\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c10.txt
git add web/src/features/roadmap && git commit -F /tmp/c10.txt
```

---

### Task 11: Initiative page — Track · Epic · Sessions · Capabilities, Advance with confirm

**Files:**
- Modify: `web/src/features/roadmap/InitiativePage.tsx`
- Create: `web/src/features/roadmap/InitiativePage.test.tsx`

**Interfaces:**
- Consumes `useInitiative(slug)` (Task 2), `useAdvanceInitiative()`, `InitiativeTrack`, `Dialog`, `sessionPath` from `@/session/sessionNavState`, `fileUrl`/`prUrl`/`commitUrl` from `@/lib/githubUrl`, `capabilityPath`/`productPath` (Task 5).
- Header: breadcrumb (Roadmap › product › initiative), `StatusPill`-style badges for stage and status, `reasonLabel` when the map says `advance` (read from `useRoadmapMap` by slug), action **Advance → <next stage>** opening a confirm `Dialog`; disabled at the final stage.
- Tabs: `track` (default): `InitiativeTrack` + artifact links (ported from the deleted `InitiativesTab.ArtifactLink`) + `nextAction` sentence; `epic`: epic ref link, progress tiles, children list with state; `sessions`: `buildSessions` rows linking to `sessionPath(id)`; `capabilities`: primary + touched as links to `capabilityPath`.

- [ ] **Step 1: Failing test** — `InitiativePage.test.tsx`

```tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { InitiativePage } from './InitiativePage'

const advance = vi.fn()
vi.mock('@/api/hooks', () => ({
  useInitiative: () => ({ data: {
    slug: 'views', repo: 'cairnea/KinDash', title: 'Use-case-first task views', stage: 'build', status: 'active',
    origin: 'human', originRef: null, epicRepo: 'cairnea/KinDash', epicNumber: 15727, createdAt: null, updatedAt: null,
    artifacts: [{ id: 'a1', kind: 'spec', repoPath: 'docs/spec.md', commitSha: 'abc1234', prNumber: null, prUrl: null, sessionId: null, model: null, createdAt: null }],
    epic: { ref: 'cairnea/KinDash#15727', url: 'https://github.com/cairnea/KinDash/issues/15727', total: 2, done: 2, blocked: 0, needsHuman: 0,
            children: [{ ref: 'cairnea/KinDash#1', title: 'Child one', state: 'closed', status: 'Done' }, { ref: 'cairnea/KinDash#2', title: 'Child two', state: 'closed', status: 'Done' }] },
    projectSlug: 'kindash', primaryCapability: { id: 'f1', slug: 'task-views', title: 'Task views' }, capabilities: [{ id: 'f1', slug: 'task-views', title: 'Task views', isPrimary: true }],
    buildSessions: [{ id: 's1', status: 'complete', branch: 'ccswitch/views' }],
    nextAction: { type: 'handoff_to_build' },
  }, isLoading: false }),
  useRoadmapMap: () => ({ data: { products: [{ slug: 'kindash', name: 'KinDash', tier: 'needs-you', counts: { inFlight: 0, blocked: 0, needsYou: 1, capabilities: 1, awaitingTriage: 0, needsHome: 0 }, capabilities: [], initiatives: [{ slug: 'views', title: 'Use-case-first task views', stage: 'build', status: 'active', projectSlug: 'kindash', primaryCapability: 'task-views', capabilities: ['task-views'], tier: 'needs-you', reason: 'advance', epic: null }] }], generatedAt: 'now' } }),
  useAdvanceInitiative: () => ({ mutate: advance, isPending: false, isError: false }),
}))

function mount(initial = '/initiatives/views') {
  render(
    <MemoryRouter initialEntries={[initial]}>
      <Routes><Route path="/initiatives/:initiativeSlug" element={<InitiativePage />} /></Routes>
    </MemoryRouter>,
  )
}

describe('InitiativePage', () => {
  it('shows the breadcrumb, the reason, and the artifact link at its SHA', () => {
    mount()
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Use-case-first task views')
    expect(screen.getByRole('link', { name: 'kindash' })).toHaveAttribute('href', '/roadmap/kindash')
    expect(screen.getByText('Ready to advance')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /spec/ })).toHaveAttribute('href', expect.stringContaining('abc1234'))
  })
  it('advances only after confirming', () => {
    mount()
    fireEvent.click(screen.getByRole('button', { name: /Advance → Deploy/ }))
    expect(advance).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm advance' }))
    expect(advance).toHaveBeenCalledWith('views', expect.anything())
  })
  it('Epic tab lists children; Sessions tab links to the session; Capabilities tab links to the capability', () => {
    mount('/initiatives/views?tab=epic')
    expect(screen.getByText('Child one')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: /Sessions/ }))
    expect(screen.getByRole('link', { name: /ccswitch\/views/ })).toHaveAttribute('href', '/sessions/s1')
    fireEvent.click(screen.getByRole('tab', { name: /Capabilities/ }))
    expect(screen.getByRole('link', { name: /Task views/ })).toHaveAttribute('href', '/roadmap/kindash/capabilities/task-views')
  })
})
```

- [ ] **Step 2: Implement `InitiativePage.tsx`** (replace the skeleton)

```tsx
import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useAdvanceInitiative, useInitiative, useRoadmapMap } from '@/api/hooks'
import { INITIATIVE_STAGES, type Initiative, type InitiativeArtifact } from '@/api/sdlc'
import { extractMutationErrorMessage } from '@/api/queryClient'
import { PageHeader } from '@/ui/PageHeader'
import { PageContent } from '@/ui/PageContent'
import { EmptyState } from '@/ui/EmptyState'
import { Breadcrumb } from '@/ui/Breadcrumb'
import { Badge } from '@/ui/Badge'
import { Button } from '@/ui/Button'
import { Dialog } from '@/ui/Dialog'
import { Tabs } from '@/ui/Tabs'
import { InitiativeTrack } from '@/initiatives/InitiativeTrack'
import { fileUrl, commitUrl, prUrl } from '@/lib/githubUrl'
import { sessionPath } from '@/session/sessionNavState'
import { CAPABILITY, EPIC, ROADMAP, initiativeStatusLabel, reasonLabel, stageLabel } from '@/features/products/vocab'
import { capabilityPath, productPath, roadmapPath } from './roadmapNavState'
import { useTabParam } from './useTabParam'
import { StatTiles } from './detail/StatTiles'

const TABS = ['track', 'epic', 'sessions', 'capabilities'] as const

function nextStage(stage: string | null): string | null {
  const i = INITIATIVE_STAGES.indexOf(stage as (typeof INITIATIVE_STAGES)[number])
  return i < 0 || i + 1 >= INITIATIVE_STAGES.length ? null : INITIATIVE_STAGES[i + 1]!
}

function ArtifactLink({ repo, artifact }: { repo: string | null; artifact: InitiativeArtifact }) {
  const kind = artifact.kind ?? 'artifact'
  if (repo && artifact.repoPath && artifact.commitSha) {
    return <a className="text-accent hover:underline" target="_blank" rel="noreferrer" href={fileUrl(repo, artifact.commitSha, artifact.repoPath)} title={`${artifact.repoPath} @ ${artifact.commitSha.slice(0, 7)}`}>{kind} ↗</a>
  }
  if (artifact.prUrl || (repo && artifact.prNumber)) {
    return <a className="text-accent hover:underline" target="_blank" rel="noreferrer" href={artifact.prUrl ?? prUrl(repo!, artifact.prNumber!)}>{kind} (PR #{artifact.prNumber}) ↗</a>
  }
  return <span className="text-ink-muted" title="not committed yet">{kind} (pending)</span>
}

const NEXT_ACTION_COPY: Record<string, string> = {
  propose: 'The synthesizer will propose the next artifact.',
  await_approval: 'An artifact PR is waiting for your review.',
  handoff_to_build: 'Ready to hand off to build.',
  done: 'Nothing left to do.',
  needs_human: 'Needs a human decision.',
}

function TrackTab({ initiative }: { initiative: Initiative }) {
  const next = initiative.nextAction
  return (
    <div className="flex flex-col gap-3 prdash-root">
      <InitiativeTrack stage={initiative.stage} status={initiative.status} epic={initiative.epic} />
      {next && <p className="text-sm text-ink-muted">{NEXT_ACTION_COPY[next.type] ?? next.type}{next.reason ? ` — ${next.reason}` : ''}</p>}
      <div className="flex flex-wrap gap-3 text-sm">
        {initiative.artifacts.length === 0
          ? <span className="text-ink-muted">no artifacts committed yet</span>
          : initiative.artifacts.map((a) => <ArtifactLink key={a.id} repo={initiative.repo} artifact={a} />)}
        {initiative.repo && initiative.artifacts.some((a) => a.commitSha) && (
          <a className="text-accent hover:underline" target="_blank" rel="noreferrer"
             href={commitUrl(initiative.repo, initiative.artifacts.find((a) => a.commitSha)!.commitSha!)}>latest commit ↗</a>
        )}
      </div>
    </div>
  )
}

function EpicTab({ initiative }: { initiative: Initiative }) {
  const epic = initiative.epic
  if (!epic) return <EmptyState title={`No ${EPIC.singular.toLowerCase()} yet`} description="Plan materialization (spec 003 Phase C) will create one." />
  return (
    <div className="flex flex-col gap-3">
      <a href={epic.url} target="_blank" rel="noreferrer" className="text-sm text-accent hover:underline">{epic.ref} ↗</a>
      <StatTiles tiles={[
        { label: 'Done', value: epic.done, tone: 'success' },
        { label: 'Total', value: epic.total },
        { label: 'Blocked', value: epic.blocked, tone: 'danger' },
        { label: 'Needs human', value: epic.needsHuman, tone: 'warning' },
      ]} />
      <ul className="flex flex-col gap-1" aria-label="Sub-issues">
        {(epic.children ?? []).map((c) => (
          <li key={c.ref} className="flex items-center gap-2 rounded-md border border-edge bg-surface p-2 text-sm">
            <Badge tone={c.state === 'closed' ? 'success' : 'neutral'}>{c.status ?? c.state ?? '—'}</Badge>
            <span className="truncate text-ink">{c.title ?? c.ref}</span>
            <span className="ml-auto text-xs text-ink-muted">{c.ref}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

function SessionsTab({ initiative }: { initiative: Initiative }) {
  const rows = initiative.buildSessions ?? []
  if (rows.length === 0) return <EmptyState title="No sessions yet" />
  return (
    <ul className="flex flex-col gap-1" aria-label="Sessions">
      {rows.map((s) => (
        <li key={s.id} className="rounded-md border border-edge bg-surface p-2 text-sm">
          <Link to={sessionPath(s.id)} className="text-ink hover:underline">{s.branch ?? s.id.slice(0, 8)}</Link>
          <span className="ml-2 text-xs text-ink-muted">{s.status ?? '—'}</span>
        </li>
      ))}
    </ul>
  )
}

function CapabilitiesTab({ initiative }: { initiative: Initiative }) {
  if (!initiative.projectSlug || initiative.capabilities.length === 0) return <EmptyState title={`No ${CAPABILITY.plural.toLowerCase()} linked`} />
  return (
    <ul className="flex flex-col gap-1" aria-label={CAPABILITY.plural}>
      {initiative.capabilities.map((c) => (
        <li key={c.id} className="rounded-md border border-edge bg-surface p-2 text-sm">
          <Link to={capabilityPath(initiative.projectSlug!, c.slug)} className="text-ink hover:underline">{c.title ?? c.slug}</Link>
          {c.isPrimary && <Badge tone="accent" className="ml-2">primary</Badge>}
        </li>
      ))}
    </ul>
  )
}

export function InitiativePage() {
  const { initiativeSlug = '' } = useParams()
  const { data: initiative, isLoading } = useInitiative(initiativeSlug || null)
  const { data: map } = useRoadmapMap()
  const advance = useAdvanceInitiative()
  const [tab, setTab] = useTabParam(TABS, 'track')
  const [confirming, setConfirming] = useState(false)

  const title = initiative?.title ?? initiativeSlug
  const node = map?.products.flatMap((p) => p.initiatives).find((i) => i.slug === initiativeSlug)
  const reason = reasonLabel(node?.reason)
  const nxt = nextStage(initiative?.stage ?? null)
  const crumbs = [
    { label: ROADMAP.label, to: roadmapPath('initiatives') },
    ...(initiative?.projectSlug ? [{ label: initiative.projectSlug, to: productPath(initiative.projectSlug) }] : []),
    { label: title },
  ]

  return (
    <div className="flex h-full min-h-0 flex-col" data-page-variant="standard">
      <PageHeader
        title={title}
        meta={
          <div className="flex flex-col gap-1">
            <Breadcrumb items={crumbs} />
            {initiative && (
              <div className="flex flex-wrap items-center gap-2">
                <Badge tone="info">{stageLabel(initiative.stage)}</Badge>
                <Badge tone={initiative.status === 'blocked' ? 'danger' : 'neutral'}>{initiativeStatusLabel(initiative.status)}</Badge>
                {reason && <Badge tone="warning">{reason}</Badge>}
                {initiative.repo && <span className="text-xs text-ink-muted">{initiative.repo}</span>}
              </div>
            )}
          </div>
        }
        actions={initiative && (
          <Button size="sm" disabled={!nxt || advance.isPending} onClick={() => setConfirming(true)}
                  title={nxt ? `Advance the gate to ${stageLabel(nxt)}` : 'Already at the final stage'}>
            {nxt ? `Advance → ${stageLabel(nxt)}` : 'Complete'}
          </Button>
        )}
      />
      <PageContent busy={isLoading && !initiative}>
        {!initiative && !isLoading && <EmptyState title="Initiative not found" action={<Link className="text-accent hover:underline" to={roadmapPath('initiatives')}>Back to the roadmap</Link>} />}
        {initiative && (
          <div className="flex flex-col gap-4">
            {advance.isError && <p role="alert" className="text-sm text-danger">{extractMutationErrorMessage(advance.error, 'Could not advance initiative')}</p>}
            <Tabs label={title} value={tab} onChange={(id) => setTab(id as typeof TABS[number])} panelId={(id) => `ini-panel-${id}`}
                  items={[
                    { id: 'track', label: 'Track' },
                    { id: 'epic', label: EPIC.singular, count: initiative.epic?.total },
                    { id: 'sessions', label: 'Sessions', count: initiative.buildSessions?.length },
                    { id: 'capabilities', label: CAPABILITY.plural, count: initiative.capabilities.length },
                  ]} />
            <div role="tabpanel" id={`ini-panel-${tab}`} aria-labelledby={`tab-${tab}`}>
              {tab === 'track' && <TrackTab initiative={initiative} />}
              {tab === 'epic' && <EpicTab initiative={initiative} />}
              {tab === 'sessions' && <SessionsTab initiative={initiative} />}
              {tab === 'capabilities' && <CapabilitiesTab initiative={initiative} />}
            </div>
          </div>
        )}
      </PageContent>
      {initiative && nxt && (
        <Dialog open={confirming} onClose={() => setConfirming(false)} title={`Advance to ${stageLabel(nxt)}?`} size="sm"
                description="This moves the gate. The stage engine will act on the new stage on its next tick.">
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setConfirming(false)}>Cancel</Button>
            <Button aria-label="Confirm advance" onClick={() => advance.mutate(initiative.slug, { onSettled: () => setConfirming(false) })}>Advance</Button>
          </div>
        </Dialog>
      )}
    </div>
  )
}
```

- [ ] **Step 3: Tests, full web gate, commit**

```bash
printf 'feat(roadmap): initiative page — track, epic, sessions, capabilities; Advance with confirm\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c11.txt
git add web/src/features/roadmap && git commit -F /tmp/c11.txt
```

---

### Task 12: Close out — docs, e2e smoke, gates, PR

**Files:**
- Modify: `docs/product/features/product-management-layer.md` (the "Notes" section: the Roadmap section replaces Products/Initiatives; paths; what Phase 3 shipped and what Phases 4–6 still owe), `docs/superpowers/specs/2026-09-07-roadmap-drilldown-design.md` (status line: "Phase 3 landed in #<PR>"), `web/README.md` if it lists sections.
- Create: `web/e2e/roadmap.spec.ts` (Playwright): open `/roadmap`, click the first product row, expect the `h1` to change and the URL to be `/roadmap/<slug>`; press Back, expect `/roadmap`. Follow the existing e2e fixtures under `web/e2e/` for how the app is served and mocked.

- [ ] **Step 1: Docs edits** as listed. In the feature doc, replace any sentence that says "Products tab" / "Initiatives tab" with the Roadmap wording and paths from finish-line spec §4.2.

- [ ] **Step 2: e2e smoke**

```ts
import { test, expect } from '@playwright/test'

test('roadmap drills from portfolio to a product and back', async ({ page }) => {
  await page.goto('/roadmap')
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Roadmap')
  const first = page.getByRole('button', { name: /^Open product / }).first()
  const name = (await first.getAttribute('aria-label'))!.replace('Open product ', '')
  await first.click()
  await expect(page).toHaveURL(/\/roadmap\/[^/]+$/)
  await expect(page.getByRole('heading', { level: 1 })).toHaveText(name)
  await page.goBack()
  await expect(page).toHaveURL(/\/roadmap$/)
})
```

If the e2e harness has no products seeded, seed one through `POST /api/pm/projects` in a `beforeEach` using the harness's API helper, or skip with a documented reason in the test file header.

- [ ] **Step 3: Full gates**

```bash
(cd web && npm run typecheck && npm run lint && npm run test && npm run build)
ruff check src/ server/ && mypy server/ --ignore-missing-imports && .venv/bin/python -m pytest tests/ -q
# full server suite, background, tee'd, with the test-slot acquire in the SAME shell (see CLAUDE.md)
```

Success criteria to confirm by hand before opening the PR:
- `grep -rn "projects.css\|initiatives.css\|InitiativesTab\|ProjectsTab" web/src` → nothing.
- `/projects`, `/projects/kindash`, `/initiatives` redirect; `/initiatives/<slug>` renders with the rail on Roadmap.
- The nav has seven entries; the list column appears on `/roadmap*` at 1440 and yields to the detail at 390.
- Screenshot `/roadmap`, `/roadmap/<product>`, `/roadmap/<product>/capabilities/<cap>`, `/initiatives/<slug>` at 1440 and attach to the PR.

- [ ] **Step 4: PR**

```bash
git push -u origin roadmap-phase3
printf '## Summary\n\nRoadmap Phase 3 (finish-line row 2, Fixes #993): one Roadmap section on the Sessions chassis replaces Products and Initiatives.\n\n- List column with Map (tree) and Initiatives scopes over GET /api/pm/map\n- Portfolio, product, capability and initiative detail levels, tabs as URL state\n- Advance-ready initiatives roll up to needs-you (spec §4.5)\n- /projects*, /initiatives redirect; ProjectsTab, InitiativesTab and their css deleted\n\nSpec: docs/superpowers/specs/2026-09-08-finish-line-persona-modes-design.md §4; docs/superpowers/specs/2026-09-07-roadmap-drilldown-design.md §5–§7, §11.\nPlan: docs/superpowers/plans/2026-09-08-roadmap-phase3-products-master-detail.md\n\n## Verification\n\n(paste the gate output and screenshots)\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)\n' > /tmp/pr-phase3.md
gh pr create --title "feat(roadmap): phase 3 — Products master/detail at /roadmap replaces Products and Initiatives (#993)" --body-file /tmp/pr-phase3.md --base master
gh pr merge --squash --auto <number>
```

---

## Self-review notes

- Spec coverage: §4.1 nav (Task 6), §4.2 routes and redirects (Tasks 5–6), §4.3 scopes Map/Initiatives (Task 5; Inbox and Queue are rows 3 and spec 003 C1), §4.5 advance tier (Task 1, surfaced in Tasks 8, 11), roadmap §6 list column (Tasks 4–5; virtualization deferred to Phase 4 with issue leaves, which is when the row count grows), §7 detail levels (Tasks 8–11), §11 tier mapping (Task 3). Not in this plan by design: §8 inline editing, §9 map-changes PRs, §12 triage on the chassis (rows 3 and 8).
- Type consistency: `RoadmapListColumnProps.onOpen(row: TreeRow)` is used by Tasks 5, 6, 8; `keyForOpenItem` lives in `roadmapNavState.ts` (Task 5) and is what `useRoadmapWorkspace` (Task 6) calls; `TreeRow` initiative rows carry a `product` so `onOpen` never needs a lookup; `InitiativeCard` takes `rowKey`/`depth`/`initiative`/`selected`/`onOpen` everywhere it is used (Tasks 5, 8).
- Known judgement calls an implementer may hit: `SessionListControls` may require a `filters` prop; `GroupedList` may require `listMode`; `Field`/`Button` prop names must be read from the primitives; test factories that build `Initiative` literals need the three new fields.
