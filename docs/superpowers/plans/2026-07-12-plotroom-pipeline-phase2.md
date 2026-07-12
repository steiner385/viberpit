# Plotroom Pipeline Replica — Phase 2 (Chrome + Hardening) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add plotroom's chrome to the ccswitch pipeline (HealthHeader lane chips, LegendPanel, PipelineSwitcher with URL/localStorage stickiness, kiosk mode, repo-collapse persistence, notification bell backed by a minimal server emitter) and close Phase-1's carry-forward findings (per-PR queue substates, ready-merge response nuances, SSE hardening, facade-coverage guard, stale-banner styling).

**Architecture:** Frontend chrome is ported verbatim from the pinned plotroom snapshot into `web/src/pipeline/` and composed inside `PipelineTab` (the host glue). The ported `PipelineView` gains three declared blend edits (controlled filter, collapse persistence, kiosk prop). The backend gains a frame-diffing notifier that emits named SSE `notification` events the already-ported `useDashboard` hook consumes, plus small `dashboard_state`/`ready_merge` refinements.

**Tech Stack:** React 19 + vitest (web/), FastAPI + pytest (server/), plotroom sources at commit `5166b52`.

**Spec:** `docs/superpowers/specs/2026-07-12-plotroom-pipeline-replica-design.md` (§5 chrome, §8 phase 2)

## Global Constraints

- **Pinned snapshot** (create if missing): `git -C /mnt/ssk-ssd/tony/GitHub/.ccswitch/clones/steiner385/plotroom worktree add /tmp/plotroom-5166b52 5166b52 --detach`. Shorthand: `$PLOT = /tmp/plotroom-5166b52/frontend/src`.
- **Port discipline** (see memory `plotroom-port-conventions`): first-line header `// Ported from plotroom@5166b52 frontend/src/<path>; edits: <list>`; byte-identical modulo declared edits; ported tests pass with ZERO assertion edits; `react-refresh/only-export-components` → file-level disable; `react-hooks/refs` render-phase ref writes → `useLayoutEffect`; dead upstream locals → DELETE (declared) — eslint suppression does NOT silence `tsc -b` (broke PR #562).
- **Branch:** `feat/plotroom-pipeline-phase2` off `origin/master` (NEVER local `master`); verify `git branch --show-current` before EVERY commit.
- **Web gates (all, before every commit):** in `web/`: focused `npx vitest run <paths>` while iterating; before commit `npx tsc --noEmit && npx eslint src && npm run build` (the `tsc -b` build is mandatory — see memory `web-build-gate-tsc-b`).
- **Server gates:** `ruff check server/ src/`; `mypy server/ --ignore-missing-imports`; tests via `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest <paths> -q`. Full-suite runs additionally need `--ignore=server/tests/daemon_properties --ignore=server/tests/test_daemon_properties.py --deselect server/tests/test_eta_parity_live.py -k "not copilot_proxy_resolve"`; `test_skill_crud_roundtrip` fails locally on clean master (pre-existing, ignore).
- **Commits** end with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- **Finish:** non-draft PR + `gh pr merge --squash --auto`.

---

### Task 1: Restore lane types + port the spine lane derivations

**Files:**
- Modify: `web/src/pipeline/types.ts` (restore `LaneView` + `Lane` verbatim from `$PLOT/types.ts` — the "Delivery Spine" section at its end; `LaneStatus` already present)
- Create: `web/src/pipeline/spine/laneStatus.ts`, `web/src/pipeline/spine/laneHealth.ts`, `web/src/pipeline/spine/lanes/{prCiLane,mergeQueueLane,mainLane,deployLane,scheduledLane,failuresLane,costLane}.ts`
- Test: port every existing spine test — check `ls $PLOT/__tests__ | grep -iE "lane"` (at minimum `laneLayout.test.ts` if it covers these; port whichever test files import `spine/laneHealth`, `spine/laneStatus`, or `spine/lanes/` — find them with `grep -rln "laneHealth\|laneStatus\|lanes/" $PLOT/__tests__`)

**Interfaces:**
- Consumes: `DashboardState`, `LaneStatus` from `./types` (Phase 1).
- Produces: `buildLaneHealth(state: DashboardState | null): LaneHealth[]` (`LaneHealth = Omit<Lane,'renderExpanded'>`); `rollup(lanes: LaneView[]): {state: RollupState; count: number; firstAttentionId: string | null}`; `attentionPhrase(count: number): string`; `LANE_GLYPH`/`LANE_WORD: Record<LaneStatus,string>`; lane ids `'pr-ci'|'merge-queue'|'main'|'deploy'|'scheduled'|'failures'|'cost'`. Consumed by Task 2 (HealthHeader) and Task 5 (jump routing).

- [ ] **Step 1: Branch**
```bash
cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/ccswitch/fcbfe508
git fetch origin && git checkout -b feat/plotroom-pipeline-phase2 origin/master
```
- [ ] **Step 2: Copy + repoint** — `mkdir -p web/src/pipeline/spine/lanes web/src/pipeline/hooks; cp $PLOT/spine/laneStatus.ts $PLOT/spine/laneHealth.ts web/src/pipeline/spine/; cp $PLOT/spine/lanes/*.ts web/src/pipeline/spine/lanes/`. Repoints: `../types` → `../types` stays correct relative to `spine/` (verify: from `web/src/pipeline/spine/` the types file is at `../types`); lanes use `../../types` (already correct). Restore `LaneView`/`Lane` in types.ts verbatim (Lane's `renderExpanded: () => import('react').ReactNode` line included).
- [ ] **Step 3: Port the tests found by the grep; run them** — `cd web && npx vitest run src/pipeline/__tests__/<ported>` → green, zero assertion edits.
- [ ] **Step 4: Gates + commit** — `npx tsc --noEmit && npx eslint src/pipeline && npm run build`; commit `feat(web/pipeline): spine lane derivations + lane types (ported)`.

---

### Task 2: HealthHeader (ported)

**Files:**
- Create: `web/src/pipeline/HealthHeader.tsx` (from `$PLOT/HealthHeader.tsx`, 64 lines)
- Test: `web/src/pipeline/__tests__/HealthHeader.test.tsx` (from `$PLOT/__tests__/HealthHeader.test.tsx`)

**Interfaces:**
- Consumes: Task 1's `buildLaneHealth`, `rollup`, `attentionPhrase`, `LANE_GLYPH`, `LANE_WORD`; `DashboardState`/`LaneStatus` types.
- Produces: `HealthHeader({state: DashboardState; onJumpToLane: (laneId: string | null, status: LaneStatus | null) => void})`. Consumed by Task 5.

- [ ] **Step 1: Copy + repoint** (`./types` stays; `./spine/laneHealth`, `./spine/laneStatus` stay — same relative layout).
- [ ] **Step 2: Ported test green with zero assertion edits** — `npx vitest run src/pipeline/__tests__/HealthHeader.test.tsx`.
- [ ] **Step 3: CSS check** — `grep -c "health-header\|health-lane\|health-rollup\|spine-glyph" web/src/pipeline/plotroom.css`. If 0 hits: the styles live in `$PLOT/styles.css`? They were ported wholesale in Phase 1 — if genuinely absent they're in `$PLOT/shell/workspace.css`; copy ONLY the `health-header`/`health-lane*`/`health-rollup`/`spine-glyph` rule blocks into `plotroom.css` inside `.prdash-root`, in a marked `/* --- ported from workspace.css: HealthHeader --- */` section (declared).
- [ ] **Step 4: Gates + commit** — `feat(web/pipeline): HealthHeader lane chips (ported)`.

---

### Task 3: LegendPanel + useFocusTrap (ported)

**Files:**
- Create: `web/src/pipeline/hooks/useFocusTrap.ts` (from `$PLOT/hooks/useFocusTrap.ts`, 91 lines), `web/src/pipeline/LegendPanel.tsx` (from `$PLOT/LegendPanel.tsx`, 318 lines)
- Test: ported `useFocusTrap.test.tsx` + `LegendPanel.test.tsx`

**Interfaces:**
- Consumes: `TILE_DEFINITIONS` from `./StatusStrip` (Phase 1); `METRIC_DEFINITIONS, SUBLINE_TERMS, LANE_STATE_DEFINITIONS, LANE_DEFINITIONS, SETTINGS_DEFINITIONS, DESIGNER_DEFINITIONS, METRIC_SECTION_GROUPS` from `./definitions` (Phase 1 ported definitions.ts verbatim, so these exist).
- Produces: `LegendPanel({open, onClose, returnFocusRef}: LegendPanelProps)`; `useFocusTrap(...)`. Consumed by Task 5.

- [ ] **Step 1: Copy + repoint** (LegendPanel: `./StatusStrip`, `./definitions`, `./hooks/useFocusTrap` — all same relative paths).
- [ ] **Step 2: Ported tests green, zero assertion edits.**
- [ ] **Step 3: CSS check** — same drill as Task 2 for `legend-panel`/`legend-*` selectors.
- [ ] **Step 4: Gates + commit** — `feat(web/pipeline): LegendPanel + focus trap (ported)`.

---

### Task 4: PipelineSwitcher + kiosk config (ported)

**Files:**
- Create: `web/src/pipeline/PipelineSwitcher.tsx` (from `$PLOT/shell/PipelineSwitcher.tsx`, 147 lines), `web/src/pipeline/kiosk.ts` (from `$PLOT/kiosk.ts`, 24 lines)
- Test: ported `PipelineSwitcher.test.tsx` + `kiosk.test.ts`

**Interfaces:**
- Consumes: nothing from earlier tasks (self-contained; uses localStorage + URL).
- Produces: `useFocusedPipeline(repos: readonly string[], enabled = true, useUrl = true): [string | null, (repo: string) => void]`; `filterRepos(repos, query, focused): string[]`; `PipelineSwitcher({repos, focused, onFocus})`; `readKioskConfig(search?): {kiosk: boolean; cycleSeconds: number}` (`?kiosk=1&cycle=N`, min 10s, default 30s). Consumed by Task 5.

- [ ] **Step 1: Copy + repoint** (check `$PLOT/shell/PipelineSwitcher.tsx` imports first — it may import shell-local helpers; port what it needs or escalate if it drags the whole shell).
- [ ] **Step 2: Ported tests green, zero assertion edits.** The switcher manipulates the URL directly (`?pipeline=`); tests run in jsdom — keep whatever the plotroom tests stub.
- [ ] **Step 3: CSS check** for `pipeline-switcher`/`switcher-*` selectors (same drill).
- [ ] **Step 4: Gates + commit** — `feat(web/pipeline): PipelineSwitcher + kiosk config (ported)`.

---

### Task 5: PipelineView blend edits + PipelineTab chrome composition

**Files:**
- Modify: `web/src/pipeline/PipelineView.tsx` (three declared blend edits), `web/src/pipeline/PipelineTab.tsx` (chrome composition), `web/src/pipeline/plotroom.css` (ccswitch-extensions: `.pipeline-stale` styling — carry-forward (g))
- Test: `web/src/pipeline/__tests__/PipelineView.blend.test.tsx` (new), `web/src/pipeline/__tests__/PipelineTab.test.tsx` (extend)

**Interfaces:**
- Consumes: Tasks 1–4 exports; `useDashboard` (already returns `notifySupported`, `notifyEnabled`, `toggleNotify` — read the ported hook to confirm exact names before wiring).
- Produces: `PipelineView` gains optional `activeFilter?: Bucket | null`, `onFilter?: (b: Bucket | null) => void` (controlled mode; internal state when omitted), `kiosk?: boolean` (default false → `StatusStrip interactive={!kiosk}`, `PrRow expandable={!kiosk}` — exactly the legacy `$PLOT/App.tsx:348,403` pattern), and collapse persistence.

- [ ] **Step 1: Failing blend tests** (write first):
```tsx
// web/src/pipeline/__tests__/PipelineView.blend.test.tsx
import { render, screen, fireEvent } from '@testing-library/react'
import { describe, expect, it, beforeEach } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { PipelineView } from '../PipelineView'
import type { DashboardState } from '../types'

const state: DashboardState = {
  generatedAt: 'now', staleSince: null,
  repos: [{ repo: 'o/r', hasDeploy: false, queue: null, prs: [{
    repo: 'o/r', number: 1, title: 't', url: 'https://x/1',
    stage: { stage: 'ci', substate: null, percent: null, etaSeconds: null, etaRangeSeconds: null, overdue: false },
    queueAheadCount: null, checks: [], groupChecks: null, mergeEtaSim: null,
  }] }],
}
const wrap = (ui: React.ReactNode) => render(<MemoryRouter>{ui}</MemoryRouter>)

beforeEach(() => localStorage.clear())

describe('PipelineView blend edits', () => {
  it('kiosk renders tiles as non-interactive divs and rows non-expandable', () => {
    wrap(<PipelineView state={state} focusedRepo={null} kiosk />)
    // StatusStrip kiosk mode: tiles are not buttons (plotroom StatusStrip interactive=false)
    expect(screen.queryByRole('button', { name: /CI running/i })).toBeNull()
  })
  it('persists repo collapse to localStorage (prdash.collapsed)', () => {
    wrap(<PipelineView state={state} focusedRepo={null} />)
    fireEvent.click(screen.getByRole('button', { name: /o\/r/ }))
    expect(JSON.parse(localStorage.getItem('prdash.collapsed')!)).toContain('o/r')
  })
  it('controlled filter: uses activeFilter prop and reports via onFilter', () => {
    const onFilter = vi.fn()
    wrap(<PipelineView state={state} focusedRepo={null} activeFilter={null} onFilter={onFilter} />)
    fireEvent.click(screen.getByRole('button', { name: /CI running/i }))
    expect(onFilter).toHaveBeenCalledWith('running')
  })
})
```
(Import `vi` from vitest; adjust the tile accessible name to `TILE_DEFINITIONS`' actual label — read `web/src/pipeline/StatusStrip.tsx` — the test must target the REAL label text.)
- [ ] **Step 2: Implement the three PipelineView edits** (each declared in the port header):
  1. Props: `{ state, focusedRepo, onFixPr, activeFilter: controlledFilter, onFilter, kiosk = false }`; internal filter state used only when `controlledFilter === undefined`: `const [ownFilter, setOwnFilter] = useState<Bucket | null>(null); const activeFilter = controlledFilter !== undefined ? controlledFilter : ownFilter; const setFilter = onFilter ?? setOwnFilter;` — pass `setFilter` to StatusStrip's `onFilter`, add `interactive={!kiosk}` to StatusStrip, `expandable={!kiosk}` on PrRow (replacing the bare `expandable`).
  2. Collapse persistence: copy the private-mode-safe helpers verbatim from `$PLOT/App.tsx:33-55` (`LS_KEY = 'prdash.collapsed'`, `readCollapsed()`, `writeCollapsed(set)`) into PipelineView above the component; initialize `useState(readCollapsed)` and call `writeCollapsed(n)` inside `toggle` after computing the new set.
- [ ] **Step 3: Compose the chrome in PipelineTab** — rewrite `web/src/pipeline/PipelineTab.tsx`:
```tsx
// web/src/pipeline/PipelineTab.tsx
// ccswitch host wrapper for the ported plotroom pipeline island (spec §2, §5, §6).
import { useCallback, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useFixPr } from '@/api/hooks'
import { ApiBaseProvider } from './ApiBaseContext'
import { useDashboard } from './useDashboard'
import { PipelineView } from './PipelineView'
import { HealthHeader } from './HealthHeader'
import { LegendPanel } from './LegendPanel'
import { PipelineSwitcher, useFocusedPipeline } from './PipelineSwitcher'
import { readKioskConfig } from './kiosk'
import { useKioskCycle } from './useKioskCycle'
import type { Bucket } from './StatusStrip'
import type { LaneStatus } from './types'
import './plotroom.css'

/** Lane chip → richest existing ccswitch surface (spec §5): Pipeline filter for
 *  the PR-flow lanes, Insights for the rest until richer targets exist. */
export function laneJumpTarget(laneId: string | null, status: LaneStatus | null):
  { kind: 'filter'; bucket: Bucket | null } | { kind: 'insights' } {
  switch (laneId) {
    case 'pr-ci': return { kind: 'filter', bucket: status === 'red' ? 'failed' : 'running' }
    case 'merge-queue': return { kind: 'filter', bucket: 'queued' }
    case 'deploy': return { kind: 'filter', bucket: 'deploy' }
    case 'failures': return { kind: 'filter', bucket: 'failed' }
    default: return { kind: 'insights' }   // main / scheduled / cost / null
  }
}

function Inner() {
  const fixPr = useFixPr()
  // eslint-disable-next-line react-hooks/exhaustive-deps -- mutate is a stable RQ handle
  const onFixPr = useCallback((id: string, number: number) => fixPr.mutate({ id, number }), [fixPr.mutate])
  const { state, stale, notifySupported, notifyEnabled, toggleNotify } = useDashboard()
  const navigate = useNavigate()
  const [{ kiosk, cycleSeconds }] = useState(readKioskConfig)
  const repos = useMemo(() => (state?.repos ?? []).map((r) => r.repo), [state])
  const [focused, focus] = useFocusedPipeline(repos, true, !kiosk)
  useKioskCycle(kiosk, cycleSeconds, repos, focused, focus)
  const [filter, setFilter] = useState<Bucket | null>(null)
  const [legendOpen, setLegendOpen] = useState(false)
  const legendBtnRef = useRef<HTMLButtonElement>(null)
  const onJumpToLane = useCallback((laneId: string | null, status: LaneStatus | null) => {
    const t = laneJumpTarget(laneId, status)
    if (t.kind === 'filter') setFilter(t.bucket)
    else navigate('/?tab=insights')
  }, [navigate])
  return (
    <>
      <div className="pipeline-chrome-bar">
        {!kiosk && <PipelineSwitcher repos={repos} focused={focused} onFocus={focus} />}
        {!kiosk && notifySupported && (
          <button type="button" className="bell-toggle" aria-pressed={notifyEnabled}
            title={notifyEnabled ? 'Browser notifications on' : 'Browser notifications off'}
            onClick={toggleNotify}>
            <span aria-hidden="true">{notifyEnabled ? '🔔' : '🔕'}</span>
          </button>
        )}
        {!kiosk && (
          <button type="button" ref={legendBtnRef} className="legend-toggle"
            aria-label="Open legend" onClick={() => setLegendOpen(true)}>?</button>
        )}
      </div>
      {state && <HealthHeader state={state} onJumpToLane={onJumpToLane} />}
      {stale && <p className="pipeline-stale" role="status">data stale — reconnecting…</p>}
      <LegendPanel open={legendOpen} onClose={() => setLegendOpen(false)} returnFocusRef={legendBtnRef} />
      <PipelineView state={state} focusedRepo={focused} onFixPr={onFixPr}
        activeFilter={filter} onFilter={setFilter} kiosk={kiosk} />
    </>
  )
}

export function PipelineTab() {
  return (
    <ApiBaseProvider base="/api/pipeline">
      <div className="prdash-root flex-1 min-h-0 overflow-y-auto">
        <Inner />
      </div>
    </ApiBaseProvider>
  )
}
```
  BEFORE finalizing: read the actual `useDashboard` return field names and the actual App.tsx bell markup (`$PLOT/App.tsx:230-243`) and LegendPanel props, and adjust this sketch to match them exactly (the ported code is the contract, not this sketch). ccswitch Insights navigation: check how Dashboard routes tabs (`grep -n "tab=insights\|insights" web/src/components/Dashboard.tsx | head`) and use the real mechanism.
- [ ] **Step 4: Kiosk cycle hook** — create `web/src/pipeline/useKioskCycle.ts` (new glue; plotroom's cycle effect lives in `$PLOT/App.tsx` around the kiosk block — read it and mirror the pause-when-hidden semantics):
```ts
// web/src/pipeline/useKioskCycle.ts
// Kiosk auto-cycle (spec §5): advance the focused repo every cycleSeconds,
// paused while the tab is hidden — mirrors plotroom App.tsx kiosk cycling.
import { useEffect } from 'react'

export function useKioskCycle(
  kiosk: boolean, cycleSeconds: number, repos: readonly string[],
  focused: string | null, focus: (repo: string) => void,
) {
  useEffect(() => {
    if (!kiosk || repos.length < 2) return
    const id = setInterval(() => {
      if (document.hidden) return
      const i = focused ? repos.indexOf(focused) : -1
      focus(repos[(i + 1) % repos.length]!)
    }, cycleSeconds * 1000)
    return () => clearInterval(id)
  }, [kiosk, cycleSeconds, repos, focused, focus])
}
```
  With a fake-timers test in `__tests__/useKioskCycle.test.ts` (advance `cycleSeconds*1000`, assert `focus` called with the next repo; assert no calls when `document.hidden` is stubbed true or `kiosk` false).
- [ ] **Step 5: Style the stale banner** (carry-forward g) — append to the ccswitch-extensions section of `plotroom.css`:
```css
.pipeline-stale { margin: 4px 0; padding: 4px 10px; border: 1px solid #d29922;
  border-radius: 6px; color: #d29922; font-size: 12px; }
```
- [ ] **Step 6: All Task-5 tests green** (blend + kiosk-cycle + extended PipelineTab smoke), then FULL web gates (`npx vitest run && npx tsc --noEmit && npx eslint src && npm run build`).
- [ ] **Step 7: Commit** — `feat(web/pipeline): chrome composition — HealthHeader, Legend, switcher, kiosk, bell, collapse persistence`.

---

### Task 6: Backend — per-PR queue substates + ready-merge response nuances

**Files:**
- Modify: `server/pipeline/dashboard_state.py` (queue-substate injection), `server/sessions/orchestrator.py` (`ready_merge` response)
- Test: `server/tests/test_dashboard_state.py`, `server/tests/test_ready_merge.py` (extend)

**Interfaces:**
- Consumes: Phase 1's `queue_view` classification (`unmergeable`, `queueBlocked` lists).
- Produces: `pr_view(..., queue_substate: str | None = None)` — when set (`"unmergeable"` or `"queue-blocked"`), overrides the PR's queue-stage substate; `dashboard_state` computes it from the repo's `queue_view` result. `ready_merge` returns `{markedReady, autoMergeArmed, alreadyArmed, cleanReadyToMerge}`.

- [ ] **Step 1: Failing tests** (append):
```python
def test_dashboard_state_injects_queue_substates():
    prs = {"o/r": [PipelinePr(pr=_pr(number=12, in_merge_queue=True)),
                   PipelinePr(pr=_pr(number=13, in_merge_queue=True))]}
    queues = {"o/r": [_entry(pr_number=12, position=1, state="UNMERGEABLE"),
                      _entry(pr_number=13, position=2, state="UNMERGEABLE")]}
    st = dashboard_state(prs, queues, {}, {"o/r": False}, generated_at="x")
    by_num = {p["number"]: p for p in st["repos"][0]["prs"]}
    assert by_num[12]["stage"]["substate"] == "unmergeable"
    assert by_num[13]["stage"]["substate"] == "queue-blocked"


def test_dashboard_state_group_failed_wins_over_queue_substate():
    # group-failed (from stage_result) must not be clobbered by injection
    failing_group = CheckGroup(kind="merge", checks=(_row(conclusion="failure"),),
                               total_seconds=0, cost_usd=None)
    prs = {"o/r": [PipelinePr(pr=_pr(number=12, in_merge_queue=True,
                                     check_groups=(failing_group,)))]}
    queues = {"o/r": [_entry(pr_number=12, position=1, state="UNMERGEABLE")]}
    st = dashboard_state(prs, queues, {}, {"o/r": False}, generated_at="x")
    assert st["repos"][0]["prs"][0]["stage"]["substate"] == "group-failed"
```
And in `test_ready_merge.py`, extend the already-armed test to assert `result["alreadyArmed"] is True` and the happy-path test `result["cleanReadyToMerge"] is False` / adjust to the new 4-key shape.
- [ ] **Step 2: Implement.** In `pr_view`, add parameter `queue_substate: str | None = None`; after building the dict: `if queue_substate and out["stage"]["stage"] == "queue" and out["stage"]["substate"] is None: out["stage"]["substate"] = queue_substate`. In `dashboard_state`, compute the repo's `qv = queue_view(entries, trains.get(repo)) if entries else None` ONCE (reuse it for the repo's `"queue"` value), then per PR: `qs = "unmergeable" if p.pr.number in (qv or {}).get("unmergeable", []) else "queue-blocked" if p.pr.number in (qv or {}).get("queueBlocked", []) else None`. In `ready_merge`: `already = merge.returncode != 0 and "already enabled" in (merge.stderr or "")`; return `{"markedReady": marked, "autoMergeArmed": merge.returncode == 0 or already, "alreadyArmed": already, "cleanReadyToMerge": False}` (`cleanReadyToMerge` stays honest-False until a mergeability probe exists — document with a comment).
- [ ] **Step 3: Tests green; ruff + mypy; commit** — `feat(pipeline): per-PR queue substates + ready-merge response nuances`.

---

### Task 7: Backend — minimal notifier (ci-failed / ready / overdue) over SSE

**Files:**
- Create: `server/pipeline/notifier.py`
- Modify: `server/api/pipeline.py` (SSE generator emits `event: notification` frames; per-frame exception guard — carry-forward (b))
- Test: `server/tests/test_pipeline_notifier.py`, extend `server/tests/test_pipeline_events.py`

**Interfaces:**
- Consumes: consecutive `DashboardState` dicts (the SSE assembler's output).
- Produces: `class TransitionNotifier: def diff(self, state: dict) -> list[dict]` — stateful; returns NotificationEvent dicts `{repo, prNumber, title, type, detail, rendered: {title, body}}` for conditions that NEWLY became active since the previous frame. Types (phase-2 minimal): `ci-failed` (stage.substate == 'ci-failed' or (stage 'ci'|'parked' and prior frame not failing... define as: substate == 'ci-failed'), `ready` (stage.stage == 'ready'), `overdue` (stage.overdue is True). Rendered exactly like plotroom's `renderNotification`: title `f"{repo}#{n} {LABEL}"` with LABELS `{'ci-failed': 'CI failed', 'ready': 'ready to merge', 'overdue': 'overdue'}`; body = PR title (no detail in the minimal emitter).

- [ ] **Step 1: Failing tests**:
```python
# server/tests/test_pipeline_notifier.py
"""Minimal frame-diffing notifier (spec §8 phase 2): ci-failed / ready / overdue.

Fires only on inactive→active transitions; re-fires only after the condition
clears for a frame. Rendered strings mirror plotroom notifier.ts renderNotification.
"""
from server.pipeline.notifier import TransitionNotifier


def _frame(substate=None, stage="ci", overdue=False, number=7):
    return {"generatedAt": "t", "staleSince": None, "repos": [{
        "repo": "o/r", "hasDeploy": False, "queue": None, "prs": [{
            "repo": "o/r", "number": number, "title": "fix auth", "url": "u",
            "stage": {"stage": stage, "substate": substate, "percent": None,
                      "etaSeconds": None, "etaRangeSeconds": None, "overdue": overdue},
        }]}]}


def test_fires_on_new_ci_failed_only_once():
    n = TransitionNotifier()
    assert n.diff(_frame()) == []                       # baseline frame
    events = n.diff(_frame(substate="ci-failed", stage="parked"))
    assert [e["type"] for e in events] == ["ci-failed"]
    assert events[0]["rendered"] == {"title": "o/r#7 CI failed", "body": "fix auth"}
    assert n.diff(_frame(substate="ci-failed", stage="parked")) == []   # still active → no refire


def test_refires_after_condition_clears():
    n = TransitionNotifier()
    n.diff(_frame())
    n.diff(_frame(substate="ci-failed", stage="parked"))
    n.diff(_frame())                                    # cleared
    assert [e["type"] for e in n.diff(_frame(substate="ci-failed", stage="parked"))] == ["ci-failed"]


def test_ready_and_overdue():
    n = TransitionNotifier()
    n.diff(_frame())
    ev = n.diff(_frame(stage="ready"))
    assert [e["type"] for e in ev] == ["ready"]
    assert ev[0]["rendered"]["title"] == "o/r#7 ready to merge"
    ev2 = n.diff(_frame(stage="ready", overdue=True))
    assert [e["type"] for e in ev2] == ["overdue"]


def test_first_frame_is_baseline_never_fires():
    n = TransitionNotifier()
    assert n.diff(_frame(substate="ci-failed", stage="parked")) == []
```
- [ ] **Step 2: Implement**:
```python
# server/pipeline/notifier.py
"""Minimal frame-diffing notifier (spec §8 phase 2; full engine lands in Phase 4).

Compares consecutive DashboardState frames and emits plotroom-shaped
NotificationEvent dicts for conditions that NEWLY became active. The first
frame is a baseline: nothing fires on it (avoids a notification storm at
server start). rendered strings mirror plotroom server/notifier.ts
renderNotification — the browser displays them verbatim.
"""
from __future__ import annotations

_LABELS = {"ci-failed": "CI failed", "ready": "ready to merge", "overdue": "overdue"}


def _active_conditions(pr: dict) -> set[str]:
    s = pr.get("stage") or {}
    out: set[str] = set()
    if s.get("substate") == "ci-failed":
        out.add("ci-failed")
    if s.get("stage") == "ready":
        out.add("ready")
    if s.get("overdue"):
        out.add("overdue")
    return out


def _render(repo: str, number: int, title: str, kind: str) -> dict:
    return {"title": f"{repo}#{number} {_LABELS[kind]}", "body": title}


class TransitionNotifier:
    def __init__(self) -> None:
        self._prev: dict[tuple[str, int], set[str]] | None = None

    def diff(self, state: dict) -> list[dict]:
        current: dict[tuple[str, int], set[str]] = {}
        prs: dict[tuple[str, int], dict] = {}
        for repo in state.get("repos", []):
            for pr in repo.get("prs", []):
                key = (repo["repo"], pr["number"])
                current[key] = _active_conditions(pr)
                prs[key] = pr
        if self._prev is None:            # baseline: record, never fire
            self._prev = current
            return []
        events: list[dict] = []
        for key, active in current.items():
            new = active - self._prev.get(key, set())
            for kind in sorted(new):
                repo, number = key
                title = prs[key].get("title", "")
                events.append({
                    "repo": repo, "prNumber": number, "title": title,
                    "type": kind, "detail": "",
                    "rendered": _render(repo, number, title, kind),
                })
        self._prev = current
        return events
```
- [ ] **Step 3: Wire into the SSE generator** (`server/api/pipeline.py`) — restructure `_state_frame` to return the state DICT (rename to `_build_state(orch) -> dict`), and in `pipeline_events`:
```python
    notifier = TransitionNotifier()          # per-connection; baseline = first frame

    async def gen():
        while True:
            if await request.is_disconnected():
                return
            try:
                state = await _build_state(orch)
            except Exception:                 # carry-forward (b): one bad frame must
                logger.exception("pipeline SSE frame build failed")   # not kill the feed
                await asyncio.sleep(_EVENTS_INTERVAL_S)
                continue
            yield f"data: {json.dumps(state)}\n\n"
            for ev in notifier.diff(state):
                yield f"event: notification\ndata: {json.dumps(ev)}\n\n"
            await asyncio.sleep(_EVENTS_INTERVAL_S)
```
(Use the module's existing logger idiom — check how sibling api modules get a logger.) Extend `test_pipeline_events.py`: a test driving two frames where the second contains a newly-failing PR asserts an `event: notification` line appears (reuse the ASGI-driver pattern already in that file; a fake orchestrator can swap its `all_prs` return between calls).
- [ ] **Step 4: Tests green; ruff + mypy; commit** — `feat(pipeline): minimal SSE notifier (ci-failed/ready/overdue) + frame-exception guard`.

---

### Task 8: Facade-coverage guard (carry-forward c)

**Files:**
- Test: `server/tests/test_orchestrator_facade.py` (extend)

**Interfaces:** none new — a reflection guard.

- [ ] **Step 1: Write the guard test**:
```python
def test_facade_covers_every_orchestrator_method_the_api_calls():
    """Every `orch.<method>(` call in server/api/*.py must exist on the facade —
    the RemoteOrchestrator breadth test guards daemon mode; this guards in-process
    mode (the Task-3 facade gap class)."""
    import re
    from pathlib import Path
    from server.sessions.facade import InProcessOrchestratorAdapter

    api_dir = Path(__file__).resolve().parents[1] / "api"
    called = set()
    for f in api_dir.glob("*.py"):
        called |= set(re.findall(r"\borch\.(\w+)\(", f.read_text()))
    missing = {m for m in called if not hasattr(InProcessOrchestratorAdapter, m)}
    assert not missing, f"facade missing: {sorted(missing)}"
```
- [ ] **Step 2: Run** — if it finds real gaps, add the missing one-liner facade methods (same `_maybe_await` idiom) in the same commit.
- [ ] **Step 3: ruff + mypy + commit** — `test(facade): guard in-process facade coverage of API-called orchestrator methods`.

---

### Task 9: Full verification + PR

- [ ] **Step 1: Full gates** — web: `npx vitest run && npx tsc --noEmit && npx eslint src && npm run build`; server: `ruff check server/ src/ && mypy server/ --ignore-missing-imports` + full pytest with the Global-Constraints exclusions.
- [ ] **Step 2: Manual smoke (if a dev server is practical):** Pipeline tab shows HealthHeader chips; `?` opens the Legend; the switcher focuses a repo and reflects `?pipeline=`; collapse survives reload; `?kiosk=1&cycle=10` cycles read-only. Otherwise rely on the test evidence and note it in the PR.
- [ ] **Step 3: Push + PR + auto-merge**:
```bash
git push -u origin feat/plotroom-pipeline-phase2
gh pr create --title "feat(pipeline): plotroom pipeline replica — Phase 2 chrome + hardening" --body "$(cat <<'EOF'
Phase 2 of the plotroom exact-replica (spec §5/§8): HealthHeader lane chips + rollup,
LegendPanel, PipelineSwitcher (?pipeline= + localStorage), kiosk mode (?kiosk=1&cycle=N),
repo-collapse persistence, notification bell backed by a minimal SSE notifier
(ci-failed/ready/overdue). Closes Phase-1 carry-forwards: per-PR queue substates
(unmergeable/queue-blocked), ready-merge response nuances, SSE per-frame exception guard,
in-process facade coverage guard, stale-banner styling.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
gh pr merge --squash --auto
```

---

## Self-Review Notes

- **Spec §8 phase-2 coverage:** HealthHeader (T2+T5), LegendPanel (T3+T5), PipelineSwitcher (T4+T5), kiosk (T4+T5), collapse persistence (T5), bell + minimal emitter (T5+T7). Carry-forwards: (a) accepted-as-documented (10 s cadence), (b) T7, (c) T8, (e) covered by T7's notification-path SSE test, (g) T5. Queue substates + ready-merge nuances: T6.
- **Type consistency:** `laneJumpTarget` consumes lane ids exactly as Task 1 produces them; `PipelineView` controlled-filter props match `StatusStrip`'s `Bucket`/`onFilter` vocabulary; `TransitionNotifier.diff` consumes the exact dict shape `dashboard_state` emits.
- **Sketch-vs-port rule:** where this plan sketches JSX that composes PORTED components (Task 5), the ported component's actual props are the contract — the implementer adjusts the sketch, never the port.
