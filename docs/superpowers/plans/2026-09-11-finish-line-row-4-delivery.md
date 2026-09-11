# Delivery section (finish-line row 4) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Automations and Insights pages (and the Settings › Automations section) with one **Delivery** section on the Sessions master/detail chassis: a list column with **Automations** and **CI health** scopes, and a detail pane that is one automation (Status · Schedule · Events · Sessions · Config) or one repo's check health.

**Architecture:** The shell learns a third list section exactly the way it learned Roadmap (`LIST_SECTIONS`, `OPEN_ITEM_RESOLVERS`, a workspace hook, a nav row). Selection is the URL: `/delivery/automations/:automationId`, `/delivery/ci/:owner/:repo`; the list scope is `?scope=ci` on `/delivery` and is implied by the path on a detail route. Every piece of the old monitor (`AutomationsTab`) and the old configuration card (`AutomationCard`) is re-homed under `web/src/features/delivery/` on token classes and the `ui/` primitives; nothing is reimplemented twice. No server change: every endpoint the section needs already exists.

**Tech Stack:** React 19 + react-router + TanStack Query (`useQueries` for per-automation actionable counts) + Tailwind tokens, vitest + Testing Library, Playwright smoke. No `server/` changes.

**Spec:** `docs/superpowers/specs/2026-09-08-finish-line-persona-modes-design.md` §2 D2/D4/D5/D9/D10, §4.1 (nav), §4.2 (routes/redirects), §4.4 (Delivery), §5 row 4, §7 (gates); `docs/superpowers/specs/2026-09-02-ui-page-migration-design.md` §2 (page contract, Standard variant), §3 PR 6 / PR 8 (which this row replaces). Rows 2 and 3 (`2026-09-08-roadmap-phase3-products-master-detail.md`, `2026-09-09-roadmap-inbox-scope.md`) are the pattern and are on master. Issue #1468.

## Global Constraints

- Nav after this row (finish-line §4.1): `sessions · pipeline · roadmap · delivery · settings`. The `automations` and `insights` rows are deleted. `NavBadgeKey 'automations'` is renamed `'delivery'`. `delivery.showInKiosk: false` (the Automations scope mutates); the pinned kiosk-safe set becomes `['pipeline']`.
- Routes (§4.2): `/delivery` (Automations scope, welcome pane), `/delivery?scope=ci` (CI health scope, welcome pane), `/delivery/automations/:automationId`, `/delivery/ci/:owner/:repo`. Redirects (`replace`): `/automations` → `/delivery`; `/insights` → `/delivery?scope=ci`; `/automation` (legacy) and `/settings/automation` → `/delivery` (Task 6). Every legacy redirect already in `routes.tsx` stays.
- Words come from `web/src/features/products/vocab.ts` only: section label `Delivery`, scopes `Automations` / `CI health`, tab labels `Status · Schedule · Events · Sessions · Config`. Code ids stay `automations`, `insights`-derived hook/QK names, `automation` settings id until deleted (D9).
- New web files live under `web/src/features/delivery/`; all are under `ccswitch/no-raw-palette`: token classes only (`bg-surface`, `text-ink-muted`, `border-edge`, `bg-canvas`, `text-accent`, `min-h-target`, `rounded-pill`, `text-danger`, `bg-warning-wash` …), no `dark:`, no hex, no `bg-blue-500`/`text-gray-500`, no `z-<n>`. Use `ui/` primitives (`Button`, `Select`, `Switch`, `Input`, `Checkbox`, `Field`, `Badge`, `Tabs`, `PageHeader`, `PageContent`, `EmptyState`, `Banner`, `Skeleton`, `Breadcrumb`). No new `.css` files.
- Tiers: reuse the roadmap tier presentation (`features/roadmap/roadmapTier.ts` `TIER_ACCENT`, `TIER_TONE`; `rows/TierDot`; vocab `TIER_LABEL`) — `AutomationTier` is a subset of `RoadmapTier`, never a parallel table (constitution VI).
- Deleted in this row, each with its test file in the same task: `web/src/automations/AutomationsTab.tsx` (+ `AutomationsTab.test.tsx`), `web/src/components/InsightsView.tsx` (+ `InsightsView.test.tsx`, `InsightsTab.test.tsx`), `web/src/components/CheckTable.tsx` (+ test; moved), `web/src/components/AutomationView.tsx`, `web/src/components/AutomationCard.tsx` (+ test), `web/src/components/__tests__/automationTab.test.tsx`, the `automation` row of `settingsDefs.ts`. `web/src/components/ScheduleEditor.tsx`, `ConsolidatePanel.tsx`, `BacklogGroupsPanel.tsx`, `ManualDispatchDialog.tsx` stay where they are (row 7 sweeps `components/`); they are imported, not copied.
- Every new routed page adds rows to `web/src/features/settings/pageContract.test.tsx` and any new `api.get` path to its mock. `routes.test.tsx`'s matrix and redirect tables are updated in the same task as the routes.
- Web gate on every task: `(cd web && npm run typecheck && npm run lint && npm run test && npm run build)` then `rm -rf web/dist`. Run the FULL vitest suite: 79 files mock `@/api/hooks`, and the shell now calls two more hooks app-wide. There is no server change, so the server gate is not needed; run `ruff check src/ server/` once at the end anyway (docs task touches nothing under `server/`).
- Bash hook: `(cd web && …)` subshells, never a persisted `cd`; commit messages via `git commit -F <file>`; never pair deploy/release/rollout/promote with prod/production in a command's text (note: "products" contains "prod" — keep such words out of command lines, put them in files).
- Commit trailer: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. Branch: `feat/finish-line-row-4-delivery` off `origin/master` (already created). One PR: `Fixes #1468`, `Refs #1520`; ready + `gh pr merge --squash --auto` when green. Never push, open the PR or rebase from a task; the controller does.

## Rulings (controller, pre-execution)

- **R1 — Settings › Automations is deleted in this row.** The Delivery detail's header (mode select, enable switch) and its Schedule and Config tabs are the one place an automation is configured; `settingsDefs.ts`'s own comment says "PR 6 folds them into the page and drops this row", and this row replaces PR 6. `/settings/automation` and `/automation` redirect to `/delivery`. Cost if wrong: one redirect and one settings row to restore.
- **R2 — Attention tier from data the list payload carries, plus the actionable count.** `automationTier(a, actionable)`: `needs-you` when `actionable > 0` or `status.level === 'error'`; `blocked` when `enabled && status.level === 'warn'` (the loop reports `warn` when paused — outside its active hours — or degraded; the closest payload-carried signal to the spec's "schedule window is closed"); `in-flight` when `activeSessions > 0` or `status.level === 'active'`; else `idle`. Actionable counts come from one `useQueries` over `GET /automations/{id}/actionable` (`useAutomationActionableCounts`), fetched only while the Delivery column is mounted. Cost if wrong: one pure function.
- **R3 — The nav badge stays Σ `activeSessions`, keyed `delivery`.** Spec §4.1 says "actionable items"; that count is per-automation (`/{id}/actionable`) and a badge that is right on every section would need N polls app-wide or a list-payload count (a server change, excluded by D10). Deferred; the controller files a follow-up issue ("list payload carries `actionableCount`; nav badge and row tier read it"). Cost if wrong: one line in `useNavBadges`.
- **R4 — CI health rows carry no tier.** Spec §4.4 asks for a meta line (7-day flaky count, slowest check) and nothing about attention for repos; rows are sorted by name with a neutral rail. Each row reads `useCheckInsights(repo, '7d', { poll: false })` (one fetch per row, no timer; the detail page owns the polling copy of its own key).
- **R5 — Kiosk loses the CI-health page for now.** `showInKiosk` is per section and the Automations scope mutates, so `delivery` is `false`; the Pipeline lane-jump that used to land on `/insights` now navigates to `/delivery?scope=ci`, which `KioskPageGuard` refuses in kiosk (it lands on the kiosk home). The controller files a follow-up ("kiosk-safe CI-health scope") per spec §4.1's "once verified read-only". Cost if wrong: a `showInKiosk` flip with evidence.
- **R6 — `?window=` is URL state** on the CI detail (page-migration §2.3: sections are URL state), read through a generalized `useSearchParamState` that `useTabParam` becomes a one-line wrapper of.
- **R7 — The `PipelineTab` prop is renamed** `onOpenInsights` → `onOpenCiHealth` with its test; a prop named for a deleted page is half-dead code.

## File map

| File | Responsibility |
|------|----------------|
| `web/src/features/products/vocab.ts` (+ test) | `DELIVERY` words, `AUTOMATION_TAB`, `MODE_DESCRIPTION`. |
| `web/src/features/delivery/deliveryNavState.ts` (+ test) | Paths, route patterns, scope-from-URL, open item, row keys. |
| `web/src/features/delivery/automationTier.ts` (+ test) | Pure: tier rule, rank, line-2 text. |
| `web/src/features/delivery/__fixtures__/automations.ts` | Shared `AUTOMATIONS` fixture for tests. |
| `web/src/api/hooks.ts` | `useStageModels({enabled, poll})`, `useCheckInsights(repo, window, {poll})`, `useAutomationActionableCounts(ids)`. |
| `web/src/features/delivery/rows/AutomationRow.tsx`, `CiRepoRow.tsx` | One row each on `CardFrame`. |
| `web/src/features/delivery/DeliveryScopeSegment.tsx` | Automations · CI health switch. |
| `web/src/features/delivery/AutomationsScopeBody.tsx`, `CiScopeBody.tsx` | The two scope bodies (search, sort, empty/loading/error). |
| `web/src/features/delivery/DeliveryListColumn.tsx` (+ test) | The `app.list` column. |
| `web/src/features/delivery/automation/actions.tsx` | `ActionButton`, `AutomationActions` (moved from `AutomationsTab`, tokenized). |
| `web/src/features/delivery/automation/EventsList.tsx`, `SessionsList.tsx` | Events tab, Sessions tab bodies. |
| `web/src/features/delivery/automation/ConfigPanel.tsx` | Moved from `AutomationCard` onto `ui/` primitives. |
| `web/src/features/delivery/automation/AutomationDetail.tsx` (+ test) | Tabs + bodies; the detail pane. |
| `web/src/features/delivery/automation/AutomationControls.tsx` | Header actions: mode `Select` + enable `Switch`. |
| `web/src/features/delivery/ci/format.ts`, `CheckTable.tsx` (+ test) | `fmtSecs`; table moved from `components/`. |
| `web/src/features/delivery/ci/CiRepoDetail.tsx` (+ test) | The four tables. |
| `web/src/lib/useSearchParamState.ts` (+ test) | `?name=` as state; `useTabParam` wraps it. |
| `web/src/features/delivery/DeliveryWelcome.tsx` | Nothing-open pane per scope. |
| `web/src/features/delivery/useDeliveryWorkspace.ts` (+ test) | The section's workspace hook. |
| `web/src/features/delivery/DeliveryPage.tsx` (+ test) | Routed page: automation / repo / welcome. |
| `web/src/shell/navDefs.ts`, `useNavBadges.ts`, `openItems.ts`, `listColumns.tsx`, `routes.tsx`, `AppShell.tsx`, `kiosk.ts` (+ tests) | Wiring. |
| `web/src/pipeline/PipelineTab.tsx` (+ test) | `onOpenCiHealth`. |
| `web/src/features/settings/settingsDefs.ts`, `SettingsPage.test.tsx`, `settingsDefs.test.ts` | Automations section removed (Task 6). |
| `web/e2e/delivery.spec.ts` | Smoke: toggle, phone hand-off. |
| deleted | see Global Constraints. |
| `docs/superpowers/specs/2026-09-08-finish-line-persona-modes-design.md` | §5 row 4 status line. |

---

### Task 1: Vocabulary, navigation state, and the tier rule (pure modules)

**Files:**
- Modify: `web/src/features/products/vocab.ts`, `web/src/features/products/vocab.test.ts`
- Create: `web/src/features/delivery/deliveryNavState.ts`, `web/src/features/delivery/deliveryNavState.test.ts`, `web/src/features/delivery/automationTier.ts`, `web/src/features/delivery/automationTier.test.ts`, `web/src/features/delivery/__fixtures__/automations.ts`

**Interfaces:**
- Produces (vocab): `DELIVERY = { label: 'Delivery', automations: 'Automations', ci: 'CI health', help: string }`; `AUTOMATION_TAB = { status: 'Status', schedule: 'Schedule', events: 'Events', sessions: 'Sessions', config: 'Config' }`; `MODE_DESCRIPTION: Record<string, string>` (moved from `AutomationCard`'s `MODE_DESCRIPTIONS`).
- Produces (nav state): `DELIVERY_PATH = '/delivery'`; `AUTOMATION_ROUTE = '/delivery/automations/:automationId'`; `CI_REPO_ROUTE = '/delivery/ci/:owner/:repo'`; `type DeliveryScope = 'automations' | 'ci'`; `deliveryScopeForSearch(search: string): DeliveryScope`; `deliveryScope(pathname: string, search: string): DeliveryScope` (path wins); `deliveryPath(scope?: DeliveryScope): string`; `automationPath(id: string): string`; `ciRepoPath(repo: string): string` (`repo` is `owner/name`); `type DeliveryOpenItem = { kind: 'automation'; automationId: string } | { kind: 'ciRepo'; repo: string }`; `deliveryOpenItem(params, search): DeliveryOpenItem | null`; `automationKey(id)`, `ciRepoKey(repo)`, `keyForDeliveryItem(item)`.
- Produces (tier): `type AutomationTier = Extract<RoadmapTier, 'needs-you' | 'blocked' | 'in-flight' | 'idle'>`; `automationTier(a: Pick<Automation, 'enabled' | 'status' | 'activeSessions'>, actionable: number): AutomationTier`; `automationTierLine(tier, a, actionable): string` (line 2 of a row); `AUTOMATION_TIER_RANK` re-exported from `TIER_RANK`.
- Produces (fixture): `AUTOMATIONS: Automation[]` — `pr-doctor` (enabled, mode `auto`, modes `['off','propose','auto']`, status `{ text: '2 PRs failing', level: 'warn', counts: null }`, `activeSessions: 1`, `workClass: 'maintenance'`, `configSchema` with one `bool` field `auto_fix` and one `repo-multiselect` `enabled_repos`, `schedule: null`), `backlog-runner` (enabled, `auto`, status `idle` 'Idle', `activeSessions: 0`, `workClass: 'development'`, `configSchema: []`), `pm-classify` (`operatorTriggered: true`, `modes: []`, `enabled: false`, status idle).

- [ ] **Step 1: Failing tests**

Append to `vocab.test.ts`:

```ts
describe('DELIVERY vocabulary (finish-line D9)', () => {
  it('names the section, both scopes and the five automation tabs', async () => {
    const { DELIVERY, AUTOMATION_TAB, MODE_DESCRIPTION } = await import('./vocab')
    expect(DELIVERY.label).toBe('Delivery')
    expect([DELIVERY.automations, DELIVERY.ci]).toEqual(['Automations', 'CI health'])
    expect(Object.values(AUTOMATION_TAB)).toEqual(['Status', 'Schedule', 'Events', 'Sessions', 'Config'])
    expect(DELIVERY.help.length).toBeGreaterThan(40)
    expect(Object.keys(MODE_DESCRIPTION)).toEqual(['off', 'propose', 'auto'])
  })
})
```

`deliveryNavState.test.ts`:

```ts
// @vitest-environment node
import { describe, it, expect } from 'vitest'
import {
  AUTOMATION_ROUTE, CI_REPO_ROUTE, DELIVERY_PATH, automationKey, automationPath, ciRepoKey, ciRepoPath,
  deliveryOpenItem, deliveryPath, deliveryScope, deliveryScopeForSearch, keyForDeliveryItem,
} from './deliveryNavState'

describe('deliveryNavState', () => {
  it('spells the paths once', () => {
    expect(DELIVERY_PATH).toBe('/delivery')
    expect(AUTOMATION_ROUTE).toBe('/delivery/automations/:automationId')
    expect(CI_REPO_ROUTE).toBe('/delivery/ci/:owner/:repo')
    expect(deliveryPath()).toBe('/delivery')
    expect(deliveryPath('automations')).toBe('/delivery')
    expect(deliveryPath('ci')).toBe('/delivery?scope=ci')
    expect(automationPath('pr-doctor')).toBe('/delivery/automations/pr-doctor')
    expect(ciRepoPath('cairnea/KinDash')).toBe('/delivery/ci/cairnea/KinDash')
  })
  it('reads the scope from ?scope=, defaulting to automations', () => {
    expect(deliveryScopeForSearch('')).toBe('automations')
    expect(deliveryScopeForSearch('?scope=ci')).toBe('ci')
    expect(deliveryScopeForSearch('?scope=nope')).toBe('automations')
  })
  it('a detail path implies its scope, whatever the search says', () => {
    expect(deliveryScope('/delivery/ci/o/r', '')).toBe('ci')
    expect(deliveryScope('/delivery/automations/pr-doctor', '?scope=ci')).toBe('automations')
    expect(deliveryScope('/delivery', '?scope=ci')).toBe('ci')
  })
  it('resolves the open item by PARAM NAME only', () => {
    expect(deliveryOpenItem({ automationId: 'pr-doctor' }, new URLSearchParams())).toEqual({ kind: 'automation', automationId: 'pr-doctor' })
    expect(deliveryOpenItem({ owner: 'o', repo: 'r' }, new URLSearchParams())).toEqual({ kind: 'ciRepo', repo: 'o/r' })
    expect(deliveryOpenItem({ id: 'x' }, new URLSearchParams())).toBeNull()
    expect(deliveryOpenItem({}, new URLSearchParams('?scope=ci'))).toBeNull()
  })
  it('keys rows the way the open item is keyed', () => {
    expect(keyForDeliveryItem({ kind: 'automation', automationId: 'a' })).toBe(automationKey('a'))
    expect(keyForDeliveryItem({ kind: 'ciRepo', repo: 'o/r' })).toBe(ciRepoKey('o/r'))
    expect(keyForDeliveryItem(null)).toBeNull()
  })
})
```

`automationTier.test.ts`:

```ts
// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { automationTier, automationTierLine } from './automationTier'

const base = { enabled: true, status: { text: 'Idle', level: 'idle' as const, counts: null }, activeSessions: 0 }
const at = (over: Partial<typeof base>, actionable = 0) => automationTier({ ...base, ...over }, actionable)

describe('automationTier', () => {
  it('needs-you when there is something to act on or the loop errored', () => {
    expect(at({}, 2)).toBe('needs-you')
    expect(at({ status: { text: 'boom', level: 'error', counts: null } })).toBe('needs-you')
  })
  it('blocked when enabled but paused/degraded (warn)', () => {
    expect(at({ status: { text: 'paused — outside scheduled active hours', level: 'warn', counts: null } })).toBe('blocked')
    expect(at({ enabled: false, status: { text: 'x', level: 'warn', counts: null } })).toBe('idle')
  })
  it('in-flight when it has live sessions or is active', () => {
    expect(at({ activeSessions: 1 })).toBe('in-flight')
    expect(at({ status: { text: 'scanning', level: 'active', counts: null } })).toBe('in-flight')
  })
  it('needs-you outranks everything', () => {
    expect(at({ activeSessions: 3 }, 1)).toBe('needs-you')
  })
  it('line 2 names the tier and the actionable count', () => {
    expect(automationTierLine('needs-you', { ...base }, 2)).toBe('Needs you · 2 actionable')
    expect(automationTierLine('in-flight', { ...base, activeSessions: 1 }, 0)).toBe('In flight · 1 live session')
    expect(automationTierLine('idle', base, 0)).toBe('Idle')
  })
})
```

- [ ] **Step 2: Run to fail**

Run: `(cd web && npx vitest run src/features/products/vocab.test.ts src/features/delivery)`
Expected: FAIL — modules missing.

- [ ] **Step 3: Implement**

`vocab.ts` — append after `ROADMAP`:

```ts
/** The Delivery section and its list scopes (finish-line spec §4.1, §4.4). */
export const DELIVERY = {
  label: 'Delivery',
  automations: 'Automations',
  ci: 'CI health',
  help: 'Is delivery healthy? Every automation that runs against your repos and sessions — what it is doing, what it needs from you, its schedule and configuration — and the CI health of each tracked repository: slow, flaky and regressing checks.',
} as const

/** The automation detail's tabs, in order (finish-line spec §4.4). */
export const AUTOMATION_TAB = {
  status: 'Status', schedule: 'Schedule', events: 'Events', sessions: 'Sessions', config: 'Config',
} as const
export type AutomationTab = keyof typeof AUTOMATION_TAB
export const AUTOMATION_TABS = Object.keys(AUTOMATION_TAB) as readonly AutomationTab[]

/** What each automation mode means, beside the mode select. */
export const MODE_DESCRIPTION: Record<string, string> = {
  off: 'Not monitoring.',
  propose: 'Suggests actions — you approve before anything runs.',
  auto: 'Acts automatically without confirmation.',
}
```

`deliveryNavState.ts`:

```ts
/**
 * Where the Delivery section lives and what is open — the one module that
 * spells these paths (finish-line spec §4.2), modelled on
 * `features/roadmap/roadmapNavState.ts`. The route PARAM NAMES are the contract:
 * the open-item resolver discriminates on them alone (`automationId`,
 * `owner` + `repo`), never on `id`, so a Delivery route can never be mistaken
 * for a live session.
 */
export const DELIVERY_PATH = '/delivery'
export const AUTOMATION_ROUTE = `${DELIVERY_PATH}/automations/:automationId`
export const CI_REPO_ROUTE = `${DELIVERY_PATH}/ci/:owner/:repo`

export type DeliveryScope = 'automations' | 'ci'
export const DELIVERY_SCOPES: readonly DeliveryScope[] = ['automations', 'ci']

export function deliveryScopeForSearch(search: string): DeliveryScope {
  const v = new URLSearchParams(search).get('scope')
  return (DELIVERY_SCOPES as readonly string[]).includes(v ?? '') ? (v as DeliveryScope) : 'automations'
}

/** The scope the list should show: a detail path names its own scope; on the
 *  bare section path `?scope=` decides. */
export function deliveryScope(pathname: string, search: string): DeliveryScope {
  if (pathname.startsWith(`${DELIVERY_PATH}/ci/`)) return 'ci'
  if (pathname.startsWith(`${DELIVERY_PATH}/automations/`)) return 'automations'
  return deliveryScopeForSearch(search)
}

export function deliveryPath(scope: DeliveryScope = 'automations'): string {
  return scope === 'automations' ? DELIVERY_PATH : `${DELIVERY_PATH}?scope=${scope}`
}
export const automationPath = (id: string) => `${DELIVERY_PATH}/automations/${encodeURIComponent(id)}`
/** `repo` is `owner/name`; each half is one path segment. */
export const ciRepoPath = (repo: string) => {
  const [owner, name] = repo.split('/')
  return `${DELIVERY_PATH}/ci/${encodeURIComponent(owner ?? '')}/${encodeURIComponent(name ?? '')}`
}

export type DeliveryOpenItem =
  | { kind: 'automation'; automationId: string }
  | { kind: 'ciRepo'; repo: string }

export function deliveryOpenItem(
  params: Readonly<Record<string, string | undefined>>,
  _search: URLSearchParams,
): DeliveryOpenItem | null {
  const { automationId, owner, repo } = params
  if (automationId) return { kind: 'automation', automationId }
  if (owner && repo) return { kind: 'ciRepo', repo: `${owner}/${repo}` }
  return null
}

export const automationKey = (id: string) => `automation:${id}`
export const ciRepoKey = (repo: string) => `ci:${repo}`

/** The list-row key an open item highlights. */
export function keyForDeliveryItem(item: DeliveryOpenItem | null): string | null {
  if (!item) return null
  return item.kind === 'automation' ? automationKey(item.automationId) : ciRepoKey(item.repo)
}
```

`automationTier.ts`:

```ts
/**
 * An automation's attention tier (finish-line spec §4.4), computed from the
 * list payload plus its actionable-item count. The vocabulary is the roadmap's
 * (`RoadmapTier`) so the row's rail, dot and pill reuse `roadmapTier.ts` and
 * `TIER_LABEL` rather than a second table.
 *
 * `blocked` reads the loop's `warn` level: the loop reports `warn` while paused
 * outside its active hours and while degraded, which is the payload-carried
 * signal closest to "its schedule window is closed" (ruling R2).
 */
import type { Automation } from '@/api/automations'
import type { RoadmapTier } from '@/api/roadmap'
import { TIER_LABEL } from '@/features/products/vocab'
import { TIER_RANK } from '@/features/roadmap/roadmapTier'

export type AutomationTier = Extract<RoadmapTier, 'needs-you' | 'blocked' | 'in-flight' | 'idle'>

type TierInput = Pick<Automation, 'enabled' | 'status' | 'activeSessions'>

export function automationTier(a: TierInput, actionable: number): AutomationTier {
  if (actionable > 0 || a.status.level === 'error') return 'needs-you'
  if (a.enabled && a.status.level === 'warn') return 'blocked'
  if (a.activeSessions > 0 || a.status.level === 'active') return 'in-flight'
  return 'idle'
}

export const AUTOMATION_TIER_RANK: Record<AutomationTier, number> = {
  'needs-you': TIER_RANK['needs-you'], blocked: TIER_RANK.blocked, 'in-flight': TIER_RANK['in-flight'], idle: TIER_RANK.idle,
}

/** Line 2 of an automation row: the tier word plus the one number it rests on. */
export function automationTierLine(tier: AutomationTier, a: TierInput, actionable: number): string {
  const word = TIER_LABEL[tier]
  if (tier === 'needs-you' && actionable > 0) return `${word} · ${actionable} actionable`
  if (tier === 'in-flight' && a.activeSessions > 0) {
    return `${word} · ${a.activeSessions} live ${a.activeSessions === 1 ? 'session' : 'sessions'}`
  }
  return word
}
```

`__fixtures__/automations.ts`:

```ts
import type { Automation } from '@/api/automations'

export const AUTOMATIONS: Automation[] = [
  {
    id: 'pr-doctor', name: 'PR doctor', description: 'Fixes failing PRs.', enabled: true, mode: 'auto',
    modes: ['off', 'propose', 'auto'], status: { text: '2 PRs failing', level: 'warn', counts: null },
    enginePolicy: 'claude_only', operatorTriggered: false,
    configSchema: [
      { key: 'auto_fix', label: 'Auto-fix', type: 'bool', default: true, help: 'Open fix sessions without asking.', choices: null },
      { key: 'enabled_repos', label: 'Repositories', type: 'repo-multiselect', default: [], help: '', choices: null },
    ],
    config: { auto_fix: true, enabled_repos: ['o/r'] }, schedule: null, lastActiveAt: '2026-09-11T10:00:00Z',
    activeSessions: 1, workClass: 'maintenance',
  },
  {
    id: 'backlog-runner', name: 'Backlog runner', description: 'Dispatches eligible issues.', enabled: true, mode: 'auto',
    modes: ['off', 'propose', 'auto'], status: { text: 'Idle', level: 'idle', counts: null },
    enginePolicy: 'claude_only', operatorTriggered: false, configSchema: [], config: {}, schedule: null,
    lastActiveAt: null, activeSessions: 0, workClass: 'development',
  },
  {
    id: 'pm-classify', name: 'PM classify', description: 'Classifies issues on request.', enabled: false, mode: null,
    modes: [], status: { text: 'Idle', level: 'idle', counts: null }, enginePolicy: 'claude_only', operatorTriggered: true,
    configSchema: [], config: {}, schedule: null, lastActiveAt: null, activeSessions: 0, workClass: 'maintenance',
  },
]
```

Check `TIER_LABEL` in `vocab.ts` has the words `Needs you`, `Blocked`, `In flight`, `Idle` (it is `Record<RoadmapTier, string>`; read it and use ITS words in the tier-line test if they differ).

- [ ] **Step 4: Run to pass**, then the web gate (`typecheck`, `lint` at least; full `test` before commit).

- [ ] **Step 5: Commit**

```bash
printf 'feat(delivery): vocabulary, navigation state and the automation tier rule (Refs #1468)\n\nPure modules for finish-line row 4. Tier words and tones are the roadmap'"'"'s (one table).\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/d1.txt
git add web/src/features/products web/src/features/delivery && git commit -F /tmp/d1.txt
```

---

### Task 2: The list column — rows, scope segment, scope bodies

**Files:**
- Modify: `web/src/api/hooks.ts` (`useStageModels`, `useCheckInsights` options; `useAutomationActionableCounts`)
- Create: `web/src/features/delivery/rows/AutomationRow.tsx`, `rows/CiRepoRow.tsx`, `DeliveryScopeSegment.tsx`, `AutomationsScopeBody.tsx`, `CiScopeBody.tsx`, `DeliveryListColumn.tsx`, `DeliveryListColumn.test.tsx`, `web/src/api/__tests__/deliveryHooks.test.tsx`

**Interfaces:**
- Consumes: Task 1's nav state, tier, fixture.
- Produces (hooks): `useStageModels({ enabled = true, poll = true } = {})` (same query key/fn as today); `useCheckInsights(repo: string | undefined, window: string, { poll = true } = {})` (existing signature stays positional for the first two); `useAutomationActionableCounts(ids: readonly string[], enabled = true): Record<string, number>` — `useQueries` over `QK.automationActionable(id)` / `fetchAutomationActionable(id)`, `staleTime: 10_000`, returns `{ [id]: data?.length ?? 0 }`.
- Produces (column): `interface DeliveryListColumnProps { scope: DeliveryScope; automations: readonly Automation[]; automationsLoading: boolean; automationsError: boolean; repos: readonly string[]; reposLoading: boolean; reposError: boolean; selectedKey: string | null; onOpenAutomation: (id: string) => void; onOpenRepo: (repo: string) => void }`.
- `AutomationRow({ automation, actionable, selected, onOpen })` — `CardFrame` row: `data-testid={`row-${automationKey(id)}`}`, `role="listitem"`, `openLabel={`Open automation ${name}`}`, rail `TIER_ACCENT[tier].rail`, identity = `TierDot` + name + (when `activeSessions > 0`) `Badge count`; trailing = mode `Badge` (`a.mode ?? 'off'`, tone `a.enabled ? 'accent' : 'neutral'`) and, when `a.workClass`, a neutral `Badge` with `workClass`; line 2 = `automationTierLine(...)` then ` · ${status.text}`.
- `CiRepoRow({ repo, selected, onOpen })` — reads `useCheckInsights(repo, '7d', { poll: false })`; identity = repo name; line 2 = `${flakiest.length} flaky · slowest ${slowest[0].check_name} ${fmtSecs(slowest[0].p90)}` or `No check history in 7d` while empty, `Loading…` while pending. Rail `bg-neutral/40`. (`fmtSecs` comes from Task 4's `ci/format.ts` — create it in THIS task if Task 4 has not run: `web/src/features/delivery/ci/format.ts` with the `fmtSecs` body from `InsightsView.tsx`, and Task 4 keeps it.)
- `DeliveryScopeSegment({ scope, automationCount, ciCount })` — copy of `RoadmapScopeSegment` with two links to `deliveryPath('automations')` / `deliveryPath('ci')`, labels `DELIVERY.automations` / `DELIVERY.ci`, `aria-label="Delivery scope"`.
- `AutomationsScopeBody({ automations, isLoading, isError, selectedKey, onOpen })` — `ScopeBody` with a `ListSearchInput` in controls; rows sorted by `AUTOMATION_TIER_RANK` then name; `role="list"` `aria-label={DELIVERY.automations}`; `Skeleton` ×3 while loading and empty; `EmptyState title="No automations registered"`; `Banner tone="danger" title="Could not load automations."` on error.
- `CiScopeBody({ repos, isLoading, isError, selectedKey, onOpen })` — same frame; rows sorted by name; `EmptyState title="No repositories with pipeline history yet"`.

- [ ] **Step 1: Failing tests**

`web/src/api/__tests__/deliveryHooks.test.tsx`:

```tsx
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return { ...actual, api: { ...actual.api, get: vi.fn(async (path: string): Promise<unknown> => {
    if (path.endsWith('/actionable')) return path.includes('pr-doctor') ? [{ id: 'o/r#1', title: 't', actions: [] }] : []
    if (path === '/pipeline/repos') return { 'o/r': { repo: 'o/r', nodes: [] } }
    return { repo: 'o/r', window: '7d', slowest: [], flakiest: [], regressions: [], runner_wait_health: [] }
  }) } }
})
import { api } from '@/api/client'
import { useAutomationActionableCounts, useCheckInsights, useStageModels } from '@/api/hooks'

const get = vi.mocked(api.get)
function wrap() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

describe('delivery hooks', () => {
  beforeEach(() => get.mockClear())
  it('useAutomationActionableCounts fetches one /actionable per id and counts', async () => {
    const { result } = renderHook(() => useAutomationActionableCounts(['pr-doctor', 'backlog-runner']), { wrapper: wrap() })
    await waitFor(() => expect(result.current).toEqual({ 'pr-doctor': 1, 'backlog-runner': 0 }))
    expect(get.mock.calls.map(([p]) => p).sort()).toEqual(['/automations/backlog-runner/actionable', '/automations/pr-doctor/actionable'])
  })
  it('useAutomationActionableCounts asks for nothing while disabled', async () => {
    const { result } = renderHook(() => useAutomationActionableCounts(['pr-doctor'], false), { wrapper: wrap() })
    expect(result.current).toEqual({ 'pr-doctor': 0 })
    await new Promise((r) => setTimeout(r, 20))
    expect(get).not.toHaveBeenCalled()
  })
  it('useStageModels honours enabled', async () => {
    renderHook(() => useStageModels({ enabled: false }), { wrapper: wrap() })
    await new Promise((r) => setTimeout(r, 20))
    expect(get).not.toHaveBeenCalled()
  })
  it('useCheckInsights fetches the repo+window once without a poll timer', async () => {
    const { result } = renderHook(() => useCheckInsights('o/r', '7d', { poll: false }), { wrapper: wrap() })
    await waitFor(() => expect(result.current.data?.repo).toBe('o/r'))
    expect(get).toHaveBeenCalledWith('/pipeline/insights/checks?repo=o%2Fr&window=7d')
  })
})
```

`DeliveryListColumn.test.tsx`:

```tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { AUTOMATIONS } from './__fixtures__/automations'
import { DeliveryListColumn } from './DeliveryListColumn'
import { automationKey, ciRepoKey } from './deliveryNavState'

vi.mock('@/api/hooks', () => ({
  useAutomationActionableCounts: () => ({ 'pr-doctor': 0, 'backlog-runner': 2, 'pm-classify': 0 }),
  useCheckInsights: (repo: string) => ({
    data: repo === 'o/r'
      ? { repo, window: '7d', slowest: [{ check_name: 'e2e', event: 'pull_request', p50: 100, p90: 400, ratio: 4, n: 9 }],
          flakiest: [{ check_name: 'ws', event: 'pull_request', flake_pct: 20, flaky_commits: 2, total_commits: 10, n: 10 }],
          regressions: [], runner_wait_health: [] }
      : undefined,
    isLoading: false, isError: false,
  }),
}))

function mount(over: Partial<React.ComponentProps<typeof DeliveryListColumn>> = {}) {
  const onOpenAutomation = vi.fn(); const onOpenRepo = vi.fn()
  render(
    <MemoryRouter>
      <DeliveryListColumn scope="automations" automations={AUTOMATIONS} automationsLoading={false} automationsError={false}
        repos={['o/r', 'a/b']} reposLoading={false} reposError={false} selectedKey={null}
        onOpenAutomation={onOpenAutomation} onOpenRepo={onOpenRepo} {...over} />
    </MemoryRouter>,
  )
  return { onOpenAutomation, onOpenRepo }
}

describe('DeliveryListColumn', () => {
  it('Automations scope: one row per automation, sorted by tier then name, with the tier line', () => {
    mount()
    const list = screen.getByRole('list', { name: 'Automations' })
    const names = within(list).getAllByRole('button', { name: /^Open automation/ }).map((b) => b.getAttribute('aria-label'))
    // backlog-runner has 2 actionable → needs-you first; pr-doctor warn+enabled → blocked; pm-classify idle
    expect(names).toEqual(['Open automation Backlog runner', 'Open automation PR doctor', 'Open automation PM classify'])
    expect(screen.getByText(/Needs you · 2 actionable/)).toBeInTheDocument()
    expect(screen.getByText(/Blocked · 2 PRs failing/)).toBeInTheDocument()
  })
  it('opens an automation and highlights the selected row', () => {
    const { onOpenAutomation } = mount({ selectedKey: automationKey('pr-doctor') })
    fireEvent.click(screen.getByRole('button', { name: 'Open automation PR doctor' }))
    expect(onOpenAutomation).toHaveBeenCalledWith('pr-doctor')
    expect(screen.getByTestId(`row-${automationKey('pr-doctor')}`)).toHaveAttribute('aria-current', 'true')
  })
  it('CI health scope: one row per repo with the 7-day meta line', () => {
    const { onOpenRepo } = mount({ scope: 'ci' })
    const list = screen.getByRole('list', { name: 'CI health' })
    expect(within(list).getAllByRole('listitem')).toHaveLength(2)
    expect(screen.getByText('1 flaky · slowest e2e 6m 40s')).toBeInTheDocument()
    expect(screen.getByText('No check history in 7d')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Open CI health o/r' }))
    expect(onOpenRepo).toHaveBeenCalledWith('o/r')
    expect(screen.getByTestId(`row-${ciRepoKey('a/b')}`)).toBeInTheDocument()
  })
  it('the scope segment links both scopes and counts them', () => {
    mount()
    expect(screen.getByRole('link', { name: 'Automations (3)' })).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('link', { name: 'CI health (2)' })).toHaveAttribute('href', '/delivery?scope=ci')
  })
  it('search narrows the Automations scope', () => {
    mount()
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'doctor' } })
    expect(screen.getAllByRole('button', { name: /^Open automation/ })).toHaveLength(1)
  })
  it('states: loading skeletons, empty, error', () => {
    mount({ automations: [], automationsLoading: true })
    expect(screen.getAllByTestId('skeleton').length).toBeGreaterThan(0)
  })
})
```

(`aria-current` on the row wrapper: set `aria-current={selected ? 'true' : undefined}` on the `listitem` div, since `aria-selected` is invalid on a listitem. `ListSearchInput` renders `role="searchbox"` — verify by reading `web/src/components/ListSearchInput.tsx`; use whatever role/label it renders. `Skeleton` renders `data-testid="skeleton"`? Read `web/src/ui/Skeleton.tsx`; adjust the assertion to what it renders.)

- [ ] **Step 2: Run to fail**

Run: `(cd web && npx vitest run src/api/__tests__/deliveryHooks.test.tsx src/features/delivery)`

- [ ] **Step 3: Hooks**

In `hooks.ts` change:

```ts
export function useStageModels({ enabled = true, poll = true }: { enabled?: boolean; poll?: boolean } = {}) {
  return useQuery<Record<string, StageModel>>({
    queryKey: QK.pipelineRepos,
    queryFn: () => api.get<Record<string, StageModel>>('/pipeline/repos'),
    staleTime: 15_000,
    refetchInterval: poll ? 30_000 : false,
    enabled,
  })
}

export function useCheckInsights(repo: string | undefined, window: string, { poll = true }: { poll?: boolean } = {}) {
  return useQuery<CheckInsights>({
    queryKey: QK.pipelineInsights(repo, window),
    queryFn: () =>
      api.get<CheckInsights>(
        `/pipeline/insights/checks?repo=${encodeURIComponent(repo as string)}&window=${window}`),
    staleTime: 15_000,
    refetchInterval: poll ? 30_000 : false,
    enabled: !!repo,
  })
}

/** Actionable-item counts for many automations at once — the Delivery list's
 *  tier input (finish-line §4.4). One `/actionable` read per id, no poll: the
 *  detail page's own `useAutomationActionable` shares each key and refreshes
 *  it on every trigger. */
export function useAutomationActionableCounts(ids: readonly string[], enabled = true): Record<string, number> {
  const results = useQueries({
    queries: ids.map((id) => ({
      queryKey: QK.automationActionable(id),
      queryFn: () => fetchAutomationActionable(id),
      enabled, staleTime: 10_000,
    })),
  })
  const out: Record<string, number> = {}
  ids.forEach((id, i) => { out[id] = results[i]?.data?.length ?? 0 })
  return out
}
```

(`useQueries` from `@tanstack/react-query`; add to the import line. `useStageModels` existing callers: `navDefs.ts` mentions it only in a comment; `InsightsView` is deleted in Task 5; `web/src/api/__tests__/useStageModels.test.ts` calls it with no args — unchanged.)

- [ ] **Step 4: Rows, segment, bodies, column** — implement per the Interfaces block, modelled line-for-line on `features/roadmap/rows/InitiativeCard.tsx` (row), `RoadmapScopeSegment.tsx` (segment), `inbox/InboxScopeBody.tsx` (body with `ScopeBody` + `ListSearchInput` — no keys, no bulk bar) and `RoadmapListColumn.tsx` (column: segment on top, error banner, one body per scope). `AutomationsScopeBody` reads `useAutomationActionableCounts(automations.map(a => a.id))` once and hands each row its count. `CiScopeBody` renders `CiRepoRow`s, each reading its own `useCheckInsights`.

- [ ] **Step 5: Run to pass; web gate; commit**

```bash
printf 'feat(delivery): list column — Automations and CI health scopes on the chassis (Refs #1468)\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/d2.txt
git add web/src/api web/src/features/delivery && git commit -F /tmp/d2.txt
```

---

### Task 3: The automation detail — header controls and five tabs

**Files:**
- Create: `web/src/features/delivery/automation/actions.tsx`, `EventsList.tsx`, `SessionsList.tsx`, `ConfigPanel.tsx`, `AutomationControls.tsx`, `AutomationDetail.tsx`, `AutomationDetail.test.tsx`
- Create: `web/src/lib/useSearchParamState.ts` (+ `useSearchParamState.test.ts`); Modify: `web/src/features/roadmap/useTabParam.ts` to wrap it.
- Reference (read, do not modify): `web/src/automations/AutomationsTab.tsx` (the monitor pieces to move), `web/src/components/AutomationCard.tsx` (ConfigPanel, SchedulePanel, engineBadge, enable/mode logic to move). Both are deleted in Tasks 5 and 6; this task RE-HOMES their logic verbatim in behaviour, on tokens.

**Interfaces:**
- `useSearchParamState<T extends string>(name: string, allowed: readonly T[], fallback: T): [T, (t: T) => void]` — `?name=` as state, `replace` navigation; `useTabParam(allowed, fallback)` becomes `useSearchParamState('tab', allowed, fallback)`.
- `AutomationControls({ automation })` — the header `actions`: when `modes.length > 2` a `Select aria-label="Mode"` over `modes` with the `MODE_DESCRIPTION[mode]` as `title`; when not `operatorTriggered` (`a.operatorTriggered ?? a.modes.length === 0`) a `Switch aria-label={`${a.name} enabled`}` whose `onChange` mirrors `AutomationCard`'s toggle (enabling a 2-mode automation with no active mode also sets the first non-`off` mode). Both call `usePatchAutomation().mutate({ id, body })`.
- `AutomationActions({ automationId, enabled })` + `ActionButton` — moved from `AutomationsTab` with the async-job polling (`pollJob`, `PHASE_LABEL`, `JOB_TERMINAL`, `JOB_POLL_MS`, `outcomeToast`) unchanged; classes → `Button size="sm" variant={action.style === 'risky' ? 'danger' : 'secondary'}` (read `ui/Button.tsx` for the real variant names) and outcome badge → `Badge tone={ok ? 'success' : warn ? 'warning' : 'danger'}`.
- `EventsList({ automationId, onOpenSession })` — `useAutomationEvents(id)`; the list from `AutomationLiveCard`'s Activity section; session-linked rows are `Button variant="link"` (or `<button className="text-accent hover:underline">`).
- `SessionsList({ automation })` — `useAutomationSessions(id)`; each row a react-router `Link` to `INACTIVE_SESSION_STATUSES.has(s.status) ? archivedSessionPath(s.id) : sessionPath(s.id, a.workClass === 'maintenance' ? 'background' : 'live')`; header "N active".
- `ConfigPanel({ automation })` — `AutomationCard`'s `ConfigPanel`/`ConfigInput`/`RepoMultiselect`/`RepoNumberMap` on `Field` + `Input`/`Select`/`Checkbox`; same live-patch-on-change behaviour.
- `AutomationDetail({ automation, onOpenSession })` — `Tabs` (`label="Automation"`, items from `AUTOMATION_TABS` minus `schedule` when `operatorTriggered`, minus `config` when `configSchema.length === 0`), `[tab, setTab] = useSearchParamState('tab', items, 'status')`; panels: **status** = description, status line with `TierDot`, `AutomationActions`, plus `ConsolidatePanel` for `pr-doctor` and `BacklogGroupsPanel` + "Custom dispatch…" `ManualDispatchDialog` for `backlog-runner` (exactly as `AutomationLiveCard` did); **schedule** = the `SchedulePanel` copy (`ScheduleEditor` + `usePatchAutomation`); **events** = `EventsList`; **sessions** = `SessionsList`; **config** = `ConfigPanel`. Panel `id`s via `panelId={(id) => `automation-panel-${id}`}`.

- [ ] **Step 1: Failing tests**

`useSearchParamState.test.ts` — replace `?tab=`-specific cases if `useTabParam` had tests (`grep -rn useTabParam web/src --include=*.test.*`); add:

```ts
it('reads ?window=, falls back when unknown, and replaces on set', () => { /* renderHook inside MemoryRouter at '/x?window=30d'; expect '30d'; set('7d'); expect location.search '?window=7d' and history length unchanged */ })
```

`AutomationDetail.test.tsx`:

```tsx
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { AUTOMATIONS } from '../__fixtures__/automations'
import { AutomationDetail } from './AutomationDetail'
import { AutomationControls } from './AutomationControls'

const patch = vi.fn(); const trigger = vi.fn().mockResolvedValue({ status: 'done', level: 'ok', message: 'Fixed' })
vi.mock('@/api/hooks', () => ({
  usePatchAutomation: () => ({ mutate: patch, isPending: false }),
  useAutomationEvents: () => ({ data: [{ ts: '2026-09-11T10:00:00Z', level: 'info', kind: null, summary: 'scanned 3 PRs', target: null, sessionId: 's1' }] }),
  useAutomationSessions: () => ({ data: [{ id: 's1', name: 'fix #1', status: 'in_progress', originRef: null }, { id: 's2', name: 'old', status: 'ended', originRef: null }] }),
  useAutomationActionable: () => ({ data: [{ id: 'o/r#1', title: 'CI red on #1', actions: [{ kind: 'fix', label: 'Fix', style: 'safe' }] }] }),
  useAutomationTrigger: () => ({ mutateAsync: trigger, isPending: false }),
  useGithubRepos: () => ({ data: [{ nameWithOwner: 'o/r' }], isLoading: false }),
  useConsolidation: () => ({ data: undefined }), useConsolidate: () => ({ mutate: vi.fn(), isPending: false }),
}))
vi.mock('@/components/ConsolidatePanel', () => ({ ConsolidatePanel: () => <div data-testid="consolidate" /> }))
vi.mock('@/components/BacklogGroupsPanel', () => ({ BacklogGroupsPanel: () => <div data-testid="groups" /> }))
vi.mock('@/components/ManualDispatchDialog', () => ({ ManualDispatchDialog: () => null }))

const prDoctor = AUTOMATIONS[0]
const mount = (a = prDoctor, path = '/delivery/automations/pr-doctor') => {
  const onOpenSession = vi.fn()
  render(<MemoryRouter initialEntries={[path]}><AutomationDetail automation={a} onOpenSession={onOpenSession} /></MemoryRouter>)
  return { onOpenSession }
}

describe('AutomationDetail', () => {
  beforeEach(() => { patch.mockReset(); trigger.mockClear() })
  it('Status tab: actionable items with their actions, and the pr-doctor consolidation panel', async () => {
    mount()
    expect(screen.getByRole('tab', { name: 'Status', selected: true })).toBeInTheDocument()
    expect(screen.getByText('CI red on #1')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Fix' }))
    await waitFor(() => expect(trigger).toHaveBeenCalledWith({ item_id: 'o/r#1', kind: 'fix' }))
    expect(screen.getByTestId('consolidate')).toBeInTheDocument()
  })
  it('tabs are URL state', () => {
    mount(prDoctor, '/delivery/automations/pr-doctor?tab=events')
    expect(screen.getByRole('tab', { name: 'Events', selected: true })).toBeInTheDocument()
    expect(screen.getByText('scanned 3 PRs')).toBeInTheDocument()
  })
  it('Sessions tab links live sessions by work class and ended ones to the archive', () => {
    mount(prDoctor, '/delivery/automations/pr-doctor?tab=sessions')
    expect(screen.getByRole('link', { name: /fix #1/ })).toHaveAttribute('href', '/sessions/background/s1')
    expect(screen.getByRole('link', { name: /old/ })).toHaveAttribute('href', '/sessions/archived/s2')
  })
  it('Config tab renders the schema and patches on change', () => {
    mount(prDoctor, '/delivery/automations/pr-doctor?tab=config')
    fireEvent.click(screen.getByRole('checkbox', { name: 'Auto-fix' }))
    expect(patch).toHaveBeenCalledWith({ id: 'pr-doctor', body: { config: expect.objectContaining({ auto_fix: false }) } })
  })
  it('an operator-triggered automation has no Schedule tab and no switch', () => {
    mount(AUTOMATIONS[2], '/delivery/automations/pm-classify')
    expect(screen.queryByRole('tab', { name: 'Schedule' })).not.toBeInTheDocument()
    render(<MemoryRouter><AutomationControls automation={AUTOMATIONS[2]} /></MemoryRouter>)
    expect(screen.queryByRole('switch')).not.toBeInTheDocument()
  })
  it('controls: mode select and enable switch patch the automation', () => {
    render(<MemoryRouter><AutomationControls automation={prDoctor} /></MemoryRouter>)
    fireEvent.change(screen.getByRole('combobox', { name: 'Mode' }), { target: { value: 'propose' } })
    expect(patch).toHaveBeenCalledWith({ id: 'pr-doctor', body: { mode: 'propose' } })
    fireEvent.click(screen.getByRole('switch', { name: 'PR doctor enabled' }))
    expect(patch).toHaveBeenCalledWith({ id: 'pr-doctor', body: { enabled: false } })
  })
})
```

- [ ] **Step 2: Run to fail.**

- [ ] **Step 3: Implement** — `useSearchParamState` is `useTabParam`'s body with `'tab'` replaced by `name`; `useTabParam` becomes `export const useTabParam = <T extends string>(allowed: readonly T[], fallback: T) => useSearchParamState('tab', allowed, fallback)`. Move the code named in Interfaces; keep every comment that explains a WHY (async job phases #668, auto-disarm #466, the 2-mode enable rule, the `RepoNumberMap` empty-input rule). `AutomationDetail`:

```tsx
export function AutomationDetail({ automation: a, onOpenSession }: { automation: Automation; onOpenSession: (id: string) => void }) {
  const operatorTriggered = a.operatorTriggered ?? a.modes.length === 0
  const items = AUTOMATION_TABS.filter((t) => !(t === 'schedule' && operatorTriggered) && !(t === 'config' && a.configSchema.length === 0))
  const [tab, setTab] = useSearchParamState<AutomationTab>('tab', items, 'status')
  const [customDispatch, setCustomDispatch] = useState(false)
  return (
    <>
      <Tabs label="Automation" items={items.map((id) => ({ id, label: AUTOMATION_TAB[id] }))} value={tab} onChange={(id) => setTab(id as AutomationTab)} panelId={(id) => `automation-panel-${id}`} className="mb-4" />
      <div role="tabpanel" id={`automation-panel-${tab}`} aria-labelledby={`tab-${tab}`} className="space-y-4">
        {tab === 'status' && (<>
          {a.description && <p className="text-sm text-ink-muted">{a.description}</p>}
          <AutomationActions automationId={a.id} enabled />
          {a.id === 'pr-doctor' && <ConsolidatePanel enabled onOpenSession={onOpenSession} />}
          {a.id === 'backlog-runner' && (<>
            <div className="flex justify-end"><Button size="sm" variant="secondary" onClick={() => setCustomDispatch(true)}>Custom dispatch…</Button></div>
            <BacklogGroupsPanel enabled onOpenSession={onOpenSession} />
            <ManualDispatchDialog open={customDispatch} onClose={() => setCustomDispatch(false)} onOpenSession={onOpenSession} />
          </>)}
        </>)}
        {tab === 'schedule' && <SchedulePanel automation={a} />}
        {tab === 'events' && <EventsList automationId={a.id} onOpenSession={onOpenSession} />}
        {tab === 'sessions' && <SessionsList automation={a} />}
        {tab === 'config' && <ConfigPanel automation={a} />}
      </div>
    </>
  )
}
```

(`Tabs` emits `id="tab-<id>"` only when `panelId` is given — it is. Read `ui/Tabs.tsx` for the exact attribute names before writing the panel's `aria-labelledby`.)

- [ ] **Step 4: Run to pass; web gate; commit**

```bash
printf 'feat(delivery): automation detail — header controls and Status/Schedule/Events/Sessions/Config tabs (Refs #1468)\n\nRe-homes the AutomationsTab monitor and the AutomationCard configuration onto ui/ primitives; behaviour unchanged.\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/d3.txt
git add web/src/lib web/src/features && git commit -F /tmp/d3.txt
```

---

### Task 4: The CI health detail

**Files:**
- Create: `web/src/features/delivery/ci/format.ts` (if Task 2 did not), `ci/CheckTable.tsx`, `ci/CheckTable.test.tsx` (moved from `components/CheckTable.test.tsx` — `git mv`, then fix the import), `ci/CiRepoDetail.tsx`, `ci/CiRepoDetail.test.tsx`
- Delete: `web/src/components/CheckTable.tsx` (after the move; `InsightsView` still imports it until Task 5 — so in THIS task change `InsightsView.tsx`'s import to `@/features/delivery/ci/CheckTable` and its `fmtSecs` to the moved one; Task 5 deletes the file).

**Interfaces:**
- `fmtSecs(s: number): string` — verbatim from `InsightsView`.
- `CheckTable<T>({ title, columns, rows, empty })` — same props; classes: `section` `rounded-md border border-edge bg-surface`, `h3` `border-b border-edge px-3 py-2 text-sm font-semibold text-ink`, header row `text-ink-muted`, body rows `border-t border-edge`, empty `text-xs text-ink-muted`.
- `WINDOWS = ['24h', '7d', '14d', '30d'] as const`; `type CiWindow`.
- `CiRepoDetail({ repo, window })` — `useCheckInsights(repo, window)` (polling); `Banner` on error; `Skeleton` while loading with no data; the four `CheckTable`s exactly as `InsightsView` renders them (same titles, columns, empty copy).
- The window `Select` lives in the PAGE header (`DeliveryPage`, Task 5) via `useSearchParamState('window', WINDOWS, '7d')`; export `WINDOWS` from `CiRepoDetail.tsx` for it.

- [ ] **Step 1: Failing test** `CiRepoDetail.test.tsx`: mock `@/api/hooks` `useCheckInsights` to return a payload with one row per table; assert the four headings, `fmtSecs` formatting (`6m 40s`), the regression `Slowdown` cell `2.0×`, and the runner-wait `↑ 10s` delta; a second case with `isError: true` shows the danger banner.
- [ ] **Step 2: Run to fail.** **Step 3: Implement.** **Step 4: Pass; gate; commit**

```bash
printf 'feat(delivery): CI health detail — the four check tables on tokens (Refs #1468)\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/d4.txt
git add -A web/src/features/delivery web/src/components && git commit -F /tmp/d4.txt
```

---

### Task 5: Shell wiring — the `delivery` section replaces `automations` and `insights`

**Files:**
- Modify: `web/src/shell/navDefs.ts`, `navDefs.test.ts`, `useNavBadges.ts`, `useNavBadges.test.tsx`, `openItems.ts`, `listColumns.tsx`, `listColumns.test.tsx`, `routes.tsx`, `routes.test.tsx`, `AppShell.tsx`, `kiosk.test.ts`, `kiosk.integration.test.tsx`, `roadmapWorkspace.test.tsx`, `web/src/App.test.tsx`, `web/src/pipeline/PipelineTab.tsx`, `web/src/pipeline/__tests__/PipelineTab.test.tsx`, `web/src/features/settings/pageContract.test.tsx`
- Create: `web/src/features/delivery/DeliveryWelcome.tsx`, `useDeliveryWorkspace.ts`, `useDeliveryWorkspace.test.tsx`, `DeliveryPage.tsx`, `DeliveryPage.test.tsx`
- Delete: `web/src/automations/AutomationsTab.tsx`, `web/src/automations/AutomationsTab.test.tsx` (and the `web/src/automations/` dir if empty), `web/src/components/InsightsView.tsx`, `InsightsView.test.tsx`, `InsightsTab.test.tsx`

**Interfaces:**
- `NavBadgeKey`: `'sessions' | 'pipeline' | 'roadmap' | 'delivery' | 'settings'`. `DEFS` rows: sessions, pipeline, roadmap, **delivery** (`icon: Workflow`, `path: DELIVERY_PATH`, `badge: 'delivery'`, `showInKiosk: false`, `hasList: true`, `help: DELIVERY.help`), settings. Drop the `LineChart` import. The `showInKiosk` docstring's "Two sections qualify today" paragraph becomes one section (`pipeline`) and names R5's follow-up.
- `useNavBadges`: `delivery: automationsActiveSessions || undefined` (R3).
- `OpenItem` gains `| DeliveryOpenItem`; `OPEN_ITEM_RESOLVERS.delivery = deliveryOpenItem`.
- `LIST_SECTIONS = ['sessions', 'roadmap', 'delivery']`; `ListColumnSources.delivery: DeliveryListColumnProps`; `case 'delivery'`.
- `useDeliveryWorkspace(active: boolean): { list: DeliveryListColumnProps }` — `useAutomations()` (already polled app-wide by the badge — no gate, no second timer), `useStageModels({ enabled: active, poll: false })` (the CI detail owns the polling copy), `scope = deliveryScope(pathname, search)`, `selectedKey = keyForDeliveryItem(deliveryOpenItem(params, new URLSearchParams(search)))`, `onOpenAutomation = (id) => navigate(automationPath(id))`, `onOpenRepo = (repo) => navigate(ciRepoPath(repo))`.
- `AppShell`: `const delivery = useDeliveryWorkspace(def?.id === 'delivery')`; sources gain `delivery: delivery.list`.
- `DeliveryWelcome({ scope, count })` — `EmptyState`: automations → title "Pick an automation", description `${count} registered — open one to see what it is doing, what it needs from you, and how it is configured.`; ci → "Pick a repository", `${count} with pipeline history`; count 0 → "No automations registered" / "No repositories with pipeline history yet".
- `DeliveryPage` — `data-page-variant="standard"`; item from `deliveryOpenItem(useParams(), search)`; **automation**: `useAutomations()` (`poll` irrelevant — same key) → find by id; title = name (slug while loading); crumbs Delivery › Automations › name; `actions={<AutomationControls automation={a} />}`; body `AutomationDetail`; unknown id after load → `EmptyState title="Automation not found"` with a link to `deliveryPath()`. **ciRepo**: title = repo; crumbs Delivery › CI health › repo; `[window, setWindow] = useSearchParamState('window', WINDOWS, '7d')`; `actions={<Select aria-label="Window" value={window} onChange=…>{WINDOWS}</Select>}`; body `CiRepoDetail`. **nothing open**: title `DELIVERY.label`, body `DeliveryWelcome` with the scope's count (`useAutomations().data?.length`, `Object.keys(useStageModels({ poll: false }).data ?? {}).length`). `PageContent busy={loading && !data}`. `onOpenSession` = `navigate(sessionPath(id))`.
- `routes.tsx`: delete `AutomationsTab`/`InsightsView` imports, `AutomationsPage`, `InsightsPage`, `INSIGHTS_PATH`; add `{ path: DELIVERY_PATH, element: <DeliveryPage /> }, { path: AUTOMATION_ROUTE, element: <DeliveryPage /> }, { path: CI_REPO_ROUTE, element: <DeliveryPage /> }`; legacy: `{ path: '/automations', element: <Navigate to={deliveryPath()} replace /> }, { path: '/insights', element: <Navigate to={deliveryPath('ci')} replace /> }`. `PipelinePage`: `<PipelineTab onOpenCiHealth={() => navigate(deliveryPath('ci'))} />`.
- `PipelineTab`: prop `onOpenInsights` → `onOpenCiHealth` (and `laneJumpTarget`'s `{ kind: 'insights' }` → `{ kind: 'ci-health' }`); update its test.

- [ ] **Step 1: Update the pinned tests first** (they define the target):
  - `navDefs.test.ts`: order `[['sessions','Sessions'],['pipeline','Pipeline'],['roadmap','Roadmap'],['delivery','Delivery'],['settings','Settings']]`; list owners `['sessions','roadmap','delivery']`; kiosk set `['pipeline']`; known badge keys `sessions, pipeline, roadmap, delivery, settings`; add `it('the delivery section path is the one deliveryNavState builds from')` mirroring the roadmap one; add `expect(NAV_DEFS.map((d) => d.id)).not.toContain('automations')` and `'insights'` to the "not a section" test.
  - `kiosk.test.ts`: `?pages=pipeline,insights` → `['pipeline']` (insights is gone); dedupe case → `?pages=pipeline,pipeline` → `['pipeline']`; the "full kiosk URL" case → `pages: ['pipeline']`.
  - `kiosk.integration.test.tsx`: `grep -n insights` and replace any `?pages=…insights` with `pipeline`.
  - `useNavBadges.test.tsx`: `result.current.automations` → `result.current.delivery`; the test name "automations counts its own data" → "delivery counts live automation sessions".
  - `roadmapWorkspace.test.tsx`: in both `it.each` lists replace `'/automations', '/insights'` with `'/delivery'`.
  - `App.test.tsx`: "clicking the Automations rail item" → `Delivery` link, `/delivery`.
  - `listColumns.test.tsx`: expects `hasList` ids to equal `LIST_SECTIONS` — unchanged assertion, new data.
  - `routes.test.tsx`: delete the two mocks; add `vi.mock('@/features/delivery/DeliveryPage', () => ({ DeliveryPage: stub('delivery') }))`; redirects add `['/automations', '/delivery'], ['/insights', '/delivery?scope=ci']`; pages table: remove `/automations`, `/insights`, add `['/delivery','page-delivery'], ['/delivery/automations/pr-doctor','page-delivery'], ['/delivery/ci/o/r','page-delivery']`; the "owns a vertical scroll container" list becomes `['/sessions/archived']` only (Delivery is a `PageContent` page like Roadmap); `MATRIX_PATHS`: same replacement (16 → 17 rows; comment "The 17 non-redirect paths"); `REDIRECT_ROUTE_PATTERNS` add `'/automations', '/insights'`; cell count `64` → `68`; refused-cells count in that block: recompute (was 14: remove `/automations` (refused) and `/insights` (allowed) → 13, add three refused Delivery rows → 16) and update the comment.
  - `pageContract.test.tsx`: rows `{ nav: 'delivery', path: '/delivery', variant: 'standard', title: 'Delivery' }`, `{ nav: 'delivery', path: '/delivery?scope=ci', variant: 'standard', title: 'Delivery' }`, `{ nav: 'delivery', path: '/delivery/automations/pr-doctor', variant: 'standard', title: 'PR doctor' }`, `{ nav: 'delivery', path: '/delivery/ci/o/r', variant: 'standard', title: 'o/r' }`; mock: `if (path === '/automations') return AUTOMATIONS` (import from the fixture inside the factory with `await import`), `if (path.startsWith('/automations/')) return []`, `if (path === '/pipeline/repos') return { 'o/r': { repo: 'o/r', nodes: [] } }`, `if (path.startsWith('/pipeline/insights/checks')) return { repo: 'o/r', window: '7d', slowest: [], flakiest: [], regressions: [], runner_wait_health: [] }`. Also remove any `/automations` or `/insights` rows that exist.
  - `PipelineTab.test.tsx`: `onOpenInsights` → `onOpenCiHealth`, `kind: 'insights'` → `'ci-health'`.

- [ ] **Step 2: Run those files to see them fail**

Run: `(cd web && npx vitest run src/shell src/App.test.tsx src/features/settings/pageContract.test.tsx src/pipeline)`

- [ ] **Step 3: Wire** everything in the Interfaces block, following `2026-09-08-roadmap-phase3-products-master-detail.md` Task 6 Steps 3–9 line for line (nav row → openItems → listColumns → workspace hook → AppShell → page → routes). `useDeliveryWorkspace.test.tsx` mirrors `useRoadmapWorkspace.test.tsx`: `active: false` fetches no `/pipeline/repos`; `active: true` fetches it once with no poll (advance timers 31 s, still one call); `/automations` is fetched regardless (badge key).

`DeliveryPage.test.tsx` (mock `@/api/hooks`: `useAutomations` → fixture, `useStageModels` → `{ data: { 'o/r': {...} } }`, `useCheckInsights` → empty payload, plus everything `AutomationDetail`/`AutomationControls` mock in Task 3): at `/delivery` h1 "Delivery" and "Pick an automation" with "3 registered"; at `/delivery?scope=ci` "Pick a repository"; at `/delivery/automations/pr-doctor` h1 "PR doctor", the `Mode` combobox and the `PR doctor enabled` switch in the header, the Status tab; at `/delivery/automations/nope` "Automation not found"; at `/delivery/ci/o/r?window=30d` h1 "o/r" and the `Window` select valued `30d`, changing it to `7d` rewrites the URL.

- [ ] **Step 4: Delete** `AutomationsTab.tsx` (+ test), `InsightsView.tsx` (+ its two tests); `grep -rn "AutomationsTab\|InsightsView\|INSIGHTS_PATH\|onOpenInsights\|NAV_BY_ID.automations\|NAV_BY_ID.insights\|'automations'" web/src` and fix every survivor (expected: `features/assistant/starters.ts` may link `/automations` — point it at `deliveryPath()`; `HelpSheet` reads `NAV_DEFS`, nothing to do).

- [ ] **Step 5: Full web gate.** Expect fallout in `vi.mock('@/api/hooks')` files that render `AppShell`/`renderApp` without `useStageModels`/`useAutomationActionableCounts`: add `useStageModels: () => ({ data: undefined, isLoading: false, isError: false })` and `useAutomationActionableCounts: () => ({})` to each that fails (`grep -rln "vi.mock('@/api/hooks'" web/src`).

- [ ] **Step 6: Commit**

```bash
printf 'feat(delivery): one Delivery section replaces Automations and Insights — nav, routes, list column, page (Refs #1468)\n\nFinish-line spec §4.1–§4.2, §4.4. /automations and /insights redirect. Deletes AutomationsTab and InsightsView.\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/d5.txt
git add -A web/src && git commit -F /tmp/d5.txt
```

---

### Task 6: Close out — retire Settings › Automations, docs, e2e smoke, gates

**Files:**
- Delete: `web/src/components/AutomationView.tsx`, `web/src/components/AutomationCard.tsx`, `web/src/components/AutomationCard.test.tsx`, `web/src/components/__tests__/automationTab.test.tsx`
- Modify: `web/src/features/settings/settingsDefs.ts` (drop the `automation` row and the `AutomationView` import; the R10 comment goes with it), `settingsDefs.test.ts` (drop `'automation'` from the legacy list; add `expect(isSettingsSectionId('automation')).toBe(false)` with a one-line comment pointing at `/delivery`), `SettingsPage.test.tsx` (the `/settings/automation` + `/automation` case → both land on `/delivery`), `web/src/shell/routes.tsx` (`'/automation'` → `deliveryPath()`; add `{ path: '/settings/automation', element: <Navigate to={deliveryPath()} replace /> }` above `${SETTINGS_PATH}/:section`), `routes.test.tsx` (redirect rows `['/automation','/delivery'], ['/settings/automation','/delivery']`; `REDIRECT_ROUTE_PATTERNS` add `'/settings/automation'`), `docs/superpowers/specs/2026-09-08-finish-line-persona-modes-design.md` (§5 row 4: append "— landed <date> (Closes #1468)" the way row 3's line reads), `web/README.md` if it names `automations/`, `InsightsView` or the Automations settings section.
- Create: `web/e2e/delivery.spec.ts`.

- [ ] **Step 1: Failing tests** — the `settingsDefs.test.ts`/`SettingsPage.test.tsx`/`routes.test.tsx` edits above; run them red.
- [ ] **Step 2: Delete + redirect + docs.** `grep -rn "AutomationCard\|AutomationView\|settingsSectionPath('automation')" web/src` → zero.
- [ ] **Step 3: e2e** — `web/e2e/delivery.spec.ts`, same harness as `roadmap.spec.ts` (intercept `**/api/**`; serve `GET /api/automations` → the Task 1 fixture inlined, `GET /api/automations/*/actionable|events|sessions` → `[]`, `GET /api/pipeline/repos` → `{}`, everything else → `[]`/`{}` as `roadmap.spec.ts` does for `/config`, `/sessions`, `/settings`):
  1. desktop: `goto('/delivery')`, click "Open automation PR doctor", expect URL `/delivery/automations/pr-doctor`, click the switch `PR doctor enabled`, expect a `PATCH /api/automations/pr-doctor` with body `{"enabled":false}` (route the PATCH and assert on `request.postDataJSON()`).
  2. phone (390×844): `goto('/delivery')` shows the list and hides the page (`[data-page-variant]` not visible); click the row → page visible, list hidden; browser Back → list again.
- [ ] **Step 4: Gates** — full web gate; `npx playwright test e2e/delivery.spec.ts` (needs `npx playwright install chromium` once; skip with a note if the sandbox has no browser); `ruff check src/ server/` (nothing should change); success criteria from spec §7: `grep -rn "dark:" web/src/features/delivery` → 0, `grep -rnE "bg-(gray|blue|red|amber|green|violet)-" web/src/features/delivery` → 0.
- [ ] **Step 5: Commit**

```bash
printf 'feat(delivery): retire Settings › Automations — the Delivery detail is the one place an automation is configured (Refs #1468)\n\n/automation and /settings/automation redirect to /delivery. Deletes AutomationView and AutomationCard. E2E smoke: toggle + phone hand-off.\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/d6.txt
git add -A web docs && git commit -F /tmp/d6.txt
```

Controller then: full vitest once more from a clean tree, `git push -u origin feat/finish-line-row-4-delivery`, PR (`Fixes #1468`, `Refs #1520`, rulings R1–R7, follow-ups filed for R3 and R5), `gh pr merge --squash --auto`.

## Self-review notes

- Spec coverage: §4.1 nav (T5), §4.2 routes + every redirect (T5, T6), §4.4 list Automations (T2: name, mode pill, status line, work class, tier, sorted by tier then name), §4.4 detail automation (T3: header controls + five tabs, `ScheduleEditor`, `useAutomationSessions` links by work class, `ConfigField`s incl. `eligible_label`), §4.4 list CI (T2: flaky count + slowest check), §4.4 detail repo (T4: four tables, window in `PageHeader.actions` via T5), §4.4 deletes (T5, T6 — the Settings section is R1, beyond the spec's list but named by the code it replaces), §7 page-contract rows (T5), nav pins (T5), redirect table (T5, T6), full gates (every task), Playwright toggle + phone hand-off (T6). Not covered and deferred with rulings: actionable-items nav badge (R3), kiosk CI-health (R5).
- Type consistency: `DeliveryScope`, `DeliveryOpenItem`, `keyForDeliveryItem`, `automationKey`/`ciRepoKey`, `AutomationTier`, `AUTOMATION_TAB`/`AutomationTab`/`AUTOMATION_TABS`, `useSearchParamState`, `WINDOWS`, `DeliveryListColumnProps` are spelled identically across T1–T6.
- Placeholders: T2 Step 4, T4 Step 3 and T5 Step 3 point at a named sibling file to model on rather than repeating ~300 lines of row/column/page code; the Interfaces blocks fix every prop, label, role and test id those files must produce, and the tests in Step 1 of each task are complete.
