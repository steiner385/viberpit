# Pipeline onto the chassis as the Workspace variant (finish-line row 5) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put the Pipeline on the page contract as the first **Workspace**-variant page (`PageHeader` with the health band as `meta` and the bell/legend as `actions`, a repo `Tabs` strip as `page.subnav`, `PageContent scroll={false}` with the board owning its scroll), move the chart CSS into an allowlisted `features/pipeline/charts/plotroom.css`, convert every other plotroom rule to token utilities, delete the `.prdash-root` remap, and finish kiosk (attract cycle across pages, wake lock, idle cursor).

**Architecture:** The charts (metro track, check gantt, waterfall, queue train) keep their CSS because their rules are geometry, not palette; they move under `features/pipeline/charts/` with their components, un-nested from `.prdash-root` and scoped instead by a `pipeline-chart` root class each chart carries. Everything else in `plotroom.css` is either dead (≈550 lines: metrics, critical path, runner routing, kiosk type scale, spine) and is deleted, or is chrome/board styling and is rewritten as Tailwind token utilities on the components (`StatusStrip`, `HealthHeader`, `PipelineView`, `PrRow`, `LegendPanel`). The page itself is new (`features/pipeline/PipelinePage.tsx`); `PipelineTab` and `PipelineSwitcher` are deleted; `useKioskCycle` becomes the page-local `useRepoCycle` and reads kiosk from `useViewport()` instead of re-parsing the URL. Kiosk completion lands in the shell (`shell/useAttractCycle.ts`, `shell/useWakeLock.ts`, `shell/useIdleCursor.ts`).

**Tech Stack:** React 19 + react-router + Tailwind v4 tokens (`web/`), vitest + Testing Library, Playwright. No `server/` changes.

**Spec:** `docs/superpowers/specs/2026-09-08-finish-line-persona-modes-design.md` §5 row 5; `docs/superpowers/specs/2026-09-02-ui-page-migration-design.md` §2 (page contract; §2.2 Workspace rule; §2.3 invariants incl. lucide icons only), §3 PR 10, §7 risk; `docs/superpowers/specs/2026-09-02-ui-shell-workspace-design.md` §5 (kiosk: PR 10 completion = attract cycle, wake lock, idle cursor); `docs/superpowers/specs/2026-09-02-ui-target-state-design.md` §9 (success criteria, chart allowlist), row 10; `docs/superpowers/specs/2026-07-12-plotroom-pipeline-replica-design.md` §kiosk (repo auto-cycle paused when hidden; read-only tiles; no row expansion) and the `PipelineSwitcher` contract (sticky focus: `?pipeline=` → localStorage → `repos[0]`). Issue #1469.

## Global Constraints

- Page contract (page-migration §2): `<div className="flex flex-col h-full min-h-0" data-page-variant="workspace">` → `PageHeader` (the only `h1`; `document.title` "Pipeline — ViberPit" via `PageHeader`) → `page.subnav` `Tabs` → `PageContent scroll={false}` (`overflow-hidden`; inner panes own scroll). Errors/stale as `Banner` first child of `PageContent`. Phone `px-4`, desktop `px-6` come from the primitives. Lucide icons only: the bell is `Bell`/`BellOff`, the legend is `HelpCircle`.
- Kiosk (shell §5, replica §kiosk): tiles read-only, rows do not expand (already via `InteractionContext`); `PageHeader` hides `actions` itself; the repo subnav is not rendered in kiosk; the focused repo auto-cycles every `cycleSeconds`, paused while `document.hidden`; the page reads kiosk from `useViewport()` — never `readKioskConfig` again (the shell read it once).
- Token classes only under `web/src/features/`, `web/src/shell/`, `web/src/ui/` (`ccswitch/no-raw-palette`), with the one allowlist `web/src/features/pipeline/charts/**` (already in `web/eslint.config.js`). `web/src/pipeline/**` is outside the rule today; every component this plan touches there is rewritten onto tokens anyway and asserted by the success greps: `grep -rn "dark:" web/src/pipeline web/src/features/pipeline` → 0; `grep -rnE "(bg|text|border)-(gray|blue|red|amber|green|violet|purple)-" web/src/pipeline web/src/features/pipeline` → 0; no `#hex`/`rgb(` in `charts/plotroom.css` (tokens `--t-*` only, no `var(--x, #fallback)`).
- Single sources of truth: lane status → tone in `pipeline/spine/laneStatus.ts` (`LANE_TONE`, beside `LANE_GLYPH`/`LANE_WORD`); bucket → tone in `StatusStrip.tsx` (`TILE_TONE`, replacing `cssClass` in `TILE_DEFINITIONS`) and reused by `LegendPanel`; the chart CSS is the only CSS file left under `features/pipeline/` and `web/src/pipeline/plotroom.css` is gone at the end (Task 4).
- Deleted, each with its test in the same task: `web/src/pipeline/PipelineTab.tsx` (+ test; `laneJumpTarget` moves), `web/src/pipeline/PipelineSwitcher.tsx` (+ test; `useFocusedPipeline` moves), `web/src/pipeline/useKioskCycle.ts` (+ test; becomes `useRepoCycle`), `web/src/pipeline/plotroom.css`, `web/src/pipeline/__tests__/plotroomTokens.test.ts` (replaced), the `.prdash-root` wrappers in `features/roadmap/InitiativePage.tsx` and `features/roadmap/detail/ProductDetail.tsx`.
- Every routed page change adds/updates its row in `web/src/features/settings/pageContract.test.tsx` (new row `{ nav: 'pipeline', path: '/pipeline', variant: 'workspace', title: 'Pipeline' }`; kiosk cell included since pipeline is kiosk-safe; the mock must stub `EventSource` the way `PipelineTab.test.tsx` does).
- Web gate on every task: `(cd web && npm run typecheck && npm run lint && npm run test && npm run build)` then `rm -rf web/dist`. Full vitest every time (79 files mock `@/api/hooks`). No server change; `ruff check src/ server/` once at the end for form.
- Bash hook: `(cd web && …)` subshells; commit messages via `git commit -F <file>`; never pair deploy/release/rollout/promote with prod/production in command text.
- Commit trailer: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. Branch `feat/finish-line-row-5-pipeline-workspace` off `origin/master` (created). One PR: `Fixes #1469`, `Refs #1520`; ready + `gh pr merge --squash --auto` when green. Never push, open the PR or rebase from a task.

## Rulings (controller, pre-execution)

- **R1 — Chart scoping.** `charts/plotroom.css` is not nested under `.prdash-root` (deleted) but under one class, `.pipeline-chart`, that every chart ROOT element carries (`<div className="pipeline-chart track">`, `<div className="pipeline-chart checks">`, `<svg className="pipeline-chart chart-svg">`, `<div className="pipeline-chart queue-train">`); root rules are written `&.track {…}`, descendants stay `.node {…}`. `LegendPanel`'s live example markup wraps each example in `<div className="pipeline-chart">`. `initiatives/InitiativeTrack.tsx` gets `pipeline-chart track` on its root and imports the charts CSS. Cost if wrong: one class name.
- **R2 — `QueueTrain` is a chart.** Its cars, progress fill and ops strip are geometry; it moves to `charts/` with its CSS. `StatusStrip` is not a chart (six tiles) and converts to tokens.
- **R3 — Chart CSS uses `--t-*` tokens directly**, no legacy aliases and no hex fallbacks (`var(--done, #2da44e)` → `var(--t-success)`); `index.css`'s legacy aliases stay for the settings sections until row 7. Cost: a token name.
- **R4 — Repo switcher becomes `ui/Tabs`** in `page.subnav` (spec PR 10), one tab per repo labelled `owner/name`, value = the focused repo, `?pipeline=` stays the URL state through `useFocusedPipeline` (moved to `features/pipeline/useFocusedPipeline.ts` verbatim with its tests). The filter box and `filterRepos` are dropped: `Tabs` scrolls horizontally and the fleet tracks ~12 repos. Cost if wrong: re-adding a filter input above the strip.
- **R5 — Stale feed is a `Banner tone="warning" role="status"`** as the first child of `PageContent`; before the first frame the page shows `Skeleton` tiles + rows (not a bare "Loading pipeline…" paragraph) with a `role="status"` "Loading pipeline…" for the existing smoke assertion.
- **R6 — Kiosk completion is shell-level** (`useAttractCycle`, `useWakeLock`, `useIdleCursor` in `web/src/shell/`, mounted from `AppShellInner` when `mode === 'kiosk'`), not page-level; the page keeps only the per-repo `useRepoCycle`. Attract cycle runs only when `kiosk.pages.length > 1` (one page = nothing to rotate; today's default is `['pipeline']`).
- **R7 — Playwright viewport projects (target-state row 10) are deferred to row 7's sweep.** `web/e2e/pipeline.spec.ts` sets its own viewports (390 / 820 / 1440) and `?kiosk=1`, the way `delivery.spec.ts` does. Cost: none now; one config change later.
- **R8 — `web/src/pipeline/` stays where it is** except the four charts, the page, the focus hook and the lane-jump helper, which live under `features/pipeline/`. Moving the other ~50 files is row 7's repo-wide sweep. Cost: two import roots for one feature until row 7.

## File map

| File | Responsibility |
|------|----------------|
| `web/src/features/pipeline/charts/plotroom.css` | The chart rules only (metro, gantt, waterfall, queue train), `.pipeline-chart`-scoped, tokens only. |
| `web/src/features/pipeline/charts/{MetroTrack,CheckGantt,Waterfall,QueueTrain}.tsx` (+ tests, `git mv`) | Chart components; roots carry `pipeline-chart`. |
| `web/src/features/pipeline/charts/plotroom.test.ts` | Replaces `plotroomTokens.test.ts`: no `.prdash-root`, no dark block, no hex/rgb, only CSS file under `features/pipeline`. |
| `web/src/initiatives/InitiativeTrack.tsx`, `features/roadmap/InitiativePage.tsx`, `features/roadmap/detail/ProductDetail.tsx` | Track imports the charts CSS; `.prdash-root` wrappers deleted. |
| `web/src/pipeline/StatusStrip.tsx` (+ test) | `TILE_TONE`; tiles on token utilities. |
| `web/src/pipeline/spine/laneStatus.ts` (+ test), `web/src/pipeline/HealthHeader.tsx` (+ test) | `LANE_TONE`; band on token utilities, sized for `PageHeader.meta`. |
| `web/src/pipeline/PipelineView.tsx` (+ tests) | Repo headers/sections on tokens; owns `overflow-y-auto`. |
| `web/src/pipeline/PrRow.tsx` (+ tests), `web/src/pipeline/LegendPanel.tsx` (+ test) | Rows and legend on tokens; legend examples in `.pipeline-chart` wrappers. |
| `web/src/features/pipeline/laneJump.ts` (+ test) | `laneJumpTarget` (moved). |
| `web/src/features/pipeline/useFocusedPipeline.ts` (+ test) | Sticky repo focus (moved from `PipelineSwitcher.tsx`). |
| `web/src/features/pipeline/useRepoCycle.ts` (+ test) | `useKioskCycle` renamed, page-local. |
| `web/src/features/pipeline/PipelinePage.tsx` (+ test) | The Workspace page. |
| `web/src/shell/routes.tsx`, `routes.test.tsx`, `features/settings/pageContract.test.tsx` | Route points at the page; contract row. |
| `web/src/shell/useAttractCycle.ts`, `useWakeLock.ts`, `useIdleCursor.ts` (+ tests), `AppShell.tsx`, `index.css` | Kiosk completion. |
| `web/e2e/pipeline.spec.ts` | Smoke at three viewports + kiosk. |
| `docs/superpowers/specs/2026-09-08-finish-line-persona-modes-design.md`, `docs/product/features/*` naming `PipelineTab`/`plotroom.css` | Docs. |
| deleted | `pipeline/PipelineTab.tsx` (+ test), `pipeline/PipelineSwitcher.tsx` (+ test), `pipeline/useKioskCycle.ts` (+ test), `pipeline/plotroom.css`, `pipeline/__tests__/plotroomTokens.test.ts`. |

---

### Task 1: The charts folder — components, scoped chart CSS, and the end of `.prdash-root`

**Files:**
- Move (`git mv`): `web/src/pipeline/{MetroTrack,CheckGantt,Waterfall,QueueTrain}.tsx` → `web/src/features/pipeline/charts/`; their tests `web/src/pipeline/__tests__/{MetroTrack,CheckGantt,Waterfall,QueueTrain}.test.tsx` → `web/src/features/pipeline/charts/__tests__/` (fix imports).
- Create: `web/src/features/pipeline/charts/plotroom.css`, `web/src/features/pipeline/charts/plotroom.test.ts`
- Modify: `web/src/pipeline/plotroom.css` (remove the moved rules and the `.prdash-root` remap block lines 9–25; keep the rest nested under a plain `.pipeline-legacy` wrapper for Tasks 2–4 — see Step 3), `web/src/pipeline/PipelineTab.tsx` (wrapper class `prdash-root` → `pipeline-legacy`; imports both CSS files), `web/src/pipeline/PrRow.tsx`, `web/src/pipeline/LegendPanel.tsx`, `web/src/pipeline/PipelineView.tsx` (import paths; legend examples wrapped), `web/src/initiatives/InitiativeTrack.tsx`, `web/src/features/roadmap/InitiativePage.tsx:216-223`, `web/src/features/roadmap/detail/ProductDetail.tsx:112-114`
- Delete: `web/src/pipeline/__tests__/plotroomTokens.test.ts`

**Interfaces:**
- Produces: `charts/plotroom.css` = `.pipeline-chart { &.track {…} .node {…} .seg {…} &.checks {…} .g-row {…} … &.chart-svg {…} &.queue-train {…} .car {…} .queue-ops {…} }` + the two `@keyframes prdash-*` the charts use + the `@media (max-width: 560px)` track rules + the reduced-motion pulse rule — every colour a `var(--t-*)` token.
- Produces: chart roots — `MetroTrack` root `className="pipeline-chart track"`, `CheckGantt` root `"pipeline-chart checks"`, `Waterfall` `<svg className="pipeline-chart chart-svg">`, `QueueTrain` root `"pipeline-chart queue-train"` (read each component for the current root element and add the class; nothing else changes).
- Produces: `InitiativeTrack` root `className="pipeline-chart track"`, `import '@/features/pipeline/charts/plotroom.css'`.
- Consumes (unchanged): every class name the four chart tests and `LegendPanel.test.tsx` assert (`.node`, `.g-row`, `.g-bar i`, `.band`, `.car`, `.car-pct i`, …).

- [ ] **Step 1: Failing test** `charts/plotroom.test.ts`:

```ts
// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'

const here = join(__dirname)
const css = readFileSync(join(here, 'plotroom.css'), 'utf8')

function cssFilesUnder(dir: string): string[] {
  return readdirSync(dir).flatMap((n) => {
    const p = join(dir, n)
    return statSync(p).isDirectory() ? cssFilesUnder(p) : n.endsWith('.css') ? [p] : []
  })
}

describe('charts/plotroom.css — the one allowlisted stylesheet', () => {
  it('is scoped by .pipeline-chart, never by the deleted .prdash-root', () => {
    expect(css).toMatch(/^\.pipeline-chart\s*\{/m)
    expect(css).not.toMatch(/prdash-root/)
  })
  it('has no dark override and no colour literals — tokens only', () => {
    expect(css).not.toMatch(/prefers-color-scheme/)
    expect(css).not.toMatch(/#[0-9a-fA-F]{3,8}\b/)
    expect(css).not.toMatch(/rgba?\(/)
    expect(css).not.toMatch(/var\(--(bg|surface|text|muted|border|accent|done|fail|amber|purple)\b/)
  })
  it('is the only css file under features/pipeline', () => {
    expect(cssFilesUnder(join(here, '..'))).toEqual([join(here, 'plotroom.css')])
  })
})
```

- [ ] **Step 2: Run to fail** — `(cd web && npx vitest run src/features/pipeline)` → the file does not exist.

- [ ] **Step 3: Split the CSS.** Read `web/src/pipeline/plotroom.css` once. Into `charts/plotroom.css` copy, converting colours to `--t-*` tokens: the `@keyframes prdash-*` blocks the charts use (lines 2–7, keep only those referenced by moved rules), the metro track rules (89–112) and their mobile variant (866–872), the gantt rules (128–219), `.chart-svg` (509–513), the queue-train rules (248–309). Re-nest them as `.pipeline-chart { … }` with `&.track`, `&.checks`, `&.chart-svg`, `&.queue-train` for the roots. Token map for this file: `var(--done…)`→`var(--t-success)`, `var(--fail…)`→`var(--t-danger)`, `var(--amber…)`→`var(--t-warning)`, `var(--purple…)`→`var(--t-info)`, `var(--accent…)`→`var(--t-accent)`, `var(--surface…)`→`var(--t-surface)`, `var(--bg…)`→`var(--t-canvas)`, `var(--border…)`→`var(--t-edge)`, `var(--text…)`→`var(--t-ink)`, `var(--muted…)`→`var(--t-ink-muted)`, `#fff` on a filled node → `var(--t-canvas)`, `rgba(0,0,0,.18)` shadows → `var(--t-edge)`; any `--z-*` → the matching `--layer-*`. Then in `web/src/pipeline/plotroom.css`: delete the moved rules and the lines 9–25 remap block; rename the line-27 wrapper `.prdash-root {` → `.pipeline-legacy {` (the remaining rules keep resolving through `index.css`'s legacy aliases until Tasks 2–4 delete them); `PipelineTab.tsx:106` wrapper class → `pipeline-legacy`. Delete `plotroomTokens.test.ts`.

- [ ] **Step 4: Move the charts** (`git mv`), add the root class to each, fix imports (`grep -rn "from '\.\./MetroTrack'\|from './MetroTrack'\|from '@/pipeline/\(MetroTrack\|CheckGantt\|Waterfall\|QueueTrain\)'" web/src` → the new path `@/features/pipeline/charts/<Name>`; tests moved under `charts/__tests__/` import `../<Name>`). `LegendPanel.tsx`: wrap each live example (`.node`, `.g-row`, `.car`) in `<div className="pipeline-chart">…</div>`; its test asserts class presence, not ancestry, so it stays green. `InitiativeTrack.tsx`: root class + CSS import; delete the `prdash-root` wrapper `<div>`s in `InitiativePage.tsx` (keep the inner flex/gap classes on the same element) and `ProductDetail.tsx` (keep `rounded-md border border-edge bg-surface p-3`), and delete the two comments that explained them.

- [ ] **Step 5: Full web gate; commit**

```bash
printf 'refactor(pipeline): charts move to features/pipeline/charts with their own token-only stylesheet; .prdash-root remap deleted (Refs #1469)\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/p1.txt
git add -A web/src && git commit -F /tmp/p1.txt
```

---

### Task 2: Status strip, health band and repo sections onto tokens

**Files:**
- Modify: `web/src/pipeline/StatusStrip.tsx` (+ `__tests__/StatusStrip.test.tsx`), `web/src/pipeline/spine/laneStatus.ts` (+ `__tests__/laneStatus.test.ts`), `web/src/pipeline/HealthHeader.tsx` (+ `__tests__/HealthHeader.test.tsx`), `web/src/pipeline/PipelineView.tsx` (+ `__tests__/PipelineView.test.tsx`), `web/src/pipeline/plotroom.css` (delete the converted rules: 57–71 repo header, 220–247 status strip, 824–851 health header, `.spine-glyph`/`.s-*` 770–774/804, the dead spine 755–823, the dead kiosk 721–742, the dead metrics/critical-path/needs-graph/cost/lint/runner blocks 433–685 and 874–1125, the dead layout 29–56, the dead tab bar 410–432 — keep ONLY what Tasks 3–4 still convert: PR rows 72–127, settings-body/section + legend 310–322/686–720, `.pr-session*`/`.pr-fix` 1127–1132, switcher 1134–1162, chrome bar/stale 1164–1174, and the shared focus-ring rule if any live class uses it)

**Interfaces:**
- Produces: `LANE_TONE: Record<LaneStatus, Tone>` = `{ green: 'success', amber: 'warning', red: 'danger', blind: 'neutral', idle: 'neutral' }` in `spine/laneStatus.ts` (`Tone` from `@/ui/Badge`).
- Produces: `TILE_TONE: Record<Bucket, Tone>` = `{ running: 'info', queued: 'accent', deploy: 'success', failed: 'danger', ready: 'warning', merged: 'neutral' }`; `TILE_DEFINITIONS` entries lose `cssClass` and gain `tone: TILE_TONE[bucket]`. Tiles: `<button|div className={cn('flex min-h-target flex-col rounded-md border border-edge px-3 py-2 text-left', TONE_WASH[tone], active && 'ring-2 ring-accent')}>` — import the tone class maps from `@/ui/Badge` if exported, else add `export const TONE_WASH`/`TONE_TEXT` exports there (one map, already the Badge's).
- Produces: `HealthHeader` markup unchanged in roles/test ids (`role="group"`, `data-testid="health-rollup"`, `health-lane-<id>`, `aria-label`s, `not-wired` → `opacity-60`), classes on tokens: the rollup a `Button variant="ghost" size="sm"` with a `Badge dot tone={LANE_TONE[roll.state]}`; lanes a `<ul className="flex flex-wrap gap-1">` of `Button variant="ghost" size="sm"` chips with `Badge dot` + title + (non-green) summary `text-xs text-ink-muted`. The `spine-glyph` glyphs (`LANE_GLYPH`) stay as `aria-hidden` text inside the chips. Height ≤ two lines so it fits `PageHeader.meta`.
- Produces: `PipelineView` root `className="flex h-full min-h-0 flex-col overflow-y-auto px-4 py-3 md:px-6"` (it owns the scroll in the Workspace variant); repo `<section>` headers on `border-b border-edge`, chevron `IconButton`/`Button variant="ghost"`; `.repo-summary*` → `text-xs text-ink-muted`.

- [ ] **Step 1: Failing tests** — `laneStatus.test.ts`: `LANE_TONE` maps the five statuses as above and every `LaneStatus` has an entry. `StatusStrip.test.tsx`: every `TILE_DEFINITIONS` row has a `tone`; the active tile carries `ring-accent`; kiosk tiles are still `div`s (existing). `HealthHeader.test.tsx`: existing assertions unchanged + `not-wired` lane has `opacity-60` (replace any `.not-wired` class assertion). `PipelineView.test.tsx`: root has `overflow-y-auto`.
- [ ] **Step 2: Run to fail.** **Step 3: Implement** the four components on tokens; delete the CSS blocks listed above. `grep -n "tile-\|health-\|repo-header\|repo-summary\|spine-glyph\|s-green" web/src/pipeline/*.tsx web/src/pipeline/**/*.tsx` → only `LegendPanel.tsx` swatches remain (Task 4 handles them; until then they render unstyled, which its tests do not assert).
- [ ] **Step 4: Full web gate; commit**

```bash
printf 'refactor(pipeline): status strip, health band and repo sections on token utilities; ~600 dead plotroom lines deleted (Refs #1469)\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/p2.txt
git add -A web/src && git commit -F /tmp/p2.txt
```

---

### Task 3: `PipelinePage` — the Workspace variant, repo tabs, header actions, repo cycle

**Files:**
- Create: `web/src/features/pipeline/laneJump.ts` (+ `laneJump.test.ts`), `web/src/features/pipeline/useFocusedPipeline.ts` (+ `useFocusedPipeline.test.tsx`, moved cases from `PipelineSwitcher.test.tsx` minus the listbox ones), `web/src/features/pipeline/useRepoCycle.ts` (+ `useRepoCycle.test.ts`, moved from `useKioskCycle.test.ts`), `web/src/features/pipeline/PipelinePage.tsx` (+ `PipelinePage.test.tsx`)
- Modify: `web/src/shell/routes.tsx` (`PipelinePage` import; drop the local wrapper), `web/src/shell/routes.test.tsx` (mock path `@/features/pipeline/PipelinePage`), `web/src/features/settings/pageContract.test.tsx` (row + `EventSource` stub), `web/src/pipeline/plotroom.css` (delete switcher 1134–1162 and chrome bar/stale 1164–1174)
- Delete: `web/src/pipeline/PipelineTab.tsx`, `__tests__/PipelineTab.test.tsx`, `web/src/pipeline/PipelineSwitcher.tsx`, `__tests__/PipelineSwitcher.test.tsx`, `web/src/pipeline/useKioskCycle.ts`, `__tests__/useKioskCycle.test.ts`

**Interfaces:**
- `laneJumpTarget(laneId, status)` — verbatim from `PipelineTab.tsx:25-36`.
- `useFocusedPipeline(repos, enabled = true, useUrl = true): [string | null, (repo: string) => void]` — verbatim from `PipelineSwitcher.tsx:27-61` (sticky focus contract: `?pipeline=` → `localStorage['workspace.focusedPipeline']` → `repos[0]`; late adoption).
- `useRepoCycle(enabled: boolean, cycleSeconds: number, repos: readonly string[], focused: string | null, focus: (r: string) => void)` — `useKioskCycle` renamed; body unchanged (interval, `document.hidden` pause, no-op for one repo).
- `PipelinePage()`:

```tsx
import { useCallback, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Bell, BellOff, HelpCircle } from 'lucide-react'
import { ApiBaseProvider } from '@/pipeline/ApiBaseContext'
import { useDashboard } from '@/pipeline/useDashboard'
import { useFixPr } from '@/api/hooks'
import { HealthHeader } from '@/pipeline/HealthHeader'
import { LegendPanel } from '@/pipeline/LegendPanel'
import { PipelineView } from '@/pipeline/PipelineView'
import type { Bucket } from '@/pipeline/StatusStrip'
import type { LaneStatus } from '@/pipeline/types'
import { Banner } from '@/ui/Banner'
import { IconButton } from '@/ui/IconButton'
import { PageContent } from '@/ui/PageContent'
import { PageHeader } from '@/ui/PageHeader'
import { Skeleton } from '@/ui/Skeleton'
import { Tabs } from '@/ui/Tabs'
import { useViewport } from '@/shell/useViewport'
import { deliveryPath } from '@/features/delivery/deliveryNavState'
import '@/features/pipeline/charts/plotroom.css'
import { laneJumpTarget } from './laneJump'
import { useFocusedPipeline } from './useFocusedPipeline'
import { useRepoCycle } from './useRepoCycle'

/** `/pipeline` — the first Workspace-variant page (page-migration §2.2): the
 *  health band is the header's meta, the bell and legend its actions, the repo
 *  strip is `page.subnav`, and the board owns its own scroll. */
export function PipelinePage() {
  return (
    <ApiBaseProvider base="/api/pipeline">
      <Inner />
    </ApiBaseProvider>
  )
}

function Inner() {
  const navigate = useNavigate()
  const { mode, kiosk } = useViewport()            // kiosk read ONCE by the shell; never re-parse the URL here
  const isKiosk = mode === 'kiosk'
  const fixPr = useFixPr()
  const onFixPr = useCallback((id: string, number: number) => fixPr.mutate({ id, number }),
    [fixPr.mutate]) // eslint-disable-line react-hooks/exhaustive-deps -- .mutate is the stable handle (see PrRow.areEqual)
  const { state, stale, notifySupported, notifyEnabled, toggleNotify } = useDashboard()
  const repos = useMemo(() => (state?.repos ?? []).map((r) => r.repo), [state])
  const [focused, focus] = useFocusedPipeline(repos, true, !isKiosk)
  useRepoCycle(isKiosk, kiosk.cycleSeconds, repos, focused, focus)
  const [filter, setFilter] = useState<Bucket | null>(null)
  const [legendOpen, setLegendOpen] = useState(false)
  const legendBtnRef = useRef<HTMLButtonElement>(null)
  const onJumpToLane = useCallback((laneId: string | null, status: LaneStatus | null) => {
    const t = laneJumpTarget(laneId, status)
    if (t.kind === 'filter') setFilter(t.bucket)
    else navigate(deliveryPath('ci'))
  }, [navigate])

  return (
    <div className="flex h-full min-h-0 flex-col" data-page-variant="workspace">
      <PageHeader
        title="Pipeline"
        meta={state ? <HealthHeader state={state} onJumpToLane={onJumpToLane} /> : undefined}
        actions={(
          <>
            {notifySupported && (
              <IconButton aria-label="Browser notifications (this tab)" aria-pressed={notifyEnabled} onClick={toggleNotify}>
                {notifyEnabled ? <Bell size={18} /> : <BellOff size={18} />}
              </IconButton>
            )}
            <IconButton ref={legendBtnRef} aria-label="Legend" aria-haspopup="dialog" aria-expanded={legendOpen} onClick={() => setLegendOpen(true)}>
              <HelpCircle size={18} />
            </IconButton>
          </>
        )}
      />
      {!isKiosk && repos.length > 0 && (
        <Tabs
          label="Pipeline repository"
          items={repos.map((r) => ({ id: r, label: r }))}
          value={focused ?? repos[0]}
          onChange={focus}
          className="shrink-0 border-b border-edge px-4 md:px-6"
        />
      )}
      <PageContent scroll={false} busy={!state}>
        {stale && <Banner tone="warning" role="status" title="Pipeline feed stalled — showing the last frame." className="m-4 mb-0" />}
        {!state
          ? (
            <div role="status" className="space-y-3 p-4 md:p-6" aria-label="Loading pipeline…">
              <span className="sr-only">Loading pipeline…</span>
              <div className="grid grid-cols-3 gap-2 md:grid-cols-6"><Skeleton /><Skeleton /><Skeleton /><Skeleton /><Skeleton /><Skeleton /></div>
              <Skeleton lines={6} />
            </div>
          )
          : (
            <PipelineView
              state={state}
              focused={focused}
              filter={filter}
              onFilter={setFilter}
              onFixPr={onFixPr}
            />
          )}
      </PageContent>
      <LegendPanel open={legendOpen} onClose={() => setLegendOpen(false)} returnFocusRef={legendBtnRef} />
    </div>
  )
}
```

Read `PipelineTab.tsx:39-101` for the exact `PipelineView` props it passes today and mirror them (the block above names the ones the inventory listed; `PipelineView` may take more — pass exactly what `Inner` passes now). Read `useViewport.tsx:32-45` for the real shape of `kiosk` on the viewport (`ViewportKiosk`) and use its field names. `IconButton` is `forwardRef`, so `legendBtnRef` works for `LegendPanel`'s `returnFocusRef`.
- `routes.tsx`: `import { PipelinePage } from '@/features/pipeline/PipelinePage'`; delete the local `PipelinePage` wrapper and the `PipelineTab` import; row `{ path: NAV_BY_ID.pipeline.path, element: <PipelinePage /> }` unchanged.

- [ ] **Step 1: Failing tests** — `laneJump.test.ts` = the six `laneJumpTarget` cases from `PipelineTab.test.tsx`. `useFocusedPipeline.test.tsx` = the sticky-focus/`?pipeline=`/`useUrl=false`/late-adoption cases from `PipelineSwitcher.test.tsx` (drop `filterRepos` and the listbox keyboard cases). `useRepoCycle.test.ts` = `useKioskCycle.test.ts` with the rename. `PipelinePage.test.tsx` (stub `EventSource` as `PipelineTab.test.tsx` does; mock `@/api/hooks` `useFixPr`; wrap in `MemoryRouter` + `QueryClientProvider` + `ViewportProvider kiosk={…}` — read `shell/useViewport.tsx` for the provider's props):
  - before the first frame: `role="status"` "Loading pipeline…", `[data-page-variant="workspace"]`, h1 "Pipeline", `document.title` "Pipeline — ViberPit".
  - with a fake `EventSource` that delivers one frame with two repos (`o/a`, `o/b`): a `tablist` named "Pipeline repository" with two tabs, `o/a` selected; clicking `o/b` sets `?pipeline=o%2Fb` in the URL (`useLocation` probe).
  - the bell: `aria-pressed` false → click → `toggleNotify` path (assert `Notification.requestPermission` was called via a stubbed `Notification` with `permission: 'default'`), icon swaps.
  - the legend: click "Legend" → `role="dialog"` open; close returns focus to the Legend button.
  - kiosk (`ViewportProvider kiosk={{ enabled: true, cycleSeconds: 10, pages: ['pipeline'] }}` + `InteractionProvider interactive={false}`): no `tablist`, no header actions, and with fake timers the focused repo advances after 10 s (`?pipeline=` is NOT written — `useUrl` false — assert through the rendered focused section instead).
  - stale: with the frame delivered then 90 s of fake time, a `role="status"` banner "Pipeline feed stalled" appears as the first child of the content region.
  - `pageContract.test.tsx`: add the row; in its `beforeEach`/module scope `vi.stubGlobal('EventSource', …)` as `PipelineTab.test.tsx` does (no frame ever arrives → the page renders its loading state, which satisfies the contract: one h1, title, variant, no horizontal overflow).
  - `routes.test.tsx`: change the mock to `vi.mock('@/features/pipeline/PipelinePage', () => ({ PipelinePage: stub('pipeline') }))`.
- [ ] **Step 2: Run to fail.** **Step 3: Implement** (create the four modules; delete the three old files + tests; delete the switcher and chrome-bar CSS). `grep -rn "PipelineTab\|PipelineSwitcher\|useKioskCycle\|readKioskConfig" web/src` → only `shell/*` (`readKioskConfig` in AppShell/kiosk.ts) and provenance comments.
- [ ] **Step 4: Full web gate; commit**

```bash
printf 'feat(pipeline): PipelinePage — the Workspace variant on the page contract; repo Tabs subnav; bell/legend as header actions; page-local useRepoCycle (Refs #1469)\n\nDeletes PipelineTab, PipelineSwitcher and useKioskCycle.\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/p3.txt
git add -A web/src && git commit -F /tmp/p3.txt
```

---

### Task 4: PR rows and the legend onto tokens; `plotroom.css` is gone

**Files:**
- Modify: `web/src/pipeline/PrRow.tsx` (+ `__tests__/PrRow.test.tsx`, `PrRow.memo.test.tsx`), `web/src/pipeline/LegendPanel.tsx` (+ `__tests__/LegendPanel.test.tsx`), `web/src/pipeline/PipelineView.tsx` (`.pr-session*`/`.pr-fix` → tokens), `web/src/features/pipeline/charts/plotroom.test.ts` (add: `web/src/pipeline/plotroom.css` does not exist)
- Delete: `web/src/pipeline/plotroom.css`

**Interfaces:**
- `PrRow`: keep `id="pr-{n}"`, every `data-testid`/`aria-*`, the `.eta-stale` marker (keep the class name as a marker AND add `text-ink-muted italic`; the test asserts the class), the memo `areEqual` contract. Classes: row `flex flex-col gap-1 border-b border-edge px-2 py-2`, head `flex items-center gap-2`, num `text-xs tabular-nums text-ink-muted`, title `truncate text-sm font-medium text-ink`, sub-line `text-xs text-ink-muted`, ready-merge `Button size="sm" variant="secondary"`, stuck-enqueue badge `Badge tone="warning"`, `.pr-fix` (was `#d29922`) → `Badge tone="warning"`, `.pr-session` link `text-accent hover:underline`.
- `LegendPanel`: `.settings-body/.settings-section/.legend-*` → `space-y-4 p-4`, `<section className="space-y-2">` with `h3 text-sm font-semibold text-ink`, rows `flex items-start gap-3 text-sm`, captions `text-xs text-ink-muted`; tile swatches read `TILE_TONE` + the Badge wash map (one source with `StatusStrip`); the live chart examples stay inside `.pipeline-chart` wrappers (Task 1).
- `charts/plotroom.test.ts` gains: `expect(existsSync(join(here, '../../../pipeline/plotroom.css'))).toBe(false)`.

- [ ] **Step 1: Failing test** (the `existsSync` assertion + any LegendPanel/PrRow class assertion you change). **Step 2: Run to fail.** **Step 3: Implement**; delete `web/src/pipeline/plotroom.css`; `grep -rn "plotroom.css" web/src` → only `features/pipeline/charts/plotroom.css` importers (`PipelinePage.tsx`, `InitiativeTrack.tsx`). Success greps from Global Constraints → 0.
- [ ] **Step 4: Full web gate; commit**

```bash
printf 'refactor(pipeline): PR rows and legend on token utilities; plotroom.css deleted — charts keep the one allowlisted stylesheet (Refs #1469)\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/p4.txt
git add -A web/src && git commit -F /tmp/p4.txt
```

---

### Task 5: Kiosk completion — attract cycle, wake lock, idle cursor

**Files:**
- Create: `web/src/shell/useAttractCycle.ts` (+ `useAttractCycle.test.tsx`), `web/src/shell/useWakeLock.ts` (+ `useWakeLock.test.ts`), `web/src/shell/useIdleCursor.ts` (+ `useIdleCursor.test.ts`)
- Modify: `web/src/shell/AppShell.tsx` (`AppShellInner`: when `mode === 'kiosk'` mount the three), `web/src/index.css` (`html[data-idle] { cursor: none; }` beside the `--scale` rule), `web/src/shell/kiosk.integration.test.tsx` (three new cases)

**Interfaces:**
- `useAttractCycle(enabled: boolean, pages: readonly NavId[], cycleSeconds: number)` — when `enabled && pages.length > 1`, every `cycleSeconds` navigate (`replace`, keeping `location.search`) to the next page's `NAV_BY_ID[id].path` (wrapping); paused while `document.hidden` (skip the tick); starts from whichever page the current pathname belongs to (`navDefForPath`).
- `useWakeLock(enabled: boolean)` — on mount (and on `visibilitychange` back to visible) call `navigator.wakeLock?.request('screen')`; release on unmount; swallow rejections (no user activation, unsupported) — a wall display without wake lock still works.
- `useIdleCursor(enabled: boolean, idleMs = 5000)` — sets `data-idle=""` on `document.documentElement` after `idleMs` without `mousemove`/`keydown`/`pointerdown`; clears on any of them; removes the attribute on unmount.

- [ ] **Step 1: Failing tests** (fake timers; `MemoryRouter` + a location probe for the cycle; a stubbed `navigator.wakeLock` `{ request: vi.fn().mockResolvedValue({ release: vi.fn() }) }`; `document.documentElement.hasAttribute('data-idle')`):
  - cycle: pages `['pipeline','delivery']`... note `delivery` is NOT kiosk-safe — use two kiosk-safe ids; today only `pipeline` is, so test with `pages=['pipeline']` (no navigation ever) and with a stubbed `NAV_DEFS` of two kiosk-safe entries (`vi.mock('@/shell/navDefs', …)`) that it advances `pipeline → x → pipeline` at 10 s, 20 s, and skips a tick while `document.hidden` (define `hidden` via `Object.defineProperty`).
  - wake lock: request on mount when enabled; not when disabled; re-request after `visibilitychange` to visible; `release` on unmount; a rejected request does not throw.
  - idle cursor: attribute appears after 5 s, disappears on `mousemove`, never appears when disabled.
  - `kiosk.integration.test.tsx`: in kiosk the `<html>` gets `data-idle` after 5 s of fake time and `navigator.wakeLock.request` was called; in desktop neither.
- [ ] **Step 2: Run to fail.** **Step 3: Implement**; mount from `AppShellInner`: `useAttractCycle(mode === 'kiosk', kiosk.pages, kiosk.cycleSeconds)`, `useWakeLock(mode === 'kiosk')`, `useIdleCursor(mode === 'kiosk')` (read the viewport's kiosk fields).
- [ ] **Step 4: Full web gate; commit**

```bash
printf 'feat(shell): kiosk completion — attract cycle across kiosk pages, screen wake lock, idle cursor (Refs #1469)\n\nShell spec §5 "PR 10 completion".\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/p5.txt
git add -A web/src && git commit -F /tmp/p5.txt
```

---

### Task 6: Close out — e2e, docs, gates

**Files:**
- Create: `web/e2e/pipeline.spec.ts`
- Modify: `docs/superpowers/specs/2026-09-08-finish-line-persona-modes-design.md` (§5 row 5 status "— landed <date> (Closes #1469)"), `docs/superpowers/specs/2026-09-02-ui-page-migration-design.md` (PR 10 row: mark landed; §7 risk line: note the outcome), any `docs/product/features/*.md` naming `PipelineTab.tsx`, `PipelineSwitcher`, `plotroom.css`, `useKioskCycle` (`grep -rln` and repoint), `web/README.md` if it names them.

- [ ] **Step 1: e2e** — same harness as `delivery.spec.ts`/`roadmap.spec.ts` (intercept `**/api/**`; for `/api/pipeline/events` return a `text/event-stream` body with one `data: {…}` frame built from a two-repo `DashboardState` fixture inlined in the spec; `/api/pipeline/snapshot` the same JSON): (a) desktop 1440: h1 "Pipeline", the tablist with two repo tabs, clicking the second rewrites `?pipeline=`, the Legend button opens a dialog and Escape returns focus; (b) phone 390×844 and tablet 820×1180: same page renders, no horizontal scroll (`document.documentElement.scrollWidth <= clientWidth`); (c) `/pipeline?kiosk=1`: no tablist, no header actions, tiles are not buttons, after `cycle` seconds (use `?cycle=10` and `page.clock`) the focused repo section changes.
- [ ] **Step 2: Docs.** **Step 3: Gates** — full web gate; `npx playwright test e2e/pipeline.spec.ts` (rebuild `dist` first if the gate removed it; remove it after); success greps; `ruff check src/ server/`.
- [ ] **Step 4: Commit**

```bash
printf 'test(pipeline): e2e smoke at three viewports and kiosk; docs for finish-line row 5 (Refs #1469)\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/p6.txt
git add -A web docs && git commit -F /tmp/p6.txt
```

Controller then: whole-branch review, one fix wave, push, PR (`Fixes #1469`, `Refs #1520`, rulings R1–R8), `gh pr merge --squash --auto`.

## Self-review notes

- Spec coverage: page contract Workspace (T3), subnav Tabs (T3, R4), health band in meta (T2+T3), bell/legend actions with lucide (T3), chart CSS allowlist + charts folder (T1, R1–R3), other rules → tokens (T2, T4), `.prdash-root` remap deleted (T1), `pipeline/kiosk.ts` already deleted earlier (noted), `useKioskCycle` → `useRepoCycle` (T3), page-contract row 390/820/1440 + kiosk (T3), shell §5 PR 10 completion (T5, R6), Playwright (T6, R7 defers projects), success greps (Global Constraints, T4).
- Type consistency: `LANE_TONE`, `TILE_TONE`, `useFocusedPipeline`, `useRepoCycle`, `laneJumpTarget`, `pipeline-chart` are spelled identically across tasks.
- Known plan-level risks: the CSS split (T1) and the row/legend conversion (T4) are the largest diffs; each stays behind class-name-preserving tests (`.node`, `.g-row`, `.car`, `.eta-stale`). `PipelineView`'s prop list is read from `PipelineTab` at implementation time rather than restated here.
