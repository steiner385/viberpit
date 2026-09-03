# AppShell & Regions (PR 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the two parallel app shells (`Dashboard.tsx` for narrow screens, `DesktopShell.tsx` for wide) with a single `AppShell` whose regions have named ids, whose placement is a per-mode table, and whose navigation truth is the router in every layout mode.

**Architecture:** `AppShell` is a react-router layout route. It renders eight regions (`app.banners`, `app.rail`, `app.list`, `app.main`, `app.dock`, `app.drawer`, `app.overlays`) and nothing else; a `REGION_VISIBILITY` table decides which appear per `LayoutMode`. One hook, `useViewport()`, derives the mode from `matchMedia` width tiers plus the `?kiosk=1` flag. The existing page components are hosted **unchanged** — this PR moves the frame, not the pages.

**Tech Stack:** React 19.2 · TypeScript 6.0 · Vite 8 · Tailwind v4 (`@theme` tokens from PR 1) · `react-router-dom` 7.18 (already a dependency — `createBrowserRouter` is available, no new deps) · vitest + @testing-library/react · jest-axe · Playwright 1.61.

**Spec:** `docs/superpowers/specs/2026-09-02-ui-shell-workspace-design.md` (sub-spec B §1–§5), which argues from `docs/superpowers/specs/2026-09-02-ui-target-state-design.md` (umbrella §4.2 region table, §6 route table, §8 PR 3 row). **Read both.**

**Scope:** Sub-spec B covers four PRs (3, 4, the shell half of 10, and 12). This plan implements **PR 3 only**. PR 4 (sessions workspace, shortcuts registry, list filters), PR 10 (attract cycle, wake-lock, Playwright viewport projects) and PR 12 (assistant drawer) get their own plans. PR 12 is additionally blocked on a backend spec that does not exist yet.

---

## Global Constraints

Copied verbatim from the specs. Every task's requirements implicitly include this section.

- **One shell.** At the end of this PR, exactly one of `Dashboard.tsx` / `DesktopShell.tsx` / `AppShell.tsx` exists: `AppShell.tsx`. (umbrella §9)
- **Replaced code is deleted in the same PR.** No `@deprecated` shims, no half-dead modules. (umbrella §2.1, CLAUDE.md "Purge replaced code in the same change")
- **Before adding a constant, helper, config list or component, grep for the existing source of truth and route through it.** Adding a parallel "correct" copy *is* the anti-pattern. (CLAUDE.md)
- **A region is a component rendered only by `AppShell`, carrying `data-region="<id>"`.** The shell decides placement and visibility; the region decides content. (umbrella §4.2)
- **URL is state.** Section, session, sub-agent, settings section, dialog, drawer and kiosk all live in the URL. Overlay params are written with `{ replace: true }`, never pushed. Per-browser preferences (widths, theme, filters) are the only client state that survives navigation, all under the `ccswitch.` localStorage namespace. (umbrella §2.4, §6)
- **The router is the single source of navigation truth in every layout mode.** No `useState` tab or selection state. (umbrella D6)
- **`useViewport()` is the only module that reads `matchMedia` for layout.** (umbrella §4.1) Non-layout media queries (`prefers-reduced-motion`, `prefers-color-scheme`, `pointer: coarse`) are exempt — see DEC-8.
- **ESLint `ccswitch/no-raw-palette` governs `web/src/ui/**` and `web/src/shell/**`:** no raw palette classes, no `dark:`, no hex in class strings, no z-index literals, no arbitrary px. Everything this plan creates under `web/src/shell/` is inside that scope.
- **Layer tokens:** `z-raised 1` · `z-sticky 20` · `z-nav 25` · `z-popover 30` · `z-overlay 40` · `z-drawer 50` · `z-dialog 60` · `z-palette 1000` · `z-toast 1100`. Use the utility (`z-nav`), never a literal.
- **Metrics:** `spacing-target 44px` (`min-h-target` / `min-w-target`); radii `sm 4` · `md 6` · `lg 10` · `pill 999`; type scale `2xs…2xl` multiplied by `--scale` (1 default, **1.25 kiosk**).
- **Naming:** `*Page` routed roots · `*Panel` pinned in-flow panels · `*Dialog`/`*Sheet` overlays · `*Card`/`*Row` widgets · `*Defs` registries. Folders: `web/src/ui/` primitives · `web/src/shell/` shell, regions, routing, viewport, kiosk, theme · `web/src/features/<feature>/` pages and widgets. (umbrella §4.5, D12)
- **Defer to Claude Code Desktop** where both products have an opinion. (umbrella §2.2, D7)
- **Commit trailer** on every commit: `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`
- **Implementers never push and never open PRs.** The controller does that.

### Gates (all must pass before any task reports DONE)

```
npm --prefix web test
npm --prefix web run typecheck
npm --prefix web run lint
npm --prefix web run build
```

This PR touches no Python, so the Python gates are not required per-task. The controller runs `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/` and `pytest server/tests/` once at final acceptance.

**Environment notes (this worktree):**
- Run web commands as `npm --prefix web <cmd>` from the worktree root. **Never `cd web/`** — a repo PreToolUse hook breaks on it.
- The shell sandbox rejects some compound commands (`&&`-chained git, some heredocs, `$(…)`, `env -u`). If a command is refused, split it into separate plain commands.
- `test_library_api.py::test_skill_crud_roundtrip` fails locally for unrelated environment reasons (the shared `.venv` imports `ccswitch.*` from the deploy worktree). Ignore it.

---

## Decisions

The code survey found ten questions the code cannot settle, plus three the spec states loosely. They are decided here so no implementer has to guess. **A decision recorded here overrides the spec's wording where they differ, and the reason is given.**

| # | Question | Decision | Why | Cost if wrong |
|---|---|---|---|---|
| **DEC-1** | `document.title`: `PageHeader` vs `useNeedsYouTitle` — both own it today (collision documented in `ui/PageHeader.tsx:37–46`) | **Neither. Create `web/src/shell/documentTitle.ts` as the single owner.** `PageHeader` calls `setBaseTitle(title)`; `useNeedsYouTitle` calls `setNeedsYouCount(n)`; the module composes `(3) Sessions — ccswitch` / `Sessions — ccswitch` / `ccswitch`. | Picking a winner leaves the loser silently clobbering. Two writers to one global is the parallel-implementation shape; one owner with two inputs is the fix. Task 2. | A title format nobody likes; one file to change. |
| **DEC-2** | Fleet-host picker is mobile-only (`Dashboard.tsx:434–439, 621–632, 773`); `DesktopShell.handleDispatch` has no `host` param | **Unify — desktop gains it.** One shell means one `DispatchBar`; the only alternatives are "desktop gains" or "mobile loses". | Mobile losing it is a regression, which is not acceptable. Additive, and it is where sub-spec B §6.3 lands anyway. | Desktop users see an Environment menu one PR earlier than planned. |
| **DEC-3** | Queued-filter predicate: `isPreSpawnStatus(s.status)` (mobile, `Dashboard.tsx:423`) vs `s.status === 'queued'` (desktop, `DesktopShell.tsx:146`) | **`isPreSpawnStatus` wins.** | It is the named abstraction and the broader set; the desktop literal is a narrower open-coded copy of the same idea — the exact divergence one shell removes. | Desktop's Queued section shows more rows than before (pre-spawn states it used to hide in the main list). |
| **DEC-4** | Sessions nav badge: `sessions.length` (mobile) vs `orderedSessions.length` (desktop) | **`sessions.length`.** | A section badge counts what the section contains, and `QueuedSection` / `PendingApprovalSection` are inside the Sessions section. Desktop's exclusion was an artifact of it not rendering `PendingApprovalSection` at all. | The badge reads higher than the main list's row count when sessions are queued. |
| **DEC-5** | `?dialog=new`: desktop has never had `NewSessionDialog` (it dispatches from the `DispatchBar`) | **`?dialog=new` opens the dialog in every mode.** | The route table is mode-independent by design (D6). A URL that works on a phone and does nothing on a desktop is precisely the F2.1 defect this PR closes. | Desktop gains a dialog path alongside the dispatch bar; both create sessions. |
| **DEC-6** | `ReviewView`: desktop renders it in-pane off `reviewId` state; the target table has `/archived/:id` | **Becomes the `/archived/:id` route in every mode.** Desktop's `reviewId` state is deleted. | umbrella §6 is explicit, and in-pane review is unreachable by URL. | Review opens as a routed page on desktop rather than in the main pane. |
| **DEC-7** | Two different `useDaemonHealth`s: `hooks/useDaemonHealth.ts` → `boolean` and `api/hooks.ts:90` → react-query result | **Both survive; rename the boolean one to `useDaemonReachable` (`hooks/useDaemonReachable.ts`).** Do **not** merge them. | They answer different questions. The name collision is a live grep hazard — ~13 test files mock the `api/hooks` one. Renaming the boolean one touches only `DaemonRestartBanner` and its own test. | A rename to revert; no behaviour change. |
| **DEC-8** | `session/brailleSpinner.ts:18` and `pipeline/motion.ts:6` read `window.matchMedia` from plain functions, so they cannot consume a React context | **Exempt, permanently.** They read `prefers-reduced-motion`, not layout. `BrowserTab.tsx:40` (`pointer: coarse`) and `shell/theme.tsx:55` (`prefers-color-scheme`) are likewise exempt. | The umbrella's constraint is "no other module reads `matchMedia` **for layout**". Motion, pointer and colour-scheme are not layout. | Someone later "fixes" a non-bug — prevented by encoding the allowlist in `useViewport.test.ts` (Task 3, Step 7). |
| **DEC-9** | `navDefs` needs an icon per section; **no tab has an icon today** | **Chosen and verified present in the installed `lucide-react`:** `sessions: MessagesSquare` · `pipeline: GitPullRequest` · `automations: Workflow` · `initiatives: Target` · `projects: Package` · `archived: Archive` · `insights: LineChart` · `settings: Settings`. | Someone had to pick; these are the conventional lucide choices for each concept. Verified by importing each name. | Swap an icon; one line each. |
| **DEC-10** | `?dialog=` currently **pushes** (`App.tsx:64–71` calls `setSearchParams` with no options) | **`{ replace: true }`,** per umbrella §6. `App.dialogs.test.tsx` is updated in the same task. | Pushing means Back closes the dialog *and* leaves a history entry per open — Back becomes unusable after a few dialogs. | Back no longer closes a dialog; it leaves the page. Called out in the PR body. |
| **DEC-11** | Spec: "`SidebarResizer` kept; width persisted at `ccswitch.listWidth`, migrating `ccswitch.sidebarWidth`" | **Migrate the storage key only.** `SIDEBAR_MIN/MAX/DEFAULT` keep their names *and values*; `lib/sidebarWidth.ts` keeps its path. Add a read-old-write-new migration inside `loadSidebarWidth()`. | `SessionCard.tsx:271–277` derives its container-query breakpoints from `SIDEBAR_MAX = 560`, and `SessionCard.layout.test.tsx:153` asserts no `@lg:`/`@xl:` classes exist. Changing the max silently breaks that. Renaming the constants is churn with no benefit in this PR. | The constant names say "sidebar" while the region is called "list". Cosmetic. |
| **DEC-12** | `components/__tests__/dedupeMobileDesktop.test.ts` is 7 **source-text greps** against the `Dashboard.tsx` / `DesktopShell.tsx` filenames — it fails on rename, not on behaviour | **Rewrite it against `AppShell.tsx` and the region files, preserving intent.** Do not delete it. | Its real guard — "no page re-inlines `AccountsManager` / `ArchivedManager` / the transcript reducer" — is still valuable and is exactly the kind of regression this PR could cause. | A guard that greps the wrong file; caught by Task 18's own review. |
| **DEC-13** | Spec §3 writes `kiosk.enabled`; the code returns `{ kiosk, cycleSeconds }` and parses only `kiosk`/`cycle` | **Rename the field to `enabled` on the move to `shell/kiosk.ts`, and add `pages: NavId[]` and `theme?: ThemeChoice`.** One consumer (`PipelineTab.tsx:48`) to update. | The spec's name is better (`config.kiosk.kiosk` reads badly) and `pages`/`theme` are required by §5. | A field rename; the compiler finds every site. |

| **DEC-14** | Umbrella D11 makes Settings sections path segments (`/settings/:section`), but `SettingsView.tsx:49` reads `?section=` and the full Settings migration is **PR 5** (sub-spec C) | **Add the `/settings/:section` route now and give `SettingsView` an optional `section` prop that takes precedence over the search param.** `?section=` keeps working; `/settings?section=x` redirects to `/settings/x`. No other change to `SettingsView`. | The route table is PR 3's deliverable and must be complete, but rewriting Settings here would pull PR 5's scope forward. A four-line adapter buys the route without the migration. | Settings reads its section from two places for one PR; PR 5 deletes the search-param path. |

**Two spec deviations worth stating plainly:**

1. **No mutable banner registry.** Sub-spec B §4.2 describes `registerBanner({ id, priority, when, render })`. This plan implements a single `useAppBanners()` hook instead (Task 6) that calls every banner hook in one place and returns a sorted `BannerStackItem[]`. It satisfies the same requirement — one declaration site, priority-sorted, kiosk-filterable — without a mutable module-level global whose contents depend on import order and which cannot be reset between tests. *Cost if wrong: a rename and a different call shape.*
2. **`DesktopShell.tsx` is at `web/src/components/DesktopShell.tsx`**, not `web/src/shell/` as sub-spec B §1 and §8 state. The spec's path is simply wrong. `web/src/shell/` currently contains only `theme.tsx` and `theme.test.tsx`.

---

## File Structure

**Created** — all under the `no-raw-palette` lint scope:

| File | Responsibility |
|---|---|
| `web/src/shell/navDefs.ts` | The one app map: `NavId`, `NavDef`, `NAV_DEFS`, `NAV_BY_ID`. Replaces `components/tabDefs.ts`. |
| `web/src/shell/documentTitle.ts` | Sole owner of `document.title`; composes base title + needs-you count. |
| `web/src/shell/useViewport.ts` | `LayoutMode`, `Viewport`, `ViewportProvider`, `useViewport()`. The only layout `matchMedia` reader. |
| `web/src/shell/kiosk.ts` | `KioskConfig`, `readKioskConfig`, `KioskProvider`, `useKiosk()`. Moved from `pipeline/kiosk.ts`. |
| `web/src/shell/InteractionContext.tsx` | `interactive: boolean` (false in kiosk); `useInteractive()`. |
| `web/src/shell/useAppBanners.ts` | Calls every banner hook once; returns sorted `BannerStackItem[]` with `tone`. |
| `web/src/shell/useNavBadges.ts` | The counts `buildTabs` used to compute, per `NavId`. |
| `web/src/shell/regions/BannerStackRegion.tsx` | `app.banners` — wraps `ui/BannerStack` with `useAppBanners()` + kiosk filter. |
| `web/src/shell/regions/NavRail.tsx` | `app.rail` (tablet/desktop). |
| `web/src/shell/regions/NavBar.tsx` | `app.rail` (phone, bottom). |
| `web/src/shell/regions/TopBar.tsx` | `app.rail` companion (phone, brand + global actions). |
| `web/src/shell/regions/ContextList.tsx` | `app.list` — hosts the section's list provider + `SidebarResizer`. |
| `web/src/shell/regions/PageHost.tsx` | `app.main` — `<Outlet/>` + scoped `SessionRuntimeProvider`. |
| `web/src/shell/regions/PromptDock.tsx` | `app.dock` — `DispatchBar` or `SessionComposer`. |
| `web/src/shell/regions/DrawerHost.tsx` | `app.drawer` — one `Sheet`, content by `?drawer=`. |
| `web/src/shell/regions/HelpSheet.tsx` | Help drawer content, derived from `navDefs` + `lib/shortcuts`. Replaces `components/HelpDrawer.tsx`. |
| `web/src/shell/regions/Overlays.tsx` | `app.overlays` — portal root + `Toaster` + `DeployOverlay` + `CommandResultOverlay`. |
| `web/src/shell/AppShell.tsx` | Layout per mode; `REGION_VISIBILITY`; the only place regions mount. |
| `web/src/shell/routes.tsx` | Route table + legacy redirects. |
| `web/src/features/sessions/SessionListColumn.tsx` | The Sessions list column, synthesized from the two shells' divergent markup. |
| `web/src/hooks/useDaemonReachable.ts` | Renamed from `hooks/useDaemonHealth.ts` (DEC-7). |

**Deleted** (all in this PR):

`web/src/components/Dashboard.tsx` (911 lines) · `web/src/components/DesktopShell.tsx` (678) · `web/src/components/tabDefs.ts` · `web/src/components/HelpDrawer.tsx` · `web/src/pipeline/kiosk.ts` · `web/src/hooks/useDaemonHealth.ts` · the `App.tsx` mode fork.

`NewSessionDialog` and `RenameDialog` are **moved** out of `Dashboard.tsx` (they are non-exported and exported respectively) into `web/src/features/sessions/`, not deleted.

**Modified:** `web/src/App.tsx` · `web/src/main.tsx` · `web/src/lib/sidebarWidth.ts` · `web/src/ui/PageHeader.tsx` · `web/src/hooks/useNeedsYouTitle.ts` · `web/src/pipeline/PipelineTab.tsx` · `web/src/pipeline/PipelineView.tsx` · `web/src/pipeline/StatusStrip.tsx` · `web/src/pipeline/PrRow.tsx` · `web/src/components/DaemonRestartBanner.tsx` · `web/src/components/session/SessionView.tsx` · `web/src/e2e/dashboard.spec.ts`.

---

## Task order and why

Tasks 1–6 build leaf modules with no dependency on the shell; each is independently testable and independently revertible. Tasks 7–14 build one region each, bottom-up, so every region has a passing test before `AppShell` composes them. Task 15 builds the route table. Task 16 composes the shell. Task 17 is the cutover — where the old shells die. Task 18 migrates the 103 orphaned tests. Task 19 adds kiosk. Task 20 verifies acceptance.

Tasks 17 and 18 are deliberately adjacent: 17 is the only task that ends with a red suite, and 18 is what makes it green again. **They are reviewed as one unit.**

### Plan fidelity — read this before you start

This plan is **not uniformly detailed, on purpose**, and you need to know which kind of task you have:

- **Tasks 1–6, 11, 15, 16, 19 give complete code.** Every value, name and signature in them was verified against the real source. Transcribe them; if you find yourself improving them, stop and report instead — a deviation there is more likely a misreading than an improvement.
- **Tasks 7–10, 12–14 give a complete *contract* — required behaviour, exact structural requirements, and the assertions your tests must make — but not full component source.** That is deliberate. Those components compose existing ones (`SessionListControls`, `QueuedSection`, `SessionList`, `AdoptPanel`, `DispatchBar`, `SidebarResizer`, …) whose real prop signatures are not reproduced here. **Read the actual component before you write the call site.** Prescribing JSX against unverified prop names would produce confidently wrong code, which is worse than none.

Wherever this plan says "copy from `<file>:<lines>` verbatim", do exactly that — do not retype from memory and do not tidy while copying. Wherever it says "confirm before writing", the confirmation is a required step, not a suggestion: several line numbers in this plan came from a survey taken at a specific commit and may have drifted by a line or two. Trust the symbol, verify the line.

If any instruction here contradicts the code you find, **the code wins and you report the contradiction.** Do not implement something you can see is wrong because the plan said so.

**The cutover (Task 17) is the risky one.** Everything before it is additive and the app keeps working; everything after it is cleanup. Tasks 1–16 must all be green before Task 17 starts.

---

### Task 1: `navDefs.ts` — the one app map

**Files:**
- Create: `web/src/shell/navDefs.ts`
- Test: `web/src/shell/navDefs.test.ts`
- Read for reference (do not modify yet): `web/src/components/tabDefs.ts`, `web/src/components/Dashboard.tsx:380`

**Interfaces:**
- Consumes: nothing.
- Produces: `type NavId`, `interface NavDef`, `const NAV_DEFS: readonly NavDef[]`, `const NAV_BY_ID: Record<NavId, NavDef>`. Tasks 7, 8, 13, 15, 16 and 18 all import from here.

**Context.** `web/src/components/tabDefs.ts` holds today's `TAB_DEFS` (id + label only) and `buildTabs(counts)`. `TabId` is declared in `Dashboard.tsx:380` and imported by `tabDefs.ts`, `DesktopShell.tsx:46` and `App.tsx:3`. `TabDef` has **no** `icon`, `path`, `showInKiosk` or `hasList` — those four fields are new and have no existing source. This task creates the replacement; `tabDefs.ts` is deleted in Task 17 once nothing imports it.

Keep the label and order of `TAB_DEFS` exactly (`Sessions, Pipeline, Automations, Initiatives, Products, Archived, Insights, Settings`) — note `projects` is labelled **"Products"**, which is deliberate and easy to "fix" by mistake.

- [ ] **Step 1: Write the failing test**

Create `web/src/shell/navDefs.test.ts`:

```ts
import { describe, it, expect } from 'vitest'
import { NAV_DEFS, NAV_BY_ID, type NavId } from './navDefs'

describe('navDefs', () => {
  it('preserves the legacy tab order and labels', () => {
    expect(NAV_DEFS.map((d) => [d.id, d.label])).toEqual([
      ['sessions', 'Sessions'],
      ['pipeline', 'Pipeline'],
      ['automations', 'Automations'],
      ['initiatives', 'Initiatives'],
      ['projects', 'Products'],
      ['archived', 'Archived'],
      ['insights', 'Insights'],
      ['settings', 'Settings'],
    ])
  })

  it('has unique ids and unique paths', () => {
    expect(new Set(NAV_DEFS.map((d) => d.id)).size).toBe(NAV_DEFS.length)
    expect(new Set(NAV_DEFS.map((d) => d.path)).size).toBe(NAV_DEFS.length)
  })

  it('gives every entry an absolute path and an icon', () => {
    for (const d of NAV_DEFS) {
      expect(d.path.startsWith('/'), `${d.id} path`).toBe(true)
      expect(typeof d.icon, `${d.id} icon`).toBe('object')
    }
  })

  it('only Sessions owns a list column', () => {
    expect(NAV_DEFS.filter((d) => d.hasList).map((d) => d.id)).toEqual(['sessions'])
  })

  it('excludes Settings from kiosk and includes every other section', () => {
    const notInKiosk = NAV_DEFS.filter((d) => !d.showInKiosk).map((d) => d.id)
    expect(notInKiosk).toEqual(['settings'])
  })

  it('NAV_BY_ID indexes every entry', () => {
    for (const d of NAV_DEFS) expect(NAV_BY_ID[d.id]).toBe(d)
    expect(Object.keys(NAV_BY_ID).length).toBe(NAV_DEFS.length)
  })

  // Guards the badge wiring in Task 6: every badge key a def declares must be a
  // key useNavBadges actually produces. Kept here (not in useNavBadges.test) so
  // the failure names the offending nav entry.
  it('declares only known badge keys', () => {
    const known = new Set(['sessions', 'pipeline', 'automations', 'archived', 'settings'])
    for (const d of NAV_DEFS) {
      if (d.badge !== undefined) expect(known.has(d.badge), `${d.id} badge=${d.badge}`).toBe(true)
    }
  })

  it('NavId is exactly the set of def ids', () => {
    // Compile-time: this fails typecheck if NavId and NAV_DEFS drift.
    const ids: NavId[] = NAV_DEFS.map((d) => d.id)
    expect(ids.length).toBe(8)
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `npm --prefix web exec -- vitest run --root web src/shell/navDefs.test.ts`
Expected: FAIL — `Failed to resolve import "./navDefs"`.

- [ ] **Step 3: Write the implementation**

Create `web/src/shell/navDefs.ts`:

```ts
import {
  MessagesSquare, GitPullRequest, Workflow, Target,
  Package, Archive, LineChart, Settings,
  type LucideIcon,
} from 'lucide-react'

/**
 * The one app map. `NavRail`, `NavBar`, the route table, the kiosk page list and
 * the help sheet all derive from this array — adding a section means adding a
 * row here and nothing else.
 *
 * Replaces `components/tabDefs.ts` (`TAB_DEFS` + `buildTabs`); `TabId` moved
 * here from `Dashboard.tsx` and is renamed `NavId`.
 */
export type NavId =
  | 'sessions' | 'pipeline' | 'automations' | 'initiatives'
  | 'projects' | 'archived' | 'insights' | 'settings'

/** Which count `useNavBadges()` supplies for this section, if any. */
export type NavBadgeKey = 'sessions' | 'pipeline' | 'automations' | 'archived' | 'settings'

export interface NavDef {
  id: NavId
  label: string
  icon: LucideIcon
  /** Absolute route path; must match the route table in `routes.tsx`. */
  path: string
  badge?: NavBadgeKey
  /** Reachable as a kiosk page. Settings is not — kiosk is read-only. */
  showInKiosk: boolean
  /** Renders a list column in `app.list`. Only Sessions does today. */
  hasList: boolean
}

/**
 * Order and labels are carried over verbatim from the deleted `TAB_DEFS`.
 * Note `projects` is labelled "Products" — that is intentional, not a typo.
 */
export const NAV_DEFS: readonly NavDef[] = [
  { id: 'sessions',    label: 'Sessions',    icon: MessagesSquare,  path: '/sessions',    badge: 'sessions',    showInKiosk: true,  hasList: true  },
  { id: 'pipeline',    label: 'Pipeline',    icon: GitPullRequest,  path: '/pipeline',    badge: 'pipeline',    showInKiosk: true,  hasList: false },
  { id: 'automations', label: 'Automations', icon: Workflow,        path: '/automations', badge: 'automations', showInKiosk: true,  hasList: false },
  { id: 'initiatives', label: 'Initiatives', icon: Target,          path: '/initiatives',                       showInKiosk: true,  hasList: false },
  { id: 'projects',    label: 'Products',    icon: Package,         path: '/projects',                          showInKiosk: true,  hasList: false },
  { id: 'archived',    label: 'Archived',    icon: Archive,         path: '/archived',    badge: 'archived',    showInKiosk: true,  hasList: false },
  { id: 'insights',    label: 'Insights',    icon: LineChart,       path: '/insights',                          showInKiosk: true,  hasList: false },
  { id: 'settings',    label: 'Settings',    icon: Settings,        path: '/settings',    badge: 'settings',    showInKiosk: false, hasList: false },
] as const

export const NAV_BY_ID: Record<NavId, NavDef> = Object.fromEntries(
  NAV_DEFS.map((d) => [d.id, d]),
) as Record<NavId, NavDef>

/** The nav entry whose path prefixes `pathname`, or null. Longest match wins. */
export function navDefForPath(pathname: string): NavDef | null {
  let best: NavDef | null = null
  for (const d of NAV_DEFS) {
    if (pathname === d.path || pathname.startsWith(d.path + '/')) {
      if (!best || d.path.length > best.path.length) best = d
    }
  }
  return best
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `npm --prefix web exec -- vitest run --root web src/shell/navDefs.test.ts`
Expected: PASS, 8 tests.

- [ ] **Step 5: Add the `navDefForPath` cases**

Append to `web/src/shell/navDefs.test.ts`:

```ts
describe('navDefForPath', () => {
  it('matches exact and nested paths', () => {
    expect(navDefForPath('/sessions')?.id).toBe('sessions')
    expect(navDefForPath('/sessions/abc123')?.id).toBe('sessions')
    expect(navDefForPath('/sessions/abc123/agents/x')?.id).toBe('sessions')
    expect(navDefForPath('/settings/general')?.id).toBe('settings')
  })

  it('does not match a path that merely shares a prefix string', () => {
    expect(navDefForPath('/sessionsfoo')).toBeNull()
  })

  it('returns null for an unknown path', () => {
    expect(navDefForPath('/nope')).toBeNull()
  })
})
```

Add `navDefForPath` to the import at the top of the test file. Run the file again: PASS, 11 tests.

- [ ] **Step 6: Run the full gates**

```
npm --prefix web test
npm --prefix web run typecheck
npm --prefix web run lint
```

- [ ] **Step 7: Commit**

```
git add web/src/shell/navDefs.ts web/src/shell/navDefs.test.ts
git commit -m "feat(shell): navDefs — the one app map (id, label, icon, path, badge, kiosk, list)"
```

---

### Task 2: `documentTitle.ts` — one owner for `document.title`

**Files:**
- Create: `web/src/shell/documentTitle.ts`, `web/src/shell/documentTitle.test.ts`
- Modify: `web/src/ui/PageHeader.tsx` (lines 28–52 — the `pageHeaderSeq` block), `web/src/hooks/useNeedsYouTitle.ts` (whole file)
- Test: `web/src/shell/documentTitle.test.ts`, and update `web/src/ui/Page.test.tsx`

**Interfaces:**
- Consumes: nothing.
- Produces: `setBaseTitle(title: string | null): void`, `setNeedsYouCount(n: number): void`, `resetDocumentTitle(): void` (test-only helper), `APP_NAME`.

**Context — the collision this closes.** Two modules write `document.title` today with no coordination:

- `web/src/ui/PageHeader.tsx:36–52` sets `` `${title} — ccswitch` `` and guards ownership with a module-level `pageHeaderSeq` counter. Its comment at L37–46 explicitly says whoever first renders `PageHeader` on a page that also uses `useNeedsYouTitle` must reconcile ownership.
- `web/src/hooks/useNeedsYouTitle.ts` sets `` `(${n}) ccswitch — needs you` `` or `'ccswitch'`, mounted only at `Dashboard.tsx:428`.

This PR mounts `useNeedsYouTitle` in `PageHost` (Task 11) — the same page where `PageHeader` lives — so the collision goes live. Per DEC-1, neither wins: both become **inputs** to one owner.

Composition rule:

| base | needsYou | `document.title` |
|---|---|---|
| `'Sessions'` | 0 | `Sessions — ccswitch` |
| `'Sessions'` | 3 | `(3) Sessions — ccswitch` |
| `null` | 0 | `ccswitch` |
| `null` | 3 | `(3) ccswitch — needs you` |

The last row preserves today's exact mobile string for the case where no `PageHeader` has claimed a base title, so nothing regresses before pages migrate in sub-spec C.

- [ ] **Step 1: Write the failing test**

Create `web/src/shell/documentTitle.test.ts`:

```ts
import { describe, it, expect, beforeEach } from 'vitest'
import { setBaseTitle, setNeedsYouCount, resetDocumentTitle, APP_NAME } from './documentTitle'

describe('documentTitle', () => {
  beforeEach(() => { resetDocumentTitle() })

  it('is just the app name with no inputs', () => {
    expect(document.title).toBe(APP_NAME)
  })

  it('appends the app name to a base title', () => {
    setBaseTitle('Sessions')
    expect(document.title).toBe('Sessions — ccswitch')
  })

  it('prefixes the needs-you count onto a base title', () => {
    setBaseTitle('Sessions')
    setNeedsYouCount(3)
    expect(document.title).toBe('(3) Sessions — ccswitch')
  })

  it('uses the legacy needs-you string when no base title is set', () => {
    setNeedsYouCount(2)
    expect(document.title).toBe('(2) ccswitch — needs you')
  })

  it('order of the two inputs does not matter', () => {
    setNeedsYouCount(3)
    setBaseTitle('Pipeline')
    expect(document.title).toBe('(3) Pipeline — ccswitch')
  })

  it('clearing the count restores the plain base title', () => {
    setBaseTitle('Sessions')
    setNeedsYouCount(3)
    setNeedsYouCount(0)
    expect(document.title).toBe('Sessions — ccswitch')
  })

  it('clearing the base title falls back to the app name', () => {
    setBaseTitle('Sessions')
    setBaseTitle(null)
    expect(document.title).toBe(APP_NAME)
  })

  it('a later base title replaces an earlier one', () => {
    setBaseTitle('Sessions')
    setBaseTitle('Settings')
    expect(document.title).toBe('Settings — ccswitch')
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `npm --prefix web exec -- vitest run --root web src/shell/documentTitle.test.ts`
Expected: FAIL — `Failed to resolve import "./documentTitle"`.

- [ ] **Step 3: Write the implementation**

Create `web/src/shell/documentTitle.ts`:

```ts
/**
 * The single owner of `document.title`.
 *
 * Two things want to write the title and neither may write it directly:
 *   - `ui/PageHeader` supplies the BASE title (the page's name).
 *   - `hooks/useNeedsYouTitle` supplies the needs-you COUNT.
 *
 * Before this module they both assigned `document.title` and clobbered each
 * other depending on effect order — the collision documented in PageHeader's
 * own source. Here they are two inputs to one composer, so the outcome does not
 * depend on which effect happens to run last.
 */
export const APP_NAME = 'ccswitch'

let base: string | null = null
let needsYou = 0

function compose(): string {
  if (base === null) {
    // No page has claimed a title. Preserve the exact legacy string so nothing
    // regresses before pages adopt PageHeader in sub-spec C.
    return needsYou > 0 ? `(${needsYou}) ${APP_NAME} — needs you` : APP_NAME
  }
  const withApp = `${base} — ${APP_NAME}`
  return needsYou > 0 ? `(${needsYou}) ${withApp}` : withApp
}

function apply(): void {
  if (typeof document !== 'undefined') document.title = compose()
}

/** Called by `PageHeader`. Pass `null` on unmount to release the base title. */
export function setBaseTitle(title: string | null): void {
  base = title
  apply()
}

/** Called by `useNeedsYouTitle`. */
export function setNeedsYouCount(n: number): void {
  needsYou = Number.isFinite(n) && n > 0 ? Math.floor(n) : 0
  apply()
}

/** Test-only: clear both inputs and reset the title. */
export function resetDocumentTitle(): void {
  base = null
  needsYou = 0
  apply()
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `npm --prefix web exec -- vitest run --root web src/shell/documentTitle.test.ts`
Expected: PASS, 8 tests.

- [ ] **Step 5: Route `PageHeader` through it**

In `web/src/ui/PageHeader.tsx`, delete the `pageHeaderSeq` counter and the whole `document.title` effect block (the comment at L28–52 explaining the unresolved collision goes with it — it is now resolved). Replace with:

```tsx
import { setBaseTitle } from '@/shell/documentTitle'

// …inside PageHeader, replacing the old title effect:
  React.useEffect(() => {
    setBaseTitle(title)
    return () => setBaseTitle(null)
  }, [title])
```

Keep everything else in `PageHeader` unchanged (the `<h1>`, the header element, `meta`, `actions`, `className`).

Then update `web/src/ui/Page.test.tsx`: any assertion expecting `document.title` to be exactly `` `${title} — ccswitch` `` still holds. Add `import { resetDocumentTitle } from '@/shell/documentTitle'` and a `beforeEach(resetDocumentTitle)` so the module-level state cannot leak between tests in that file.

- [ ] **Step 6: Route `useNeedsYouTitle` through it**

Rewrite `web/src/hooks/useNeedsYouTitle.ts` in full:

```ts
import { useEffect } from 'react'
import { setNeedsYouCount } from '@/shell/documentTitle'
import { triageTier } from '@/session/triageTier'
import type { Session } from '@/api/types'

/**
 * Reports how many sessions need a human into the shared title composer.
 * It does NOT write `document.title` itself — see `shell/documentTitle.ts`.
 */
export function useNeedsYouTitle(sessions: Session[]): void {
  const needsYou = sessions.filter((s) => {
    const tier = triageTier(s)
    return tier === 'needs-you' || tier === 'error'
  }).length

  useEffect(() => {
    setNeedsYouCount(needsYou)
    return () => setNeedsYouCount(0)
  }, [needsYou])
}
```

Check the real import paths for `triageTier` and `Session` against the existing file before writing — copy them from the version you are replacing rather than trusting the paths above.

- [ ] **Step 7: Add the integration test**

Append to `web/src/shell/documentTitle.test.ts` a test that mounts `PageHeader` and `useNeedsYouTitle` together and asserts the composed title, proving the collision is actually gone:

```tsx
// Requires: import { render } from '@testing-library/react'
//           import { renderHook } from '@testing-library/react'
//           import { PageHeader } from '@/ui/PageHeader'
//           import { useNeedsYouTitle } from '@/hooks/useNeedsYouTitle'
it('PageHeader and useNeedsYouTitle compose instead of clobbering', () => {
  render(<PageHeader title="Sessions" />)
  expect(document.title).toBe('Sessions — ccswitch')
  renderHook(() => useNeedsYouTitle([
    { id: 'a', status: 'needs_input' } as never,
  ]))
  expect(document.title).toBe('(1) Sessions — ccswitch')
})
```

Rename the file to `documentTitle.test.tsx` since it now contains JSX. Build the session fixture so that `triageTier` actually returns `'needs-you'` for it — read `web/src/session/triageTier.ts` and construct a real minimal session rather than casting, if a cheap one exists. **If you must cast, say so in your report** — an unsafe cast in a test is a known debt in this repo (issue #952).

- [ ] **Step 8: Run the gates**

```
npm --prefix web test
npm --prefix web run typecheck
npm --prefix web run lint
```

Expect `Page.test.tsx` and any test asserting the old needs-you string to need updating. `Dashboard.tsx:428` still calls `useNeedsYouTitle` and still works — the signature is unchanged.

- [ ] **Step 9: Commit**

```
git add web/src/shell/documentTitle.ts web/src/shell/documentTitle.test.tsx web/src/ui/PageHeader.tsx web/src/ui/Page.test.tsx web/src/hooks/useNeedsYouTitle.ts
git commit -m "fix(shell): one owner for document.title; PageHeader and needs-you compose"
```

---

### Task 3: `useViewport()` — the only layout media-query reader

**Files:**
- Create: `web/src/shell/useViewport.ts`, `web/src/shell/useViewport.test.tsx`
- Read for reference: `web/src/lib/useMediaQuery.ts` (whole file, 25 lines)

**Interfaces:**
- Consumes: `readKioskConfig` from Task 4 — **but Task 4 runs after this one**, so this task takes the kiosk config as a **prop** on the provider (`ViewportProvider({ kiosk, children })`) and does not import `shell/kiosk.ts`. Task 16 supplies it.
- Produces: `type LayoutMode`, `type WidthTier`, `interface Viewport`, `ViewportProvider`, `useViewport(): Viewport`.

**Context.** Today exactly **one** layout breakpoint exists: `(min-width: 1024px)`, read at `App.tsx:187` (the shell fork) and `SessionCard.tsx:190` (swipe-overflow clipping). `768px` and `1440px` are new. `web/src/lib/useMediaQuery.ts` returns `false` when `matchMedia` is unavailable, and its effect early-returns, so jsdom defaults to the narrow branch — preserve that behaviour exactly.

Per DEC-8, the non-layout `matchMedia` readers stay where they are: `session/brailleSpinner.ts:18`, `pipeline/motion.ts:6`, `BrowserTab.tsx:40`, `shell/theme.tsx:55`.

- [ ] **Step 1: Write the failing test**

Create `web/src/shell/useViewport.test.tsx`:

```tsx
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ViewportProvider, useViewport } from './useViewport'

/** Stub matchMedia so that every query in `matching` reports true. */
function stubMatchMedia(matching: string[]) {
  vi.stubGlobal('matchMedia', (q: string) => ({
    matches: matching.includes(q),
    media: q,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }))
}

function Probe() {
  const v = useViewport()
  return <span data-testid="v">{`${v.mode}/${v.width}/${v.coarsePointer}`}</span>
}

function renderAt(matching: string[], kiosk: Parameters<typeof ViewportProvider>[0]['kiosk'] = null) {
  stubMatchMedia(matching)
  render(<ViewportProvider kiosk={kiosk}><Probe /></ViewportProvider>)
  return screen.getByTestId('v').textContent
}

const W768 = '(min-width: 768px)'
const W1024 = '(min-width: 1024px)'
const W1440 = '(min-width: 1440px)'
const COARSE = '(pointer: coarse)'

describe('useViewport', () => {
  beforeEach(() => { vi.unstubAllGlobals() })

  it('defaults to phone with no matchMedia (jsdom)', () => {
    vi.stubGlobal('matchMedia', undefined)
    render(<ViewportProvider kiosk={null}><Probe /></ViewportProvider>)
    expect(screen.getByTestId('v').textContent).toBe('phone/phone/false')
  })

  it('below 768 is phone', () => {
    expect(renderAt([])).toBe('phone/phone/false')
  })

  it('768–1023 is tablet', () => {
    expect(renderAt([W768])).toBe('tablet/tablet/false')
  })

  it('1024–1439 is desktop', () => {
    expect(renderAt([W768, W1024])).toBe('desktop/desktop/false')
  })

  it('1440+ is desktop at the wide tier', () => {
    expect(renderAt([W768, W1024, W1440])).toBe('desktop/wide/false')
  })

  it('reports a coarse pointer', () => {
    expect(renderAt([COARSE])).toBe('phone/phone/true')
  })

  it('kiosk overrides the mode at any width but keeps the width tier', () => {
    expect(renderAt([W768, W1024, W1440], { enabled: true, cycleSeconds: 30, pages: ['pipeline'] }))
      .toBe('kiosk/wide/false')
  })

  it('a disabled kiosk config does not override the mode', () => {
    expect(renderAt([W768, W1024], { enabled: false, cycleSeconds: 30, pages: ['pipeline'] }))
      .toBe('desktop/desktop/false')
  })

  it('throws when used outside the provider', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    expect(() => render(<Probe />)).toThrow(/useViewport must be used within/)
    spy.mockRestore()
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `npm --prefix web exec -- vitest run --root web src/shell/useViewport.test.tsx`
Expected: FAIL — `Failed to resolve import "./useViewport"`.

- [ ] **Step 3: Write the implementation**

Create `web/src/shell/useViewport.ts`:

```ts
import * as React from 'react'
import { useMediaQuery } from '@/lib/useMediaQuery'

export type LayoutMode = 'phone' | 'tablet' | 'desktop' | 'kiosk'
export type WidthTier = 'phone' | 'tablet' | 'desktop' | 'wide'

/** The kiosk half of the viewport, supplied by AppShell (see shell/kiosk.ts). */
export interface ViewportKiosk {
  enabled: boolean
  cycleSeconds: number
  pages: readonly string[]
}

export interface Viewport {
  mode: LayoutMode
  /** Always the real width tier, even in kiosk — kiosk layouts use it for column counts. */
  width: WidthTier
  coarsePointer: boolean
  kiosk: ViewportKiosk | null
}

const ViewportContext = React.createContext<Viewport | null>(null)

/**
 * The ONLY module that reads matchMedia for layout (umbrella §4.1).
 *
 * Deliberately exempt, because they are not layout queries and two of them are
 * plain functions that cannot consume a React context:
 *   - session/brailleSpinner.ts, pipeline/motion.ts  (prefers-reduced-motion)
 *   - components/BrowserTab.tsx                      (pointer: coarse, for touch UI)
 *   - shell/theme.tsx                                (prefers-color-scheme)
 */
export function ViewportProvider({
  kiosk,
  children,
}: {
  kiosk: ViewportKiosk | null
  children: React.ReactNode
}) {
  const atTablet = useMediaQuery('(min-width: 768px)')
  const atDesktop = useMediaQuery('(min-width: 1024px)')
  const atWide = useMediaQuery('(min-width: 1440px)')
  const coarsePointer = useMediaQuery('(pointer: coarse)')

  const value = React.useMemo<Viewport>(() => {
    const width: WidthTier = atWide ? 'wide' : atDesktop ? 'desktop' : atTablet ? 'tablet' : 'phone'
    const natural: LayoutMode = atDesktop ? 'desktop' : atTablet ? 'tablet' : 'phone'
    return {
      mode: kiosk?.enabled ? 'kiosk' : natural,
      width,
      coarsePointer,
      kiosk: kiosk ?? null,
    }
  }, [atTablet, atDesktop, atWide, coarsePointer, kiosk])

  return <ViewportContext.Provider value={value}>{children}</ViewportContext.Provider>
}

export function useViewport(): Viewport {
  const v = React.useContext(ViewportContext)
  if (!v) throw new Error('useViewport must be used within a ViewportProvider')
  return v
}
```

Rename the file to `useViewport.tsx` — it contains JSX.

- [ ] **Step 4: Run the test to verify it passes**

Run: `npm --prefix web exec -- vitest run --root web src/shell/useViewport.test.tsx`
Expected: PASS, 9 tests.

- [ ] **Step 5: Pin the DEC-8 exemption so nobody "fixes" it**

Append to `web/src/shell/useViewport.test.tsx`:

```ts
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'

/**
 * useViewport is the only LAYOUT matchMedia reader. This walks the source and
 * fails if a new min-width query appears outside it. Non-layout queries
 * (motion, pointer, colour-scheme) are listed as deliberate exemptions — see
 * DEC-8 in the plan. Adding a file here must be a decision, not a reflex.
 */
const LAYOUT_QUERY = /matchMedia\(\s*['"`]\((?:min|max)-width/
const EXEMPT = new Set<string>([
  'src/shell/useViewport.tsx',
  'src/lib/useMediaQuery.ts',      // the generic subscription primitive
])

function walk(dir: string, out: string[] = []): string[] {
  for (const e of readdirSync(dir)) {
    const p = join(dir, e)
    if (statSync(p).isDirectory()) { walk(p, out); continue }
    if (/\.(ts|tsx)$/.test(e) && !/\.test\.tsx?$/.test(e)) out.push(p)
  }
  return out
}

it('no module outside useViewport reads a layout media query', () => {
  const root = join(process.cwd(), 'src')
  const offenders = walk(root)
    .filter((p) => LAYOUT_QUERY.test(readFileSync(p, 'utf8')))
    .map((p) => p.slice(process.cwd().length + 1))
    .filter((rel) => !EXEMPT.has(rel))
  expect(offenders, offenders.join('\n')).toEqual([])
})
```

**Verify this guard can fail:** temporarily add `matchMedia('(min-width: 900px)')` to any source file, confirm the test goes red and names that file, then remove it. Report what the failure message said. A guard you have not seen fail is not a guard.

Note this will initially fail because `App.tsx:187` and `SessionCard.tsx:190` both call `useMediaQuery('(min-width: 1024px)')` — the regex above targets raw `matchMedia(`, not `useMediaQuery(`, so they will not match. **Confirm that is true by running it.** If you decide the guard should also catch `useMediaQuery('(min-width:…')`, then `App.tsx` and `SessionCard.tsx` must be added to `EXEMPT` with a comment saying they are removed in Tasks 17 and (for SessionCard) a later PR — and the exemption for `App.tsx` must be deleted in Task 17. State which form you chose in your report.

- [ ] **Step 6: Run the gates**

```
npm --prefix web test
npm --prefix web run typecheck
npm --prefix web run lint
```

- [ ] **Step 7: Commit**

```
git add web/src/shell/useViewport.tsx web/src/shell/useViewport.test.tsx
git commit -m "feat(shell): useViewport — one layout mode source, with an exemption guard"
```

---

### Task 4: move and extend `kiosk.ts`

**Files:**
- Create: `web/src/shell/kiosk.ts`, `web/src/shell/kiosk.test.ts`
- Delete: `web/src/pipeline/kiosk.ts`
- Modify: `web/src/pipeline/PipelineTab.tsx:15,48,50,51`

**Interfaces:**
- Consumes: `NavId`, `NAV_DEFS` (Task 1); `ThemeChoice` from `@/shell/theme`.
- Produces: `interface KioskConfig { enabled; cycleSeconds; pages; theme }`, `readKioskConfig(search?): KioskConfig`, `DEFAULT_CYCLE_SECONDS`, `MIN_CYCLE_SECONDS`.

**Context.** `web/src/pipeline/kiosk.ts` (25 lines, ported from plotroom) returns `{ kiosk, cycleSeconds }` and parses only `?kiosk=1` and `?cycle=N`. Its single consumer is `PipelineTab.tsx:48` (`const [{ kiosk, cycleSeconds }] = useState(readKioskConfig)`) — despite the file's own comment claiming it is read at App mount. There is **no test file for it today**.

Per DEC-13: rename `kiosk` → `enabled`, add `pages: NavId[]` and `theme?: ThemeChoice`, move to `shell/`.

Validation rules from sub-spec B §5: `pages` is validated against `showInKiosk` and defaults to `['pipeline']`; `cycle` has a minimum of 10 s and a default of 30.

- [ ] **Step 1: Write the failing test**

Create `web/src/shell/kiosk.test.ts`:

```ts
import { describe, it, expect } from 'vitest'
import { readKioskConfig, DEFAULT_CYCLE_SECONDS, MIN_CYCLE_SECONDS } from './kiosk'

describe('readKioskConfig', () => {
  it('is disabled with no params', () => {
    const c = readKioskConfig('')
    expect(c.enabled).toBe(false)
    expect(c.cycleSeconds).toBe(DEFAULT_CYCLE_SECONDS)
    expect(c.pages).toEqual(['pipeline'])
    expect(c.theme).toBeUndefined()
  })

  it('enables only on kiosk=1', () => {
    expect(readKioskConfig('?kiosk=1').enabled).toBe(true)
    expect(readKioskConfig('?kiosk=0').enabled).toBe(false)
    expect(readKioskConfig('?kiosk=true').enabled).toBe(false)
  })

  it('clamps cycle to the minimum and rounds', () => {
    expect(readKioskConfig('?cycle=60').cycleSeconds).toBe(60)
    expect(readKioskConfig('?cycle=3').cycleSeconds).toBe(MIN_CYCLE_SECONDS)
    expect(readKioskConfig('?cycle=45.6').cycleSeconds).toBe(46)
  })

  it('ignores a non-numeric or empty cycle', () => {
    expect(readKioskConfig('?cycle=abc').cycleSeconds).toBe(DEFAULT_CYCLE_SECONDS)
    expect(readKioskConfig('?cycle=').cycleSeconds).toBe(DEFAULT_CYCLE_SECONDS)
  })

  it('parses a comma-separated page list', () => {
    expect(readKioskConfig('?pages=pipeline,insights').pages).toEqual(['pipeline', 'insights'])
  })

  it('drops unknown pages and pages that are not kiosk-safe', () => {
    // 'settings' has showInKiosk:false; 'nope' is not a NavId at all.
    expect(readKioskConfig('?pages=pipeline,settings,nope').pages).toEqual(['pipeline'])
  })

  it('falls back to the default when every requested page is rejected', () => {
    expect(readKioskConfig('?pages=settings').pages).toEqual(['pipeline'])
    expect(readKioskConfig('?pages=').pages).toEqual(['pipeline'])
  })

  it('de-duplicates the page list while preserving order', () => {
    expect(readKioskConfig('?pages=insights,pipeline,insights').pages).toEqual(['insights', 'pipeline'])
  })

  it('accepts only light or dark for theme', () => {
    expect(readKioskConfig('?theme=light').theme).toBe('light')
    expect(readKioskConfig('?theme=dark').theme).toBe('dark')
    expect(readKioskConfig('?theme=system').theme).toBeUndefined()
    expect(readKioskConfig('?theme=purple').theme).toBeUndefined()
  })

  it('parses a full kiosk URL', () => {
    const c = readKioskConfig('?kiosk=1&pages=pipeline,insights&cycle=15&theme=dark')
    expect(c).toEqual({ enabled: true, cycleSeconds: 15, pages: ['pipeline', 'insights'], theme: 'dark' })
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `npm --prefix web exec -- vitest run --root web src/shell/kiosk.test.ts`
Expected: FAIL — cannot resolve `./kiosk`.

- [ ] **Step 3: Write the implementation**

Create `web/src/shell/kiosk.ts`:

```ts
// Originally ported from plotroom@5166b52 frontend/src/kiosk.ts; moved here from
// web/src/pipeline/kiosk.ts and extended with `pages` and `theme` (sub-spec B §5).
import { NAV_DEFS, type NavId } from './navDefs'
import type { ThemeChoice } from './theme'

export const DEFAULT_CYCLE_SECONDS = 30
export const MIN_CYCLE_SECONDS = 10
const DEFAULT_PAGES: readonly NavId[] = ['pipeline']

export interface KioskConfig {
  enabled: boolean
  /** Seconds each page stays on screen before the attract cycle advances. */
  cycleSeconds: number
  /** Pages the attract cycle rotates through; always kiosk-safe and non-empty. */
  pages: readonly NavId[]
  /** Pins the theme for a wall display. `system` is not accepted — a wall
   *  display has no user to follow. */
  theme?: ThemeChoice
}

const KIOSK_SAFE = new Set<string>(NAV_DEFS.filter((d) => d.showInKiosk).map((d) => d.id))

function parsePages(raw: string | null): readonly NavId[] {
  if (raw === null) return DEFAULT_PAGES
  const seen = new Set<string>()
  const out: NavId[] = []
  for (const part of raw.split(',')) {
    const id = part.trim()
    if (!KIOSK_SAFE.has(id) || seen.has(id)) continue
    seen.add(id)
    out.push(id as NavId)
  }
  return out.length > 0 ? out : DEFAULT_PAGES
}

function parseCycle(raw: string | null): number {
  if (raw === null || raw.trim() === '') return DEFAULT_CYCLE_SECONDS
  const n = Number(raw)
  if (!Number.isFinite(n)) return DEFAULT_CYCLE_SECONDS
  return Math.max(MIN_CYCLE_SECONDS, Math.round(n))
}

function parseTheme(raw: string | null): ThemeChoice | undefined {
  return raw === 'light' || raw === 'dark' ? raw : undefined
}

/**
 * Parse `?kiosk=1&pages=a,b&cycle=N&theme=light|dark`.
 *
 * Read once at AppShell mount — changing the URL requires a reload, which is
 * exactly what a wall display does anyway.
 */
export function readKioskConfig(search: string = window.location.search): KioskConfig {
  const p = new URLSearchParams(search)
  return {
    enabled: p.get('kiosk') === '1',
    cycleSeconds: parseCycle(p.get('cycle')),
    pages: parsePages(p.get('pages')),
    theme: parseTheme(p.get('theme')),
  }
}
```

Check `web/src/shell/theme.tsx` for the actual exported name of the theme union before writing the import — the survey reports `ThemeChoice`, but confirm it and confirm it is exported as a type.

- [ ] **Step 4: Run the test to verify it passes**

Run: `npm --prefix web exec -- vitest run --root web src/shell/kiosk.test.ts`
Expected: PASS, 10 tests.

- [ ] **Step 5: Update the one consumer and delete the old module**

In `web/src/pipeline/PipelineTab.tsx`:
- change the import at L15 from `./kiosk` to `@/shell/kiosk`;
- change L48 from `const [{ kiosk, cycleSeconds }] = useState(readKioskConfig)` to `const [{ enabled: kiosk, cycleSeconds }] = useState(readKioskConfig)` — keeping the local name `kiosk` means L50, L51, L62 and L97 need no edit.

Then `git rm web/src/pipeline/kiosk.ts`.

- [ ] **Step 6: Verify nothing else imported it**

```
grep -rn "pipeline/kiosk" web/src
grep -rn "readKioskConfig" web/src
```

Expected: the first prints nothing; the second prints only `shell/kiosk.ts`, `shell/kiosk.test.ts` and `pipeline/PipelineTab.tsx`.

- [ ] **Step 7: Run the gates and commit**

```
npm --prefix web test
npm --prefix web run typecheck
npm --prefix web run lint
git add -A web/src/shell/kiosk.ts web/src/shell/kiosk.test.ts web/src/pipeline/PipelineTab.tsx web/src/pipeline/kiosk.ts
git commit -m "refactor(shell): move kiosk config to shell/, add pages and theme params"
```

---

### Task 5: `InteractionContext` — kiosk read-only, expressed once

**Files:**
- Create: `web/src/shell/InteractionContext.tsx`, `web/src/shell/InteractionContext.test.tsx`
- Modify: `web/src/pipeline/PipelineView.tsx:58,67,95,101`, `web/src/pipeline/StatusStrip.tsx:78`, `web/src/pipeline/PrRow.tsx:103-108`, `web/src/pipeline/PipelineTab.tsx:97`

**Interfaces:**
- Consumes: nothing.
- Produces: `InteractionProvider({ interactive, children })`, `useInteractive(): boolean`.

**Context.** Today "read-only" is threaded as props: `PipelineTab` passes `kiosk` to `PipelineView` (L97), which passes `interactive={!kiosk}` to `StatusStrip` (L101) and `expandable={!kiosk}` to `PrRow` (L95). Sub-spec B §5 makes these read a context instead, so any component anywhere can be read-only without a prop chain.

`useInteractive()` must default to **`true`** outside a provider, not throw — unlike `useViewport`. A component rendered in a test with no shell must behave normally; a missing provider meaning "inert" would silently break every existing test.

- [ ] **Step 1: Write the failing test**

Create `web/src/shell/InteractionContext.test.tsx`:

```tsx
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { InteractionProvider, useInteractive } from './InteractionContext'

function Probe() {
  return <span data-testid="i">{String(useInteractive())}</span>
}

describe('InteractionContext', () => {
  it('defaults to interactive outside a provider', () => {
    render(<Probe />)
    expect(screen.getByTestId('i').textContent).toBe('true')
  })

  it('reports false inside a non-interactive provider', () => {
    render(<InteractionProvider interactive={false}><Probe /></InteractionProvider>)
    expect(screen.getByTestId('i').textContent).toBe('false')
  })

  it('reports true inside an interactive provider', () => {
    render(<InteractionProvider interactive={true}><Probe /></InteractionProvider>)
    expect(screen.getByTestId('i').textContent).toBe('true')
  })

  it('the innermost provider wins', () => {
    render(
      <InteractionProvider interactive={false}>
        <InteractionProvider interactive={true}><Probe /></InteractionProvider>
      </InteractionProvider>,
    )
    expect(screen.getByTestId('i').textContent).toBe('true')
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `npm --prefix web exec -- vitest run --root web src/shell/InteractionContext.test.tsx`
Expected: FAIL — cannot resolve `./InteractionContext`.

- [ ] **Step 3: Write the implementation**

Create `web/src/shell/InteractionContext.tsx`:

```tsx
import * as React from 'react'

/**
 * Whether the UI accepts input. False in kiosk, where the app is a wall display
 * with no user in front of it.
 *
 * Defaults to TRUE outside a provider on purpose: a component rendered without
 * the shell (every unit test) must behave normally. A default of `false` would
 * silently make the whole suite read-only.
 */
const InteractionContext = React.createContext<boolean>(true)

export function InteractionProvider({
  interactive,
  children,
}: {
  interactive: boolean
  children: React.ReactNode
}) {
  return <InteractionContext.Provider value={interactive}>{children}</InteractionContext.Provider>
}

export function useInteractive(): boolean {
  return React.useContext(InteractionContext)
}
```

- [ ] **Step 4: Run to verify it passes** — PASS, 4 tests.

- [ ] **Step 5: Rewire `StatusStrip` and `PrRow` to the context**

In `web/src/pipeline/StatusStrip.tsx`: delete the `interactive?: boolean` prop from `StatusStripProps` and its default in the destructure; instead `const interactive = useInteractive()` inside the component. Keep the doc comment's meaning, updated to say where the value now comes from.

In `web/src/pipeline/PrRow.tsx`: same treatment for `expandable?: boolean` → `const expandable = useInteractive()`.

In `web/src/pipeline/PipelineView.tsx`: delete the `kiosk = false` prop from the signature (L58/L67) and the `interactive={!kiosk}` / `expandable={!kiosk}` pass-throughs (L95, L101).

In `web/src/pipeline/PipelineTab.tsx`: delete `kiosk={kiosk}` from the `<PipelineView …/>` at L97, and wrap the returned tree in `<InteractionProvider interactive={!kiosk}>`. **Keep** the `{!kiosk && (…chrome bar…)}` conditional at L62–93 and the `useKioskCycle` / `useFocusedPipeline` calls as they are — those are page-level concerns, not primitive interactivity.

- [ ] **Step 6: Fix the tests that passed those props**

```
grep -rn "interactive=\|expandable=\|kiosk=" web/src/pipeline web/src/components/__tests__ --include=*.tsx --include=*.ts
```

Every test that rendered `<StatusStrip interactive={false}/>` or `<PrRow expandable={false}/>` must now wrap in `<InteractionProvider interactive={false}>`. Update each; do not delete assertions.

- [ ] **Step 7: Run the gates and commit**

```
npm --prefix web test
npm --prefix web run typecheck
npm --prefix web run lint
git add web/src/shell/InteractionContext.tsx web/src/shell/InteractionContext.test.tsx web/src/pipeline
git commit -m "refactor(pipeline): read-only comes from InteractionContext, not a kiosk prop chain"
```

---

### Task 6: `useAppBanners` and `useNavBadges` — one declaration site each

**Files:**
- Create: `web/src/shell/useAppBanners.ts`, `web/src/shell/useAppBanners.test.tsx`, `web/src/shell/useNavBadges.ts`, `web/src/shell/useNavBadges.test.tsx`
- Modify: `web/src/ui/BannerStack.tsx` (add `tone` to `BannerStackItem`)
- Rename: `web/src/hooks/useDaemonHealth.ts` → `web/src/hooks/useDaemonReachable.ts` (DEC-7), updating `web/src/components/DaemonRestartBanner.tsx:18`

**Interfaces:**
- Consumes: `NavBadgeKey` (Task 1); the existing banner hooks.
- Produces: `useAppBanners(opts: { allLimitedReset: string | null | undefined; onDismissAllLimited: () => void }): BannerStackItem[]`; `useNavBadges(): Record<NavBadgeKey, number | boolean | undefined>`.

**Context — the fragmentation this closes.** Banners are declared in **three** places today with different sets:

| Item | priority | App.tsx | Dashboard.tsx | DesktopShell.tsx |
|---|---|---|---|---|
| `daemon` | 90 | ✅ L192 | — | — |
| `git-credential` | 80 | ✅ L193 | — | — |
| `all-limited` | 70 | — | ✅ L657 | ✅ L637 |
| `input-hold` | 60 | — | ✅ L660 | **missing** |
| `deploy` | 50 | — | ✅ L663 | ✅ L640 |
| `update` | 30 | ✅ L191 | — | — |

`UpdatePendingBanner` (`input-hold`) is therefore **mobile-only** — a real bug this task fixes by construction. `all-limited` and `deploy` are duplicated, each shell carrying its own copy of the render-time and `setTimeout` auto-clear logic.

Per the spec deviation stated in Decisions: **one hook, not a mutable registry.**

`BannerStackItem` gains `tone` so kiosk can filter to `danger` only (Task 19). Tones: `daemon` and `all-limited` are `danger`; `git-credential` is `warning`; `update`, `input-hold` and `deploy` are `info`.

- [ ] **Step 1: Add `tone` to `BannerStackItem`**

In `web/src/ui/BannerStack.tsx`, extend the interface:

```ts
export interface BannerStackItem {
  id: string
  priority: number
  node: React.ReactNode
  /** Used by the shell to filter the stack (kiosk shows `danger` only).
   *  Optional so existing call sites keep compiling; treated as 'info'. */
  tone?: 'info' | 'warning' | 'danger'
}
```

Do **not** change `BannerStack`'s rendering — filtering is the shell's job, not the primitive's. Add one test in `web/src/ui/Banner.test.tsx` asserting `tone` is carried through untouched (i.e. `BannerStack` ignores it).

- [ ] **Step 2: Rename the colliding hook (DEC-7)**

```
git mv web/src/hooks/useDaemonHealth.ts web/src/hooks/useDaemonReachable.ts
```

Rename the exported function `useDaemonHealth` → `useDaemonReachable` inside it, update its doc comment to say explicitly that it is **not** `api/hooks.ts`'s `useDaemonHealth` (which returns a react-query result and is mocked in ~13 test files), and update the import in `web/src/components/DaemonRestartBanner.tsx:18`. Move/rename its test file to match if one exists.

Verify: `grep -rn "hooks/useDaemonHealth" web/src` prints nothing.

- [ ] **Step 3: Write the failing test for `useAppBanners`**

Create `web/src/shell/useAppBanners.test.tsx`. Mock every banner hook so the test asserts *composition*, not the hooks' own behaviour (each already has its own tests):

```tsx
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook } from '@testing-library/react'

vi.mock('@/hooks/useInputHold', () => ({ useInputHold: vi.fn() }))
vi.mock('@/lib/deployController', async (orig) => ({ ...(await orig<object>()) }))

import { useInputHold } from '@/hooks/useInputHold'
import { useAppBanners } from './useAppBanners'

const NONE = { allLimitedReset: undefined, onDismissAllLimited: () => {} }

describe('useAppBanners', () => {
  beforeEach(() => {
    vi.mocked(useInputHold).mockReturnValue({ holding: false, count: 0, mode: 'updating' })
  })

  it('declares every banner exactly once', () => {
    const { result } = renderHook(() => useAppBanners(NONE))
    const ids = result.current.map((i) => i.id)
    expect(new Set(ids).size).toBe(ids.length)
    expect(ids.sort()).toEqual(
      ['all-limited', 'daemon', 'deploy', 'git-credential', 'input-hold', 'update'].sort(),
    )
  })

  it('preserves the legacy priorities', () => {
    const { result } = renderHook(() => useAppBanners(NONE))
    const byId = Object.fromEntries(result.current.map((i) => [i.id, i.priority]))
    expect(byId).toEqual({
      daemon: 90, 'git-credential': 80, 'all-limited': 70,
      'input-hold': 60, deploy: 50, update: 30,
    })
  })

  it('assigns a tone to every item', () => {
    const { result } = renderHook(() => useAppBanners(NONE))
    for (const i of result.current) expect(i.tone, i.id).toBeDefined()
    const byId = Object.fromEntries(result.current.map((i) => [i.id, i.tone]))
    expect(byId.daemon).toBe('danger')
    expect(byId['all-limited']).toBe('danger')
    expect(byId['git-credential']).toBe('warning')
  })

  it('includes input-hold — the banner that was mobile-only before this PR', () => {
    const { result } = renderHook(() => useAppBanners(NONE))
    expect(result.current.some((i) => i.id === 'input-hold')).toBe(true)
  })

  it('renders no all-limited node until a reset time is supplied', () => {
    const { result } = renderHook(() => useAppBanners(NONE))
    expect(result.current.find((i) => i.id === 'all-limited')?.node).toBeNull()
  })
})
```

`renderHook` needs the app's providers (react-query at minimum). Look at how an existing hook test in `web/src/hooks/__tests__/` builds its wrapper and reuse that exact pattern — do not invent a new one.

- [ ] **Step 4: Run to verify it fails.**

- [ ] **Step 5: Write `useAppBanners`**

Create `web/src/shell/useAppBanners.ts`. It calls each banner hook once and returns the item array; `BannerStack` already sorts by priority descending and drops null nodes, so this hook does not sort.

```ts
import type { BannerStackItem } from '@/ui/BannerStack'
// …import the six banner components and useInputHold, exactly as App.tsx and
// Dashboard.tsx do today. Copy the import paths from those files verbatim.

export interface AppBannersOptions {
  /** ISO reset time for the all-limited banner, or null/undefined for hidden.
   *  Owned by the shell because both old shells owned a copy. */
  allLimitedReset: string | null | undefined
  onDismissAllLimited: () => void
}

/**
 * The one place app-level banners are declared.
 *
 * Before this, banners were declared in three components (App, Dashboard,
 * DesktopShell) with different sets — which is how `input-hold` came to render
 * on phones only, and how `all-limited` and `deploy` came to be duplicated with
 * two copies of the auto-clear logic.
 *
 * Priorities are carried over unchanged so the visual order does not move.
 */
export function useAppBanners({
  allLimitedReset,
  onDismissAllLimited,
}: AppBannersOptions): BannerStackItem[] {
  const inputHold = useInputHold()

  return [
    { id: 'daemon',         priority: 90, tone: 'danger',  node: <DaemonRestartBanner /> },
    { id: 'git-credential', priority: 80, tone: 'warning', node: <GitCredentialBanner /> },
    {
      id: 'all-limited', priority: 70, tone: 'danger',
      node: allLimitedReset !== undefined
        ? <AllLimitedBanner earliestReset={allLimitedReset} onDismiss={onDismissAllLimited} />
        : null,
    },
    {
      id: 'input-hold', priority: 60, tone: 'info',
      node: inputHold.holding
        ? <UpdatePendingBanner queuedCount={inputHold.count} mode={inputHold.mode} />
        : null,
    },
    { id: 'deploy', priority: 50, tone: 'info', node: <DeployPanel /> },
    { id: 'update', priority: 30, tone: 'info', node: <GlobalUpdateBanner /> },
  ]
}
```

The file contains JSX — name it `useAppBanners.tsx`.

Read `Dashboard.tsx:656–664` and `App.tsx:190–194` and copy the exact props each banner component is given today. Do not guess `UpdatePendingBanner`'s prop names — read them from `Dashboard.tsx:660–662`.

- [ ] **Step 6: Run to verify it passes.**

- [ ] **Step 7: Write `useNavBadges`**

Create `web/src/shell/useNavBadges.ts`, replacing `buildTabs`'s count computation (duplicated at `Dashboard.tsx:500–509` and `DesktopShell.tsx:422–431`):

```ts
import type { NavBadgeKey } from './navDefs'

export type NavBadges = Partial<Record<NavBadgeKey, number | boolean>>

/**
 * The counts the deleted `buildTabs(counts)` computed, in one place.
 *
 * DEC-4: `sessions` counts EVERY session in the section, including queued and
 * pending-approval rows, because those render inside the Sessions section too.
 * DesktopShell used `orderedSessions.length` (queued excluded) — that was an
 * artifact of it never rendering PendingApprovalSection.
 */
export function useNavBadges(): NavBadges {
  // …useSessions(), useArchivedSessions(), useAllPrs(), useAutomations(),
  //   and the needs-reauth check — copy each call and its import from
  //   Dashboard.tsx:500-509 verbatim.
  return {
    sessions: sessions.length || undefined,
    pipeline: inFlightPrCount(prs) || undefined,
    automations: automationsActiveSessions || undefined,
    archived: archivedSessions.length || undefined,
    settings: hasNeedsReauth || undefined,
  }
}
```

Test it (`useNavBadges.test.tsx`) by mocking the four query hooks and asserting: every key is a `NavBadgeKey`; a zero count is `undefined` (so no badge renders) rather than `0`; `settings` is boolean-or-undefined, never a number; and `sessions` counts queued sessions too — the DEC-4 decision, pinned so a later reader cannot quietly revert it.

- [ ] **Step 8: Run the gates and commit**

```
npm --prefix web test
npm --prefix web run typecheck
npm --prefix web run lint
git add web/src/shell/useAppBanners.tsx web/src/shell/useAppBanners.test.tsx web/src/shell/useNavBadges.ts web/src/shell/useNavBadges.test.tsx web/src/ui/BannerStack.tsx web/src/ui/Banner.test.tsx web/src/hooks web/src/components/DaemonRestartBanner.tsx
git commit -m "feat(shell): one declaration site for banners and nav badges; rename useDaemonReachable"
```

---

### Task 7: `NavRail` — `app.rail` for tablet and desktop

**Files:**
- Create: `web/src/shell/regions/NavRail.tsx`, `web/src/shell/regions/NavRail.test.tsx`

**Interfaces:**
- Consumes: `NAV_DEFS`, `navDefForPath` (Task 1); `useNavBadges` (Task 6); `useViewport` (Task 3); `useTheme` from `@/shell/theme`; `Button`, `IconButton`, `Menu`, `Badge` from `@/ui/`.
- Produces: `NavRail` (no props — it reads location and viewport itself).

**Contract (sub-spec B §4.2).** Items come from `NAV_DEFS`; badges from `useNavBadges()`. A bottom cluster holds **New session** (navigates to `/sessions` and focuses the dispatcher), **Assistant** (`?drawer=assistant`), **Help** (`?drawer=help`), **Theme** (a `Menu` of system/light/dark) and **Settings** (`/settings/general`). At `width === 'wide'` a persisted expand/collapse toggle shows labels. `role="navigation"`; `aria-current="page"` on the active item.

Notes that decide the implementation:
- The rail is icon-only at `tablet` and `desktop`; labels appear only when expanded, which is only offered at `wide`.
- Expansion is persisted at `ccswitch.railExpanded` (the `ccswitch.` namespace is a global constraint).
- Settings appears **both** as a nav item and in the bottom cluster in the spec's text. Render it **once**, in the bottom cluster, and exclude it from the main item loop — a duplicated nav target with two `aria-current` candidates is an accessibility bug. Record this in your report.
- "focuses the dispatcher" is PR 4 work. Here, **New session** navigates to `/sessions?dialog=new` (DEC-5), which every mode now honours.

- [ ] **Step 1: Write the failing test**

Create `web/src/shell/regions/NavRail.test.tsx`. Render inside a `MemoryRouter` plus the providers the rail needs, mocking `useNavBadges`:

```tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { NavRail } from './NavRail'

vi.mock('@/shell/useNavBadges', () => ({ useNavBadges: vi.fn(() => ({ sessions: 3, settings: true })) }))

function renderRail(path = '/sessions') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      {/* wrap in ViewportProvider + ThemeProvider exactly as AppShell does */}
      <NavRail />
    </MemoryRouter>,
  )
}

describe('NavRail', () => {
  it('is a landmark carrying the region id', () => {
    renderRail()
    const nav = screen.getByRole('navigation')
    expect(nav).toHaveAttribute('data-region', 'app.rail')
  })

  it('renders one link per nav def except Settings, which lives in the bottom cluster', () => {
    renderRail()
    const nav = screen.getByRole('navigation')
    for (const label of ['Sessions', 'Pipeline', 'Automations', 'Initiatives', 'Products', 'Archived', 'Insights']) {
      expect(within(nav).getByRole('link', { name: new RegExp(label) })).toBeInTheDocument()
    }
    expect(within(nav).getAllByRole('link', { name: /Settings/ })).toHaveLength(1)
  })

  it('marks the active section with aria-current', () => {
    renderRail('/pipeline')
    expect(screen.getByRole('link', { name: /Pipeline/ })).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('link', { name: /Sessions/ })).not.toHaveAttribute('aria-current')
  })

  it('marks the section active on a nested route', () => {
    renderRail('/sessions/abc123')
    expect(screen.getByRole('link', { name: /Sessions/ })).toHaveAttribute('aria-current', 'page')
  })

  it('shows a badge count and folds it into the accessible name', () => {
    renderRail()
    expect(screen.getByRole('link', { name: /Sessions/ }).textContent).toContain('3')
  })

  it('opens the help drawer via the URL, not local state', async () => {
    renderRail()
    await userEvent.click(screen.getByRole('button', { name: /Help/ }))
    // assert the router location gained ?drawer=help — use a location probe
    // component rendered as a sibling; see routes.test.tsx for the pattern.
  })

  it('every control meets the 44px touch target', () => {
    renderRail()
    for (const el of screen.getAllByRole('link')) {
      expect(el.className).toMatch(/min-h-target/)
    }
  })

  it('has no axe violations', async () => {
    const { container } = renderRail()
    // NOTE: jsdom applies no stylesheet, so axe covers roles and names here,
    // NOT colour contrast. Token contrast is covered by src/__tests__/contrast.test.ts.
    expect(await axe(container)).toHaveNoViolations()
  })
})
```

Fill in the provider wrapper and the location probe by copying the patterns already used in `web/src/App.test.tsx` — do not invent new helpers.

- [ ] **Step 2: Run to verify it fails.**

- [ ] **Step 3: Implement `NavRail`**

Key requirements, all load-bearing:

```tsx
<nav data-region="app.rail" role="navigation" aria-label="Sections" className="…z-nav…">
```

- Iterate `NAV_DEFS.filter((d) => d.id !== 'settings')` for the main cluster.
- Active detection uses `navDefForPath(location.pathname)?.id === def.id` — **not** `pathname === def.path`, so `/sessions/abc` keeps Sessions active. This is why `navDefForPath` exists.
- Each item is a react-router `<Link>` (or `NavLink`), never a `<button>` with `navigate()` — a nav target must be middle-clickable and copyable.
- `aria-current="page"` on the active link only.
- Badge: render `<Badge>` when the value is a number; render a warning dot plus the word "needs attention" in the accessible name when the value is `true` (that is what `settings` supplies). Copy the accessible-name convention from the deleted `TabBar` so screen-reader output does not change — read `web/src/components/TabBar.tsx` for the exact string.
- Icon size 20; `aria-hidden="true"` on the icon; the label is the accessible name whether or not it is visible (use a visually-hidden span when collapsed rather than `aria-label`, so the visible text and the accessible name cannot drift — the M6 lesson from PR 2).
- Bottom cluster order: New session, Assistant, Help, Theme, Settings.
- Expand toggle rendered only when `useViewport().width === 'wide'`; state persisted at `ccswitch.railExpanded` via a small `useLocalStorage`-style read/write in the same file (grep first: if `web/src/lib/` already has a persisted-boolean helper, use it).
- All interactive elements carry `min-h-target min-w-target`.
- Tokens only: `bg-surface`, `border-edge`, `text-ink-muted`, `text-ink`, `bg-accent-wash` for the active item. No raw palette, no `dark:`.

- [ ] **Step 4: Run to verify it passes. Then run the gates and commit.**

```
git add web/src/shell/regions/NavRail.tsx web/src/shell/regions/NavRail.test.tsx
git commit -m "feat(shell): NavRail region — nav from navDefs, badges from useNavBadges"
```

---

### Task 8: `NavBar` and `TopBar` — `app.rail` on phone

**Files:**
- Create: `web/src/shell/regions/NavBar.tsx`, `web/src/shell/regions/TopBar.tsx`, `web/src/shell/regions/PhoneNav.test.tsx`

**Interfaces:**
- Consumes: same as Task 7, plus `Sheet` from `@/ui/Sheet`.
- Produces: `NavBar`, `TopBar`.

**Contract (sub-spec B §4.2).** `NavBar` is the bottom bar on phone: the **first four** `NAV_DEFS` items plus a **More** control opening a `Sheet` that lists the rest and the global actions. `TopBar` is the slim top bar carrying brand + **Assistant**/**Help** icons. The page title is the page's own `h1`, not the top bar's — so `TopBar` must not render an `h1`.

Both carry `data-region="app.rail"`. **They never render at the same time as `NavRail`**, and on phone both render together — which means `app.rail` legitimately appears **twice** on phone. Task 16's singleton test must therefore assert *at most one rail component per mode*, not *at most one `data-region="app.rail"` node*. Encode that here so Task 16 does not discover it late: give `TopBar` `data-region="app.rail"` and `data-rail="top"`, and `NavBar` `data-region="app.rail"` and `data-rail="bottom"`.

- [ ] **Step 1: Write the failing test** — `PhoneNav.test.tsx` covering both components:
  - `NavBar` renders exactly the first four `NAV_DEFS` ids as links, in order.
  - The fifth control is **More**, and it is a `button`, not a link.
  - Clicking More opens a sheet listing every remaining nav item **and** Settings.
  - `aria-current="page"` follows the route, including nested routes.
  - `TopBar` renders the brand and the Assistant + Help buttons and **no `h1`** (`expect(within(topbar).queryByRole('heading', { level: 1 })).toBeNull()`).
  - Both carry `data-region="app.rail"` with distinct `data-rail` values.
  - Every control meets `min-h-target`.
  - Safe-area: `NavBar`'s outer element carries a bottom padding class that references `env(safe-area-inset-bottom)`. Grep `web/src` for how the existing mobile chrome does this (`Dashboard`'s header uses `paddingTop: calc(env(safe-area-inset-top) + 1rem)` as an inline style at `App.tsx:47–74`) and match that mechanism rather than inventing a class.
  - axe clean, with the same "roles and names only" comment as Task 7.

- [ ] **Step 2–4: Run red, implement, run green.**

Implementation notes: the More sheet is `ui/Sheet` with `side="bottom"` if it supports it, otherwise the default; its items are `NAV_DEFS.slice(4)` plus the same global actions as `NavRail`'s bottom cluster. Reuse `NavRail`'s item renderer by extracting it into a shared `NavItem` component in `NavRail.tsx` and importing it here — do **not** write a second item renderer.

- [ ] **Step 5: Gates and commit**

```
git add web/src/shell/regions/NavBar.tsx web/src/shell/regions/TopBar.tsx web/src/shell/regions/PhoneNav.test.tsx web/src/shell/regions/NavRail.tsx
git commit -m "feat(shell): NavBar and TopBar — the phone form of app.rail"
```

---

### Task 9: `SessionListColumn` — synthesize one list from two divergent copies

**Files:**
- Create: `web/src/features/sessions/SessionListColumn.tsx`, `web/src/features/sessions/SessionListColumn.test.tsx`

**Interfaces:**
- Consumes: existing `SessionListControls`, `QueuedSection`, `PendingApprovalSection`, `SessionList`, `AdoptPanel`, `DaemonHealthIndicator` (all in `web/src/components/`, unchanged); `isPreSpawnStatus`; `useSessionSearch`, `useSessionOrdering`, `useListNavigation`.
- Produces: `SessionListColumn` — props `{ selectedId: string | null; onOpen: (id: string) => void; … }`. **Define the exact prop list from what the two shells actually pass** (see below) and record it in your report so Task 10 can consume it.

**Context — this component does not exist.** It must be synthesized from two divergent markups:

| Concern | `Dashboard.tsx` | `DesktopShell.tsx` | This component |
|---|---|---|---|
| `PendingApprovalSection` | ✅ L738 | ❌ absent | **include** |
| `DaemonHealthIndicator` | ✅ L690 | ❌ absent | **include** |
| Queued filter | `isPreSpawnStatus(s.status)` L423 | `s.status === 'queued'` L146 | **`isPreSpawnStatus`** (DEC-3) |
| `SessionList selectedId` | `highlightedSession?.id ?? null` L474 | `selectedSessionId ?? highlightedSession?.id` L507 | **desktop's form** — the open session wins over the keyboard highlight, falling back to the highlight |
| `SessionListControls` gate | `sessions.length > 0` L726 | `sessions.length > 0` L479 | same |
| `AdoptPanel` gate | `config?.adoptionEnabled` L764 | `config?.adoptionEnabled` L529 | same |
| Scroll container | plain div | `data-testid="session-list-scroll"` L493 | **keep the testid** — `DesktopListScroll.test.tsx` uses it and Task 18 re-pins those assertions |

Order (from `DesktopShell.tsx:451–532`, which is the fuller of the two): New Session button → `SessionListControls` → scroll container { `PendingApprovalSection`, `QueuedSection`, `SessionList`, `AdoptPanel` } → `DaemonHealthIndicator`.

**Do not port the `listScrollTop` / `restoreListScroll` callback-ref workaround** (`DesktopShell.tsx:186–195, 493–494`). It exists solely to survive the keyed-provider remount that Task 11 removes. Deleting it is the point. Task 18 re-pins the *behaviour* (scroll position survives opening a session) against the new structure, where it holds for free because the list no longer remounts.

- [ ] **Step 1: Write the failing test** covering, at minimum:
  - renders `PendingApprovalSection` when a pending-approval session exists (**the desktop regression this fixes**);
  - renders `DaemonHealthIndicator` (ditto);
  - a pre-spawn session appears in `QueuedSection` and **not** in the main `SessionList` — pin `isPreSpawnStatus`, and add a case for a status that is pre-spawn but not literally `'queued'`, which is the exact behaviour desktop lacked;
  - `selectedId` prefers the open session over the keyboard highlight;
  - `SessionListControls` is hidden when there are no sessions;
  - `AdoptPanel` respects `config.adoptionEnabled` both ways;
  - the scroll container keeps `data-testid="session-list-scroll"`;
  - axe clean.

Read `web/src/session/isPreSpawnStatus` (grep for it) to pick real statuses for the fixtures rather than inventing strings.

- [ ] **Step 2–4: Run red, implement, run green.**

- [ ] **Step 5: Report the prop signature.** Task 10 consumes it; write the exact `interface SessionListColumnProps` into your report.

- [ ] **Step 6: Gates and commit**

```
git add web/src/features/sessions/SessionListColumn.tsx web/src/features/sessions/SessionListColumn.test.tsx
git commit -m "feat(sessions): SessionListColumn — one list column, union of both shells"
```

---

### Task 10: `ContextList` — `app.list`, and the `listWidth` migration

**Files:**
- Create: `web/src/shell/regions/ContextList.tsx`, `web/src/shell/regions/ContextList.test.tsx`
- Modify: `web/src/lib/sidebarWidth.ts` (storage key + migration), `web/src/lib/__tests__/sidebarWidth.test.ts`

**Interfaces:**
- Consumes: `SessionListColumn` (Task 9); `navDefForPath` (Task 1); `useViewport` (Task 3); existing `SidebarResizer`, `useSidebarWidth`.
- Produces: `ContextList`.

**Contract.** `ContextList` renders the list provider for the current section. Only Sessions has one (`NAV_DEFS[…].hasList`). Resizable on desktop via the existing `SidebarResizer`. **Scroll position survives route changes because `ContextList` never remounts on `:id` change** — that is the whole point, and it is what makes Task 9's dropped workaround safe.

- [ ] **Step 1: The `listWidth` migration (DEC-11)**

In `web/src/lib/sidebarWidth.ts`, change the key and add a one-way migration. Keep `SIDEBAR_MIN = 280`, `SIDEBAR_MAX = 560`, `SIDEBAR_DEFAULT = 320` and the exported names **unchanged** — `SessionCard.tsx:271–277` reasons from `SIDEBAR_MAX = 560` and `SessionCard.layout.test.tsx:153` asserts on the consequence.

```ts
export const SIDEBAR_STORAGE_KEY = 'ccswitch.listWidth'
/** Pre-PR-3 key. Read once, then written forward under the new key. */
const LEGACY_STORAGE_KEY = 'ccswitch.sidebarWidth'

export function loadSidebarWidth(): number {
  try {
    const current = localStorage.getItem(SIDEBAR_STORAGE_KEY)
    if (current !== null) return clampSidebarWidth(Number(current))
    const legacy = localStorage.getItem(LEGACY_STORAGE_KEY)
    if (legacy !== null) {
      const px = clampSidebarWidth(Number(legacy))
      // Migrate forward, then drop the old key so this runs at most once.
      localStorage.setItem(SIDEBAR_STORAGE_KEY, String(px))
      localStorage.removeItem(LEGACY_STORAGE_KEY)
      return px
    }
  } catch { /* private mode / disabled storage */ }
  return SIDEBAR_DEFAULT
}
```

Add tests to `web/src/lib/__tests__/sidebarWidth.test.ts`: a legacy value is migrated and the old key removed; the new key wins when both exist; a corrupt legacy value falls back to the default and still clears the old key; nothing throws when `localStorage` throws.

- [ ] **Step 2: Write the failing `ContextList` test**
  - carries `data-region="app.list"`;
  - renders `SessionListColumn` on `/sessions` and on `/sessions/:id`;
  - renders **nothing** on `/pipeline` (a section with `hasList: false`) — assert the region node is absent, not merely empty;
  - renders `SidebarResizer` at `desktop`/`wide` and **not** at `phone`/`tablet`;
  - **the list does not remount when `:id` changes**: render at `/sessions/a`, write a sentinel onto the scroll node (e.g. set `scrollTop`, or attach a data attribute via a ref), rerender at `/sessions/b`, and assert the same DOM node is still there. Use `expect(nodeBefore).toBe(nodeAfter)` on the element captured by `getByTestId('session-list-scroll')` — identity, not just presence. This is the assertion that makes Task 9's dropped workaround provably unnecessary.

- [ ] **Step 3–4: Run red, implement, run green.**

- [ ] **Step 5: Gates and commit**

```
git add web/src/shell/regions/ContextList.tsx web/src/shell/regions/ContextList.test.tsx web/src/lib/sidebarWidth.ts web/src/lib/__tests__/sidebarWidth.test.ts
git commit -m "feat(shell): ContextList region; migrate sidebarWidth to ccswitch.listWidth"
```

---

### Task 11: `PageHost` — `app.main`, and the provider-scope fix (F2.3)

**Files:**
- Create: `web/src/shell/regions/PageHost.tsx`, `web/src/shell/regions/PageHost.test.tsx`
- Modify: `web/src/components/session/SessionView.tsx:49`

**Interfaces:**
- Consumes: `SessionRuntimeProvider` (`web/src/components/session/SessionRuntimeProvider.tsx`); `useNeedsYouTitle` (Task 2); `useSessions`.
- Produces: `PageHost`.

**Context — F2.3 has two halves, and the spec only names one.**

*Half A, desktop over-scoping.* `DesktopShell.tsx:673–677` wraps the **entire shell** — header, tab bar, banners, the whole sidebar, the resizer, the main pane, the bottom prompt and all three dialogs — in `<SessionRuntimeProvider key={selection.id}>`. Selecting a different session changes the `key`, so the whole subtree remounts; opening or closing a session mounts/unmounts the provider, remounting both ways. The `listScrollTop` + `restoreListScroll` callback-ref pair (L186–195, 493–494) exists purely to paper over that, and `DesktopListScroll.test.tsx` pins the workaround.

*Half B, mobile never uses the provider at all.* `SessionView.tsx:49` calls `useSessionRuntime(sessionId, initialPrompt)` **directly** and threads `rt` down as a prop. So the same runtime is obtained two different ways depending on width.

The fix is one provider, in one place, wrapping only what needs it: the routed page **and** `PromptDock` (the composer needs the same runtime the transcript has). `ContextList` stays outside — which is what makes Task 10's identity assertion pass and Task 9's dropped workaround safe.

`useNeedsYouTitle` moves here (from `Dashboard.tsx:428`) so every mode gets the needs-you count, not just phones. After Task 2 it is a *contributor*, so this cannot clobber a page title.

- [ ] **Step 1: Write the failing test**

`PageHost.test.tsx` must assert:
- carries `data-region="app.main"`;
- renders the routed `<Outlet/>` content;
- on `/sessions/:id`, the outlet is wrapped in `SessionRuntimeProvider` with the session id — assert by rendering a probe child that calls `useSessionRuntimeContext()` and does **not** throw;
- on `/sessions` (no id) the probe **does** throw (no provider) — i.e. the provider is not mounted when no session is open;
- **changing `:id` does not remount the outlet's parent chrome**: mock `SessionRuntimeProvider` as a pass-through that records its `key`/`sessionId`, rerender from `/sessions/a` to `/sessions/b`, and assert the runtime provider saw a new session id;
- `useNeedsYouTitle` runs here: with two needs-you sessions mocked, `document.title` contains `(2)`.

Copy the `SessionRuntimeProvider` mocking pattern from `web/src/components/__tests__/DesktopAdvanceOnArchive.test.tsx:76–77`, which already does exactly this.

- [ ] **Step 2: Run to verify it fails.**

- [ ] **Step 3: Implement `PageHost`**

```tsx
/**
 * app.main — the routed page, plus the session runtime when one is open.
 *
 * SCOPE IS THE POINT (audit F2.3). The provider wraps the outlet and the dock,
 * and nothing else. It deliberately does NOT wrap ContextList: the old desktop
 * shell keyed the whole shell on the session id, so selecting a session
 * remounted the session list and lost its scroll position — which the deleted
 * `listScrollTop`/`restoreListScroll` callback-ref pair existed to paper over.
 * With the provider scoped here, the list simply never remounts.
 */
export function PageHost({ dock }: { dock?: React.ReactNode }) {
  const { id } = useParams()
  const { data: sessions = [] } = useSessions()
  useNeedsYouTitle(sessions)

  const body = (
    <div data-region="app.main" className="flex-1 min-h-0 flex flex-col">
      <Outlet />
      {dock}
    </div>
  )

  return id ? (
    <SessionRuntimeProvider key={id} sessionId={id}>{body}</SessionRuntimeProvider>
  ) : body
}
```

Confirm against `routes.tsx` (Task 15) that `useParams().id` is the right param name — if the route is declared `/sessions/:sessionId`, use that. Read the route table before writing this line.

The `dock` prop exists so `AppShell` can hand `PromptDock` in and have it land *inside* the provider. Do not import `PromptDock` here — that would make `PageHost` decide dock visibility, which is the shell's job.

- [ ] **Step 4: Run to verify it passes.**

- [ ] **Step 5: Fix half B — route `SessionView` through the provider**

In `web/src/components/session/SessionView.tsx:49`, replace the direct `useSessionRuntime(sessionId, initialPrompt)` call with `useSessionRuntimeContext()`. `SessionView` is now always rendered under `PageHost`'s provider.

`initialPrompt` currently reaches `useSessionRuntime` through `SessionView`'s props. Trace where it comes from (`App.tsx:59–61` puts it in router `state`) and pass it to `SessionRuntimeProvider` in `PageHost` instead — read it with `useLocation().state`. Assert in the test that a session opened with an initial prompt still seeds it, because `SessionView.test.tsx` has two existing tests for exactly that ("initialPrompt seeding" and "no duplicate on echo") that Task 18 must keep green.

This is the riskiest edit in the task. If `initialPrompt` turns out to be threaded some other way, **stop and report** rather than guessing — say what you found.

- [ ] **Step 6: Gates and commit**

```
git add web/src/shell/regions/PageHost.tsx web/src/shell/regions/PageHost.test.tsx web/src/components/session/SessionView.tsx
git commit -m "fix(shell): PageHost scopes SessionRuntimeProvider to the page and dock (F2.3)"
```

---

### Task 12: `PromptDock` — `app.dock`

**Files:**
- Create: `web/src/shell/regions/PromptDock.tsx`, `web/src/shell/regions/PromptDock.test.tsx`

**Interfaces:**
- Consumes: `useSessionRuntimeContext`; existing `DispatchBar` and `SessionComposer`.
- Produces: `PromptDock`.

**Contract (sub-spec B §4.2).** `DispatchBar` when no session is open; `SessionComposer` (from the runtime context) when one is. Safe-area bottom padding on phone. Hidden in kiosk (the shell decides that, via `REGION_VISIBILITY` — `PromptDock` itself does not check the mode). Appears **only in the Sessions section** (D10) — again the shell's decision, not the dock's.

This replaces `DesktopShell`'s `bottomPrompt` (L559–587) and `BottomComposer` (L117–120), and `Dashboard`'s `DispatchBar` at L767–792.

**DEC-2 applies here:** the fleet-host picker (`hosts={enabledFleetHosts}` and the `host` create-param) is mobile-only today (`Dashboard.tsx:434–439, 621–632, 773`). One dock means one `DispatchBar`, so desktop gains it. Port the `useQuery(['fleet','hosts'])` call and the `host` argument exactly as `Dashboard.tsx` has them.

- [ ] **Step 1: Write the failing test** asserting: `data-region="app.dock"`; renders `DispatchBar` with no open session; renders `SessionComposer` when a session runtime is present; passes `hosts` to `DispatchBar` (the DEC-2 behaviour, pinned so it cannot silently regress to mobile-only); the safe-area padding is present.

- [ ] **Step 2–4: Run red, implement, run green.**

- [ ] **Step 5: Gates and commit**

```
git add web/src/shell/regions/PromptDock.tsx web/src/shell/regions/PromptDock.test.tsx
git commit -m "feat(shell): PromptDock region; the fleet-host picker is no longer phone-only"
```

---

### Task 13: `DrawerHost` and `HelpSheet` — `app.drawer`

**Files:**
- Create: `web/src/shell/regions/DrawerHost.tsx`, `web/src/shell/regions/HelpSheet.tsx`, `web/src/shell/regions/DrawerHost.test.tsx`
- Delete: `web/src/components/HelpDrawer.tsx`

**Interfaces:**
- Consumes: `Sheet`, `Kbd` from `@/ui/`; `shortcutsByGroup` from `@/lib/shortcuts`; `NAV_DEFS` (Task 1); `useViewport` (Task 3).
- Produces: `DrawerHost`, `HelpSheet`.

**Contract (sub-spec B §4.2, D13).** One `Sheet`; content selected by `?drawer=`: `help` → `HelpSheet`, `assistant` → a static "Assistant is coming" sheet with **no controls** (PR 12 replaces it). Width persisted at `ccswitch.drawerWidth`. The drawer stays mounted while `?drawer=` is set across route changes, and `AppShell` keeps the param when navigating sections. At `width === 'wide'` or `desktop` ≥1280 it is a grid column (push); otherwise an overlay sheet.

**`HelpDrawer` is deleted, and its content is regenerated rather than copied.** Today's `components/HelpDrawer.tsx` has a hand-maintained `NAV_MAP` (L5–11) listing "Sessions / Accounts / Archived / Session view / Review" — already stale against `TAB_DEFS` (audit finding F1.3). `HelpSheet` derives its nav section from `NAV_DEFS` so it cannot go stale again.

Its TIPS also claim **"Shift+Tab cycles the permission mode"**, which D8 removes. Do **not** carry that line over. The `Shift+Tab` binding itself is removed in PR 4 (the shortcuts registry); here, only the help text changes. Note the discrepancy in your report: between this PR and PR 4, `Shift+Tab` still works but is no longer documented. That is the right order — documenting a shortcut that still exists is a smaller sin than the reverse, and PR 4 lands immediately after.

- [ ] **Step 1: Write the failing test**
  - `data-region="app.drawer"`;
  - absent entirely when `?drawer=` is unset (assert the node is not in the document);
  - `?drawer=help` renders the help content; `?drawer=assistant` renders the placeholder;
  - `?drawer=bogus` renders **nothing** (an unknown value must not open an empty sheet);
  - the help sheet lists every `NAV_DEFS` label — table-driven from `NAV_DEFS`, so adding a section without updating help fails the test;
  - the help sheet does **not** mention `Shift+Tab`;
  - closing the sheet clears `?drawer=` and uses `replace`, not `push` (assert the history length did not grow);
  - the assistant placeholder contains no `button`, `input` or `textarea`;
  - axe clean.

- [ ] **Step 2–4: Run red, implement, run green.**

- [ ] **Step 5: Delete `HelpDrawer` and rewire its two mounts**

`git rm web/src/components/HelpDrawer.tsx`. It is mounted at `Dashboard.tsx:908` and `DesktopShell.tsx:669`; both files are deleted in Task 17, so if those mounts are still live at this point, leave them — Task 17 removes them with the files. Confirm with `grep -rn "HelpDrawer" web/src` that only those two (plus tests) reference it, and list what you found in your report.

`useGlobalHelpKey` (the `?`/`Cmd+/` listener) currently opens the drawer via local state in both shells. Point it at `?drawer=help` instead. It moves into the shortcuts registry in PR 4; here it just needs to keep working.

- [ ] **Step 6: Gates and commit**

```
git add web/src/shell/regions/DrawerHost.tsx web/src/shell/regions/HelpSheet.tsx web/src/shell/regions/DrawerHost.test.tsx
git rm web/src/components/HelpDrawer.tsx
git commit -m "feat(shell): DrawerHost + HelpSheet derived from navDefs; delete the stale HelpDrawer"
```

---

### Task 14: `Overlays` — `app.overlays`

**Files:**
- Create: `web/src/shell/regions/Overlays.tsx`, `web/src/shell/regions/Overlays.test.tsx`
- Modify: `web/src/main.tsx:25` (move `Toaster`)

**Interfaces:**
- Consumes: `getOverlayRoot` from `@/ui/portal`; existing `DeployOverlay`, `CommandResultOverlay`, `Toaster` (sonner).
- Produces: `Overlays`.

**Contract (umbrella §4.2).** The portal target for `Dialog`/`Sheet`/`Menu`, plus `Toaster`, `DeployOverlay` and `CommandResultOverlay`.

Two facts from the survey that shape this:
- `Toaster` lives at `main.tsx:25`, **inside** `<BrowserRouter>` but **outside** `<ErrorBoundary>`. Moving it into `Overlays` puts it inside the error boundary. That is a behaviour change: a crash in the app tree would now take the toaster with it. **Keep it in `main.tsx`** and have `Overlays` render only the portal root, `DeployOverlay` and `CommandResultOverlay`. Record this deviation from the spec in your report with that reasoning.
- `DeployOverlay` is currently at `App.tsx:218`, inside `DeployControllerProvider`. `Overlays` must therefore render inside that provider too, or the provider must move up. Check which and say so.
- `ui/portal.ts`'s `getOverlayRoot()` already creates and returns the `#overlays` node lazily (from PR 2). `Overlays` must **not** create a second one — call `getOverlayRoot()` and let it own the node. Grep `web/src/ui/portal.ts` and confirm before writing.

- [ ] **Step 1: Write the failing test** asserting: `data-region="app.overlays"` exists; `getOverlayRoot()` returns a node that is a child of it (or is it); rendering `Overlays` twice does not produce two overlay roots; `DeployOverlay` and `CommandResultOverlay` mount.

- [ ] **Step 2–4: Run red, implement, run green.**

- [ ] **Step 5: Gates and commit**

```
git add web/src/shell/regions/Overlays.tsx web/src/shell/regions/Overlays.test.tsx
git commit -m "feat(shell): Overlays region — one portal root, deploy and command-result overlays"
```

---

### Task 15: `routes.tsx` — the route table and every legacy redirect

**Files:**
- Create: `web/src/shell/routes.tsx`, `web/src/shell/routes.test.tsx`
- Modify: `web/src/components/settings/SettingsView.tsx` (accept an optional `section` prop — DEC-14)

**Interfaces:**
- Consumes: `AppShell` (Task 16) as the layout route. **Task 16 runs after this one**, so build the table against a placeholder layout element that this task's test supplies, and Task 16 wires the real one. Export the route array, not a router instance, so the test can mount it in a `MemoryRouter`.
- Produces: `const ROUTES: RouteObject[]`, and `LEGACY_REDIRECTS` (exported for the test to iterate).

**The table (umbrella §6, verbatim).**

| Path | Renders |
|---|---|
| `/` | redirect → `/sessions` |
| `/sessions` | Sessions page (list; welcome pane on desktop) |
| `/sessions/:id` | Sessions page with the session open |
| `/sessions/:id/agents/:agentId` | Sessions page, Tasks pane focused on the sub-agent |
| `/pipeline` | Pipeline page |
| `/automations` | Automations page |
| `/initiatives` | Initiatives page |
| `/projects` | Products page |
| `/insights` | Insights page |
| `/archived` | Archive list |
| `/archived/:id` | Read-only review (DEC-6) |
| `/settings` | redirect → `/settings/general` |
| `/settings/:section` | Settings page |
| `*` | redirect → `/sessions` |

**Legacy redirects (all `replace`).**

| From | To |
|---|---|
| `/session/:sessionId` | `/sessions/:sessionId` |
| `/session/:sessionId/subagent/:agentId` | `/sessions/:sessionId/agents/:agentId` |
| `/review/:sessionId` | `/archived/:sessionId` |
| `/accounts` | `/settings/accounts` |
| `/automation` | `/settings/automation` |
| `/library` | `/settings/:section` from `?section=`, else `/settings/general` |
| `/settings?section=X` | `/settings/X` |
| `?dialog=settings` (on any path) | `/settings/general`, param dropped |

Note the singular→plural change: every existing session URL a user has bookmarked is `/session/:id`, and every existing review URL is `/review/:id`. These redirects are the only thing keeping those bookmarks alive — do not skip them, and do not make them `push`.

- [ ] **Step 1: Write the failing test**

`routes.test.tsx` should be table-driven over the two tables above. Render `ROUTES` in a `MemoryRouter` with a `LocationProbe` (a component that renders `useLocation().pathname + useLocation().search` into a testid) and assert:

```tsx
describe('route table', () => {
  it.each([
    ['/',            '/sessions'],
    ['/session/abc', '/sessions/abc'],
    ['/session/abc/subagent/x', '/sessions/abc/agents/x'],
    ['/review/abc',  '/archived/abc'],
    ['/accounts',    '/settings/accounts'],
    ['/automation',  '/settings/automation'],
    ['/settings',    '/settings/general'],
    ['/library',     '/settings/general'],
    ['/nope',        '/sessions'],
  ])('%s redirects to %s', (from, to) => { /* … */ })

  it('/settings?section=accounts redirects to /settings/accounts', () => { /* … */ })
  it('/library?section=skills redirects to /settings/skills', () => { /* … */ })

  it.each(['/sessions', '/sessions/abc', '/sessions/abc/agents/x', '/pipeline',
           '/automations', '/initiatives', '/projects', '/insights',
           '/archived', '/archived/abc', '/settings/general'])(
    '%s renders without redirecting', (path) => { /* assert location is unchanged */ })

  it('every NAV_DEFS path is a real route', () => {
    for (const d of NAV_DEFS) { /* render d.path, assert no redirect away from it */ }
  })

  it('redirects replace rather than push', () => {
    // Mount at /session/abc, then assert going Back does not return to /session/abc.
  })
})
```

That last assertion matters: a pushed redirect makes the Back button bounce the user forward again, which is the classic redirect-loop bug.

The `every NAV_DEFS path is a real route` test is the guard that keeps `navDefs.ts` and `routes.tsx` from drifting — the failure mode where a rail item 404s.

- [ ] **Step 2: Run to verify it fails.**

- [ ] **Step 3: Implement `routes.tsx`**

Use `createBrowserRouter`-compatible `RouteObject[]` with `AppShell` as the layout route and the section pages as children, so the shell does not unmount on navigation:

```tsx
export const ROUTES: RouteObject[] = [
  {
    element: <AppShell />,
    children: [
      { path: '/', element: <Navigate to="/sessions" replace /> },
      { path: '/sessions', element: <SessionsPage /> },
      { path: '/sessions/:id', element: <SessionsPage /> },
      { path: '/sessions/:id/agents/:agentId', element: <SessionsPage /> },
      // …one entry per section page, hosting the EXISTING component unchanged…
      { path: '/settings', element: <Navigate to="/settings/general" replace /> },
      { path: '/settings/:section', element: <SettingsView /> },
      // …legacy redirects…
      { path: '*', element: <Navigate to="/sessions" replace /> },
    ],
  },
]
```

Parameterised redirects need a small component each, because `<Navigate>` cannot interpolate params:

```tsx
function RedirectSession() {
  const { sessionId } = useParams()
  return <Navigate to={`/sessions/${sessionId}`} replace />
}
```

Write one per parameterised redirect (`/session/:sessionId`, `/session/:sessionId/subagent/:agentId`, `/review/:sessionId`, `/library`). `LibraryRedirect` already exists at `App.tsx:33–37` — move it here rather than rewriting it, adjusting its target from `/settings?section=X` to `/settings/X`.

**Which components do the section routes render?** PR 3 hosts the existing pages **unchanged**. Read `DesktopShell.tsx:534–555` (`mainPane`) for the current mapping — `PipelineTab`, `AutomationsTab`, `InitiativesTab`, `ProjectsTab`, `InsightsView`, `SettingsView`, `ArchivedManager`, `ReviewView` — and use exactly those. `SessionsPage` does not exist yet; for this PR it is a thin wrapper that renders `SessionView` when `:id` is present and `WelcomePane` otherwise (both lifted from `DesktopShell`). Sub-spec B PR 4 replaces it with the real workspace.

- [ ] **Step 4: DEC-14 — make `SettingsView` accept a path section**

`SettingsView.tsx:49` reads `?section=` via `useSearchParams`. Add an optional `section?: string` prop that takes precedence over the search param, and have the `/settings/:section` route pass `useParams().section`. Do **not** rewrite `SettingsView`'s internals — the full Settings migration is PR 5 (sub-spec C). Keep `?section=` working so nothing in-flight breaks.

- [ ] **Step 5: Run to verify it passes. Gates. Commit.**

```
git add web/src/shell/routes.tsx web/src/shell/routes.test.tsx web/src/components/settings/SettingsView.tsx
git commit -m "feat(shell): the route table — every mode, with legacy redirects for old bookmarks"
```

---

### Task 16: `AppShell` — regions, the visibility matrix, and the layout per mode

**Files:**
- Create: `web/src/shell/AppShell.tsx`, `web/src/shell/AppShell.test.tsx`, `web/src/shell/visibilityMatrix.test.tsx`

**Interfaces:**
- Consumes: every region (Tasks 6–14); `useViewport`, `ViewportProvider` (Task 3); `readKioskConfig` (Task 4); `InteractionProvider` (Task 5); `navDefForPath` (Task 1).
- Produces: `AppShell`, and **`REGION_VISIBILITY`** — exported, because `visibilityMatrix.test.tsx` uses the spec table as its fixture.

**The matrix (umbrella §4.2, verbatim).** `1` = rendered, `0` = not, `D` = danger-tone only.

| Region | phone | tablet | desktop | kiosk |
|---|---|---|---|---|
| `app.banners` | 1 | 1 | 1 | D |
| `app.rail` | 1 (`NavBar` + `TopBar`) | 1 (`NavRail`) | 1 (`NavRail`) | 0 |
| `app.list` | 1 when no item open | 1 when no item open | 1 | 0 |
| `app.main` | 1 | 1 | 1 | 1 |
| `app.dock` | 1 in Sessions | 1 in Sessions | 1 in Sessions | 0 |
| `app.drawer` | 1 when `?drawer=` | 1 when `?drawer=` | 1 when `?drawer=` (push ≥1280, else overlay) | 0 |
| `app.overlays` | 1 | 1 | 1 | 1 (dialogs refused) |

Three cells are conditional on more than the mode, and the conditions are exact:
- `app.list` renders when `navDefForPath(pathname)?.hasList` **and** (mode is `desktop`, or no item is open). "An item is open" means the route has an `:id` param.
- `app.dock` renders when the current section is `sessions` (D10).
- `app.drawer` renders when `?drawer=` is a recognised value.

Express this as data, not as nested JSX conditionals:

```ts
export type RegionId =
  | 'app.banners' | 'app.rail' | 'app.list'
  | 'app.main' | 'app.dock' | 'app.drawer' | 'app.overlays'

export interface RegionContext { mode: LayoutMode; hasList: boolean; itemOpen: boolean; inSessions: boolean; drawer: string | null }

export const REGION_VISIBILITY: Record<RegionId, (c: RegionContext) => boolean> = {
  'app.banners':  () => true,               // kiosk filters by tone, it does not hide the region
  'app.rail':     (c) => c.mode !== 'kiosk',
  'app.list':     (c) => c.mode !== 'kiosk' && c.hasList && (c.mode === 'desktop' || !c.itemOpen),
  'app.main':     () => true,
  'app.dock':     (c) => c.mode !== 'kiosk' && c.inSessions,
  'app.drawer':   (c) => c.mode !== 'kiosk' && c.drawer !== null,
  'app.overlays': () => true,
}
```

- [ ] **Step 1: Write `visibilityMatrix.test.tsx` first**

Table-driven, with the spec's table as the literal fixture — every mode × every region, plus the conditional cells:

```ts
const EXPECTED: Array<[LayoutMode, RegionId, Partial<RegionContext>, boolean]> = [
  ['phone',   'app.rail',   {}, true],
  ['kiosk',   'app.rail',   {}, false],
  ['phone',   'app.list',   { hasList: true,  itemOpen: false }, true],
  ['phone',   'app.list',   { hasList: true,  itemOpen: true  }, false],
  ['desktop', 'app.list',   { hasList: true,  itemOpen: true  }, true],
  ['desktop', 'app.list',   { hasList: false, itemOpen: false }, false],
  ['tablet',  'app.dock',   { inSessions: true  }, true],
  ['tablet',  'app.dock',   { inSessions: false }, false],
  ['kiosk',   'app.dock',   { inSessions: true  }, false],
  ['desktop', 'app.drawer', { drawer: 'help' }, true],
  ['desktop', 'app.drawer', { drawer: null   }, false],
  ['kiosk',   'app.drawer', { drawer: 'help' }, false],
  // …fill in every remaining mode × region cell from the table above…
]
```

Assert `REGION_VISIBILITY[region]({ mode, ...defaults, ...overrides }) === expected`. Every one of the 28 cells must appear. A test that covers only the interesting cells is how the boring ones break.

- [ ] **Step 2: Write `AppShell.test.tsx`**

This is the region-singleton guard from umbrella §7:

```tsx
it.each(['phone', 'tablet', 'desktop', 'kiosk'] as const)(
  'in %s mode every region mounts at most once', (mode) => {
    renderShellAt(mode, '/sessions')
    const counts = new Map<string, number>()
    for (const el of document.querySelectorAll('[data-region]')) {
      const id = el.getAttribute('data-region')!
      counts.set(id, (counts.get(id) ?? 0) + 1)
    }
    // app.rail is legitimately two nodes on phone (TopBar + NavBar) — see Task 8.
    const allowed = (id: string) => (mode === 'phone' && id === 'app.rail' ? 2 : 1)
    for (const [id, n] of counts) expect(n, `${id} in ${mode}`).toBeLessThanOrEqual(allowed(id))
  })
```

Plus: the regions present in each mode match `REGION_VISIBILITY`; `AppShell` renders no `data-region` node that is not a `RegionId`; and axe is clean per mode (with the roles-and-names-only comment).

To drive the mode, stub `matchMedia` exactly as `useViewport.test.tsx` does — import that helper rather than copying it, extracting it to a shared test util if needed.

- [ ] **Step 3: Run both to verify they fail.**

- [ ] **Step 4: Implement `AppShell`**

```tsx
export function AppShell() {
  const [kiosk] = React.useState(readKioskConfig)   // read once at mount
  return (
    <ViewportProvider kiosk={kiosk}>
      <InteractionProvider interactive={!kiosk.enabled}>
        <AppShellInner />
      </InteractionProvider>
    </ViewportProvider>
  )
}
```

`AppShellInner` calls `useViewport()`, builds the `RegionContext` from the location, and renders a CSS-grid layout per mode following sub-spec B §4.1:

```
desktop:  [banners]
          [rail][list?][main][drawer?]     grid-template-columns: auto auto 1fr auto
          [dock?]
tablet:   [banners] / [rail][list | main] / [dock?]
phone:    [banners][topbar] / [list | main] / [dock?] / [navbar]
kiosk:    [banners(danger)] / [main]
```

Rules that are easy to get wrong:
- `PromptDock` is passed **into** `PageHost` as its `dock` prop (Task 11), so it lands inside the session runtime provider. It is not a sibling.
- The drawer is a grid **column** when `width === 'wide'` or (`mode === 'desktop'` and ≥1280); otherwise `DrawerHost` renders its own overlay `Sheet`. Since `useViewport` has no 1280 tier, add `atPush = useMediaQuery('(min-width: 1280px)')` **inside `useViewport`** and expose it as `Viewport.drawerPushes` — do not read `matchMedia` in `AppShell`, which would violate the global constraint and the Task 3 guard. This means editing `useViewport.tsx` and its test; do it here and say so in your report.
- When navigating between sections, preserve `?drawer=` (sub-spec B §4.2). Implement by intercepting nav in `NavItem` (Task 7) — it already builds the `to`; append the current `drawer` param there.
- Use only layer tokens (`z-nav`, `z-drawer`) — no literals.

- [ ] **Step 5: Run both tests green. Gates. Commit.**

```
git add web/src/shell/AppShell.tsx web/src/shell/AppShell.test.tsx web/src/shell/visibilityMatrix.test.tsx web/src/shell/useViewport.tsx web/src/shell/useViewport.test.tsx
git commit -m "feat(shell): AppShell — one shell, regions placed by a per-mode visibility matrix"
```

---

### Task 17: the cutover — delete both shells

**Files:**
- Modify: `web/src/App.tsx` (becomes ~15 lines), `web/src/main.tsx`
- Delete: `web/src/components/Dashboard.tsx`, `web/src/components/DesktopShell.tsx`, `web/src/components/tabDefs.ts`
- Move: `NewSessionDialog` and `RenameDialog` out of `Dashboard.tsx` → `web/src/features/sessions/`

**This is the risky task.** Everything before it was additive; the app still ran on the old shells. This commit is where they die. Tasks 1–16 must all be green before starting.

**Order matters.** Do the moves *before* the deletes, and run the suite between each step, so a failure names one cause.

- [ ] **Step 1: Move the two dialogs out of `Dashboard.tsx`**

`NewSessionDialog` (`Dashboard.tsx:81–327`, non-exported, with `NewSessionDialogProps` at L72–79) and `RenameDialog` (L339–376, exported, props at L331–337) both survive the shell. Move each to its own file under `web/src/features/sessions/`, unchanged apart from imports. Update every importer of `RenameDialog` (`grep -rn "RenameDialog" web/src`).

Run the suite. Expect green — this is a pure move.

Commit: `refactor(sessions): move NewSessionDialog and RenameDialog out of Dashboard`

- [ ] **Step 2: Rewrite `App.tsx`**

It collapses to the router plus the providers that wrap it:

```tsx
import { RouterProvider, createBrowserRouter } from 'react-router-dom'
import { DeployControllerProvider } from './lib/deployController'
import { ROUTES } from './shell/routes'

const router = createBrowserRouter(ROUTES)

export default function App() {
  return (
    <DeployControllerProvider>
      <RouterProvider router={router} />
    </DeployControllerProvider>
  )
}
```

`main.tsx` currently mounts `<BrowserRouter>` (L21) around `<App/>`. `createBrowserRouter` + `RouterProvider` **replaces** it — having both is an error. Remove `BrowserRouter` from `main.tsx` and keep `Toaster` there (Task 14's decision), which means `Toaster` is now outside the router. Check whether anything in the toast path uses router hooks; if it does, keep `BrowserRouter` out and move `Toaster` inside `Overlays` after all — and say which you did and why.

The banner stack that was at `App.tsx:190–194` is now `BannerStackRegion` inside `AppShell`. `DeployOverlay` at L218 moves into `Overlays`. `LibraryRedirect`, `DashboardRoute`, `SessionRoute`, `ReviewRoute` and `SubSessionRoute` are all deleted — their behaviour lives in `routes.tsx` and the region components.

**`SessionRoute`'s archive-advance effects (`App.tsx:93–158`) are not dead code.** They implement: `nextSessionAfterClose` navigation on done, and the 503/409 fallback that navigates to `/review/:id` when a session was never live. Port both into the Sessions page (or a `useArchiveAdvance` hook next to it) — do **not** drop them. `App.test.tsx` has three tests for exactly this behaviour (L280, L301, L327) which Task 19 must keep green. Read those three tests first and let them define the contract.

Run the suite. **Expect many failures** — that is Task 18's job. Confirm the failures are confined to the 11 files named in Task 18 plus the e2e spec; if anything else fails, stop and report.

- [ ] **Step 3: Delete the shells**

```
git rm web/src/components/Dashboard.tsx web/src/components/DesktopShell.tsx web/src/components/tabDefs.ts
```

Then verify nothing still imports them:

```
grep -rn "Dashboard\|DesktopShell\|tabDefs\|buildTabs\|TAB_DEFS\|TabId" web/src --include=*.ts --include=*.tsx
```

Every remaining hit must be in a test file that Task 18 rewrites, or must be fixed now. `TabId` in particular was imported by `App.tsx:3` and `DesktopShell.tsx:46`; it is replaced by `NavId` from `shell/navDefs.ts`.

Also delete `web/src/components/TabBar.tsx` **if** nothing but the deleted shells used it — check first (`grep -rn "TabBar" web/src`). If `SettingsView` or another page uses it, leave it.

- [ ] **Step 4: Confirm the success criterion**

```
ls web/src/components/Dashboard.tsx web/src/components/DesktopShell.tsx web/src/shell/AppShell.tsx
```

Expected: the first two do not exist; the third does. This is umbrella §9's literal wording: *"Exactly one of `Dashboard.tsx` / `DesktopShell.tsx` / `AppShell.tsx` exists: `AppShell.tsx`."*

- [ ] **Step 5: Commit (tests still red — that is expected and stated in the message)**

```
git add -A web/src
git commit -m "feat(shell)!: single AppShell replaces Dashboard and DesktopShell

Tests for the deleted shells are migrated in the next commit."
```

> **Controller note:** Task 17 is the one task in this plan that ends with a red suite, by construction — the shells and their tests cannot be deleted in the same breath without an unreviewable mega-diff. **Review Tasks 17 and 18 as a single unit** and do not accept 17 on its own. If a fresh implementer picks up Task 18, hand it Task 17's report as context.

---

### Task 18: migrate the 103 orphaned tests

**Files:**
- Delete: `web/src/components/__tests__/DesktopShell.test.tsx`, `DesktopAdvanceOnArchive.test.tsx`, `DesktopListScroll.test.tsx`, `DesktopSessionActions.test.tsx`, `DashboardTabs.test.tsx`, `DashboardSettings.test.tsx`, `DashboardAllLimited.test.tsx`
- Rewrite: `web/src/App.test.tsx`, `web/src/App.dialogs.test.tsx`, `web/src/components/__tests__/dedupeMobileDesktop.test.ts`, `web/src/e2e/dashboard.spec.ts`
- Touch as needed: `SessionView.test.tsx` and its siblings, `a11y.test.tsx`, `ArchivedTab.test.tsx`, `KeyboardSessions.test.tsx`, `NewSessionBranch.test.tsx`, `SessionActions.test.tsx`

**The inventory.** 103 `it`s across 11 files, plus a wider blast radius. **Every one of these is a behaviour someone deliberately pinned. Deleting a test file is only allowed when its behaviour is re-pinned elsewhere — name where, for each.**

| File | `it`s | Disposition |
|---|---:|---|
| `App.test.tsx` | 18 | **Rewrite in place.** Router assertions still apply — but paths change (`/session/:id` → `/sessions/:id`). The "renders the DesktopShell side-by-side layout at lg and up" case (L235–250) becomes an `AppShell` desktop-mode case. The 3 archive-advance cases (L280, 301, 327) must survive verbatim in behaviour — they pin the code Task 17 Step 2 ported. |
| `App.dialogs.test.tsx` | 4 | **Rewrite.** `?dialog=new` now works in every mode (DEC-5) and is written with `replace` (DEC-10) — the "closing clears the param" case must now also assert no history entry was added. |
| `DesktopShell.test.tsx` | 22 | **Delete, re-pin.** Map each: master-detail selection → `AppShell.test.tsx` + `routes.test.tsx`; the 5 `detachKeyYieldsToTextEntry` cases and `Tab`-then-`a` → **PR 4** (shortcuts registry) — *these cannot be re-pinned here.* See the warning below. |
| `DesktopAdvanceOnArchive.test.tsx` | 5 | **Move** to the Sessions page's test file; the behaviour is unchanged, only the host component differs. |
| `DesktopListScroll.test.tsx` | 2 | **Re-pin in `ContextList.test.tsx`** (Task 10 already writes the identity assertion). The workaround is gone; the behaviour must still hold. |
| `DesktopSessionActions.test.tsx` | 1 | **Move** with `MainPaneSession` → the Sessions page. |
| `DashboardTabs.test.tsx` | 13 | **Delete, re-pin in `NavRail.test.tsx` / `PhoneNav.test.tsx`.** Roving focus and `aria-controls` were `TabBar` behaviours; the rail is a `nav` of links, so `aria-controls`/`tabpanel` assertions do **not** carry over — say so explicitly rather than silently dropping them. The needs-reauth badge cases carry over to `useNavBadges`. |
| `DashboardSettings.test.tsx` | 6 | **Move** to the moved `NewSessionDialog`'s own test file (Task 17 Step 1). |
| `DashboardAllLimited.test.tsx` | 3 | **Delete** — exact twins of three `DesktopShell.test.tsx` cases; re-pin once in `useAppBanners.test.tsx` and the Sessions page test. |
| `dedupeMobileDesktop.test.ts` | 7 | **Rewrite (DEC-12).** Its greps target the two deleted filenames. Retarget at `AppShell.tsx` + the region files, keeping the intent: no page re-inlines `AccountsManager`/`ArchivedManager`/the transcript reducer. |
| `SessionView.test.tsx` | 22 | **Keep, adjust.** Its subject still exists. It will need the `SessionRuntimeProvider` wrapper now that Task 11 Step 5 made `SessionView` consume the context. |

> **Warning — do not silently drop the detach-key tests.** `DesktopShell.test.tsx` contains 5 cases pinning `detachKeyYieldsToTextEntry` (composer caret, question-card draft) plus ArrowLeft/Ctrl+Z/Escape detach semantics. Those handlers live in `DesktopShell.tsx:229–267` and die with it. **The shortcuts registry that replaces them is PR 4, not this PR.** You have two honest options: (a) port the handlers into the Sessions page unchanged for this PR and keep the 5 tests green there, or (b) delete handlers and tests together and record it as a known gap PR 4 closes. **Option (a) is strongly preferred** — a regression that lands in PR 3 and is fixed in PR 4 is still a regression on `master` in between. Whichever you choose, state it in your report; do not let these 5 tests quietly vanish.

- [ ] **Step 1: Write the disposition map first**

Before changing any test, produce a table listing **all 103 `it` names** and, for each, where its behaviour lands: kept / moved to `<file>` / re-pinned by `<test>` / dropped with reason. Write it to `.superpowers/sdd/<plan>/task-18-disposition.md`. This is the artifact the reviewer checks against; without it, "we migrated the tests" is unverifiable.

Get the names with:
```
grep -n "  it(" web/src/App.test.tsx web/src/App.dialogs.test.tsx web/src/components/__tests__/DesktopShell.test.tsx web/src/components/__tests__/DesktopAdvanceOnArchive.test.tsx web/src/components/__tests__/DesktopListScroll.test.tsx web/src/components/__tests__/DesktopSessionActions.test.tsx web/src/components/__tests__/DashboardTabs.test.tsx web/src/components/__tests__/DashboardSettings.test.tsx web/src/components/__tests__/DashboardAllLimited.test.tsx web/src/components/session/__tests__/SessionView.test.tsx
```

- [ ] **Step 2: Work the map top to bottom, running the suite after each file.**

- [ ] **Step 3: Update the Playwright spec**

`web/e2e/dashboard.spec.ts` pins `viewport: { width: 375, height: 812 }` at L25 **specifically to force the mobile fork**, and its header comment (L11–19) documents desktop-vs-mobile assertion differences that no longer exist. Rewrite the comment to say the viewport now selects a *layout mode*, not a *code path*, and re-check every assertion in the file against phone mode under `AppShell`. Do not add the four viewport projects — that is PR 10.

- [ ] **Step 4: The suite must be fully green.**

```
npm --prefix web test
npm --prefix web run typecheck
npm --prefix web run lint
npm --prefix web run build
```

Report the before/after test counts. A drop in total `it` count is expected (duplicated mobile/desktop twins collapse); **an unexplained drop is a finding**. Account for the delta explicitly: `103 → N, of which X were true duplicates, Y moved, Z dropped with reason`.

- [ ] **Step 5: Commit**

```
git add -A web/src web/e2e
git commit -m "test(shell): migrate the 103 tests pinned to the deleted shells"
```

---

### Task 19: kiosk at shell level

**Files:**
- Modify: `web/src/shell/AppShell.tsx`, `web/src/shell/regions/BannerStackRegion.tsx`, `web/src/ui/Dialog.tsx`
- Create: `web/src/shell/kiosk.integration.test.tsx`

**Contract (sub-spec B §5, PR 3 half only).** When `mode === 'kiosk'`:
- `app.rail`, `app.list`, `app.dock`, `app.drawer` are hidden (already true via `REGION_VISIBILITY` from Task 16 — this task verifies it end-to-end);
- `InteractionContext.interactive === false` (already wired in Task 16);
- `--scale: 1.25` is set on `<html>`;
- `page.header` actions are hidden;
- `Dialog` is refused (opening one is a no-op that logs);
- banners are filtered to `tone === 'danger'`.

**Not in this PR:** `useAttractCycle`, wake-lock, cursor hiding, Playwright viewport projects. Those are PR 10. Do not build them.

- [ ] **Step 1: `--scale` on `<html>`**

`AppShell` sets `document.documentElement.style.setProperty('--scale', '1.25')` when kiosk is enabled, and removes it on unmount. The type scale already multiplies by `--scale` (PR 1's token layer) so nothing else is needed. Test: with `?kiosk=1`, `document.documentElement.style.getPropertyValue('--scale')` is `'1.25'`; without it, `''`.

- [ ] **Step 2: Filter banners by tone**

In `BannerStackRegion`, when `mode === 'kiosk'`, pass only items with `tone === 'danger'`. Task 6 gave every item a tone. Test: in kiosk, the `daemon` and `all-limited` banners can render but `update`, `deploy`, `input-hold` and `git-credential` cannot.

Note the region itself stays mounted in kiosk (the matrix says `D`, not `0`) — it renders nothing when no danger banner is active, which `[&:empty]:hidden` already handles.

- [ ] **Step 3: Refuse dialogs in kiosk**

In `ui/Dialog.tsx`, read `useInteractive()`; when false, render nothing and `console.warn` once with the dialog's title. Add a test asserting a `Dialog` with `open` inside `<InteractionProvider interactive={false}>` renders no dialog node.

Keep this in `Dialog` rather than in `AppShell`: a dialog opened from anywhere must be refused, and only the primitive sees them all.

- [ ] **Step 4: Hide `page.header` actions**

In `ui/PageHeader.tsx`, do not render `actions` when `!useInteractive()`. Test both ways.

- [ ] **Step 5: Write `kiosk.integration.test.tsx`** — render the shell at `?kiosk=1` and assert, in one place: no `app.rail`/`app.list`/`app.dock`/`app.drawer`; `app.main` present; `--scale` set; a `danger` banner renders and an `info` one does not; a `Dialog` does not open; `StatusStrip` tiles are not buttons (the Task 5 wiring, verified end-to-end).

- [ ] **Step 6: Gates and commit**

```
git add web/src/shell web/src/ui/Dialog.tsx web/src/ui/PageHeader.tsx
git commit -m "feat(shell): kiosk is a shell-level read-only layout mode"
```

---

### Task 20: acceptance

No new behaviour. Verify the PR's claims and produce the evidence for the PR body.

- [ ] **Step 1: The umbrella §9 success criteria that apply to PR 3**

```
ls web/src/components/Dashboard.tsx web/src/components/DesktopShell.tsx web/src/shell/AppShell.tsx
grep -rn "tabDefs\|buildTabs\|TAB_DEFS" web/src
grep -rln "data-region" web/src --include=*.tsx
grep -rn "matchMedia" web/src --include=*.ts --include=*.tsx | grep -v test
```

Expected: only `AppShell.tsx` of the three exists; no `tabDefs`/`buildTabs`/`TAB_DEFS` hits outside deleted history; `data-region` appears only in region components and `ui/BannerStack.tsx`; the `matchMedia` hits are exactly the DEC-8 exemption list plus `useViewport` and `lib/useMediaQuery`.

- [ ] **Step 2: Every route renders in every mode**

Extend `routes.test.tsx` with a matrix: each of the 11 non-redirect paths × `phone`/`tablet`/`desktop`/`kiosk`, asserting the page renders and no error boundary trips. That is 44 cases and it is the PR's central claim (umbrella §9: *"Every route in §6 renders the same page at 390, 820, 1440 and `?kiosk=1`"*). The Playwright half of that criterion is PR 10.

- [ ] **Step 3: All gates, including the Python ones**

```
npm --prefix web test
npm --prefix web run typecheck
npm --prefix web run lint
npm --prefix web run build
ruff check src/ server/
mypy server/ --ignore-missing-imports
```

The Python gates should be untouched by this PR; run them to prove it.

- [ ] **Step 4: Report the evidence** — region counts per mode, the test-count delta from Task 18's disposition map, and the four greps above, as pasteable blocks for the PR body.

---

## Self-review

**Spec coverage.** Sub-spec B §1 (PR 3 scope): `AppShell` T16 · regions T6–T14 · `useViewport` T3 · `navDefs` T1 · route table T15 · `BannerStack` T6 · `DrawerHost` (help) T13 · kiosk at shell level T4/T5/T19 · hosts existing pages T15 · deletes `Dashboard`/`DesktopShell`/`tabDefs`/the `App.tsx` fork T17. §3 `useViewport` → T3. §4.1 layout per mode → T16. §4.2 region contracts → T6–T14. §4.3 `navDefs` → T1. §4.4 routing → T15. §5 kiosk PR 3 half → T19. §8 tests: `AppShell.test` T16 · `visibilityMatrix.test` T16 · `routes.test` T15/T20 · `useViewport.test` T3 · `kiosk.test` T4/T19 · deleted shell tests T18. Umbrella §7 guards: region singleton T16 · visibility matrix T16 · a11y per region T7–T14.

**Deliberately out of scope** (each belongs to a later PR, named): shortcuts registry, `SessionHeader`, composer row, list filters (PR 4) · `useAttractCycle`, wake-lock, cursor hiding, Playwright viewport projects (PR 10) · `AssistantSheet` (PR 12) · page-variant contract and the palette sweep (sub-spec C) · `session.panes`, Views menu, diff chip, CI bar (PR 4).

**Known gap carried by this plan:** the 5 `detachKeyYieldsToTextEntry` tests (Task 18's warning). The plan's preferred resolution is to port the handlers unchanged so nothing regresses on `master` between PR 3 and PR 4.

**Type consistency.** `NavId` (T1) is used by `kiosk.ts` (T4), `useNavBadges` (T6), `NavRail` (T7), `NavBar` (T8), `routes` (T15). `NavBadgeKey` (T1) keys `useNavBadges`'s return (T6) and `NavDef.badge` (T1) — pinned by a test in T1. `BannerStackItem.tone` (T6) is consumed by `BannerStackRegion` (T19). `ViewportKiosk` (T3) is structurally satisfied by `KioskConfig` (T4) — **T4 must keep `enabled`, `cycleSeconds` and `pages`**; T3's test constructs that shape literally. `RegionContext`/`RegionId` (T16) are used only by T16's two tests. `SessionListColumnProps` (T9) is consumed by `ContextList` (T10) — T9 Step 5 requires the implementer to publish the exact signature.

**Sequencing check.** T3 does not import T4 (it takes kiosk as a prop) — deliberate, so the two can land in either order. T15 needs `AppShell` from T16, which needs the regions from T6–T14: T15 is written against a placeholder and T16 wires the real one. Every task before T17 leaves the app running on the old shells.
