# M-Track-3: Stage Tiles + Session-Inclusive Repos Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a row of per-stage metric tiles (CI · Ready · Queue · Deploy · Merged) above the Pipeline board that count PRs per stage and act as multi-select toggle filters, and make repos with archived (not just live) sessions appear on the board.

**Architecture:** A pure `stageBucketOf` helper maps a PR to its current stage bucket using the same `node_states` the track renders; a `StageTiles` component counts per bucket and toggles a filter `Set` owned by `PipelineView`; `PipelineBoard` filters PRs by the active set and drops empty repo groups. One backend change makes the population poller's watched-repo set include archived-session repos (distinct `repo` over the persisted `DbSession` table).

**Tech Stack:** React + TypeScript + Vite / vitest (frontend); Python 3.11 / SQLAlchemy async / pytest (backend).

## Global Constraints

- **Spec:** `docs/superpowers/specs/2026-06-30-pipeline-stage-tiles-and-session-repos-design.md`.
- **Tile buckets** are the fixed ordered set `['ci','ready','queue','deploy','merged']`, labels `CI · Ready · Queue · Deploy · Merged`. A PR's bucket = the `kind` of its **current** track node (the `active` node in `node_states`, else the **last `done`** node in model order); fallback to the flat `pr.stage` string (`ci→ci, ready→ready, parked→ci, queue→queue, merged→merged`); else `null`. This MUST use the same `node_states` the `StageTrack` renders so counts match what is lit.
- **Toggle = multi-select union.** Active filter is a `Set<StageBucket>`; **empty set = no filter (show all)** — today's behaviour. Non-empty → board shows only PRs whose bucket ∈ set; repo groups with zero matching PRs are not rendered. **Tile counts are computed from the UNFILTERED PR list** (stable while a filter is on).
- **Colours/labels** reuse the existing stage palette vocabulary; no new design tokens.
- **Backend:** widen the watched-repo set to repos across **all persisted sessions (live + archived)** — `_pr_repos` becomes async and unions in-memory `_records` repos with `SELECT DISTINCT repo FROM sessions WHERE repo IS NOT NULL`. **`retention_days = 7` is UNCHANGED.** No new "session-only / empty-track" repo rows — every shown repo still has ≥1 PR.
- **Signature-change discipline (memory `signature-change-needs-full-suite`):** `_pr_repos` goes sync→async — grep ALL call sites and run the WHOLE `server/tests/`, not a subset.
- **Backend gate:** `ruff` + `mypy` + `pytest`, via `/mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/42659d9c/.venv/bin/python`; auth/API tests prefix `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1`. **Frontend gate (in `web/`):** `npx vitest run`, `npx tsc -b --force`, `npx eslint . --max-warnings 0`.

---

### Task 1: `stageBucketOf` helper + bucket vocabulary

**Files:**
- Create: `web/src/session/stageBucket.ts`
- Test: `web/src/session/__tests__/stageBucket.test.ts`

**Interfaces:**
- Consumes: `PipelinePr`, `StageModel`, `StageNode` (`web/src/api/types.ts`). `StageNode.kind ∈ 'ci'|'gate'|'queue'|'deploy'|'merged'`; `node_states?: {id:string; state:NodeState}[]`; `pr.stage?: string`.
- Produces:
  - `export type StageBucket = 'ci' | 'ready' | 'queue' | 'deploy' | 'merged'`
  - `export const STAGE_BUCKETS: StageBucket[]` (the fixed order)
  - `export const STAGE_BUCKET_LABELS: Record<StageBucket, string>`
  - `export function stageBucketOf(pr: PipelinePr, model: StageModel | undefined): StageBucket | null`

- [ ] **Step 1: Write the failing test**

```ts
// web/src/session/__tests__/stageBucket.test.ts
import { describe, it, expect } from 'vitest'
import { stageBucketOf, STAGE_BUCKETS } from '@/session/stageBucket'
import type { PipelinePr, StageModel } from '@/api/types'

const model: StageModel = {
  repo: 'o/r',
  nodes: [
    { id: 'ci', label: 'CI', kind: 'ci', order: 0, source: 'derived' },
    { id: 'ready', label: 'Ready', kind: 'gate', order: 1, source: 'derived' },
    { id: 'queue', label: 'Queue', kind: 'queue', order: 2, source: 'derived' },
    { id: 'deploy:prod', label: 'prod', kind: 'deploy', order: 3, source: 'derived' },
    { id: 'merged', label: 'Merged', kind: 'merged', order: 4, source: 'derived' },
  ],
}
const pr = (o: Partial<PipelinePr>): PipelinePr => ({
  number: 1, url: 'u', state: 'open', checks: 'pending', title: 't', deployed: false,
  session: null, node_states: [], ...o,
} as unknown as PipelinePr)

describe('stageBucketOf', () => {
  it('uses the active node kind (gate→ready)', () => {
    expect(stageBucketOf(pr({ node_states: [
      { id: 'ci', state: 'done' }, { id: 'ready', state: 'active' }, { id: 'queue', state: 'pending' },
    ] }), model)).toBe('ready')
  })
  it('deploy node → deploy bucket', () => {
    expect(stageBucketOf(pr({ node_states: [
      { id: 'queue', state: 'done' }, { id: 'deploy:prod', state: 'active' },
    ] }), model)).toBe('deploy')
  })
  it('no active node → last done node (fully merged → merged)', () => {
    expect(stageBucketOf(pr({ node_states: [
      { id: 'ci', state: 'done' }, { id: 'merged', state: 'done' },
    ] }), model)).toBe('merged')
  })
  it('falls back to flat stage when no model/node_states', () => {
    expect(stageBucketOf(pr({ node_states: [], stage: 'queue' } as Partial<PipelinePr>), undefined)).toBe('queue')
    expect(stageBucketOf(pr({ node_states: [], stage: 'parked' } as Partial<PipelinePr>), undefined)).toBe('ci')
  })
  it('unknown → null', () => {
    expect(stageBucketOf(pr({ node_states: [], stage: '' } as Partial<PipelinePr>), undefined)).toBeNull()
  })
  it('exposes the fixed bucket order', () => {
    expect(STAGE_BUCKETS).toEqual(['ci', 'ready', 'queue', 'deploy', 'merged'])
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run (in `web/`): `npx vitest run src/session/__tests__/stageBucket.test.ts`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `stageBucket.ts`**

```ts
// web/src/session/stageBucket.ts
import type { PipelinePr, StageModel, StageNode } from '@/api/types'

export type StageBucket = 'ci' | 'ready' | 'queue' | 'deploy' | 'merged'

export const STAGE_BUCKETS: StageBucket[] = ['ci', 'ready', 'queue', 'deploy', 'merged']

export const STAGE_BUCKET_LABELS: Record<StageBucket, string> = {
  ci: 'CI', ready: 'Ready', queue: 'Queue', deploy: 'Deploy', merged: 'Merged',
}

const KIND_TO_BUCKET: Record<StageNode['kind'], StageBucket> = {
  ci: 'ci', gate: 'ready', queue: 'queue', deploy: 'deploy', merged: 'merged',
}

const STAGE_STR_TO_BUCKET: Record<string, StageBucket> = {
  ci: 'ci', ready: 'ready', parked: 'ci', queue: 'queue', merged: 'merged',
}

// A PR's bucket = the kind of its CURRENT track node (active, else last done in
// model order), so a tile's count matches what is lit on the StageTrack.
export function stageBucketOf(pr: PipelinePr, model: StageModel | undefined): StageBucket | null {
  const states = pr.node_states
  if (model && states && states.length) {
    const stateById = new Map(states.map((s) => [s.id, s.state]))
    const nodes = [...model.nodes].sort((a, b) => a.order - b.order)
    let node = nodes.find((n) => stateById.get(n.id) === 'active')
    if (!node) {
      const done = nodes.filter((n) => stateById.get(n.id) === 'done')
      node = done.length ? done[done.length - 1] : undefined
    }
    if (node) return KIND_TO_BUCKET[node.kind] ?? null
  }
  const s = pr.stage
  return (s && STAGE_STR_TO_BUCKET[s]) ?? null
}
```

- [ ] **Step 4: Run to verify it passes**

Run (in `web/`): `npx vitest run src/session/__tests__/stageBucket.test.ts && npx tsc -b --force`
Expected: PASS, no type errors.

- [ ] **Step 5: Commit**

```bash
git add web/src/session/stageBucket.ts web/src/session/__tests__/stageBucket.test.ts
git commit -m "feat(web): stageBucketOf — map a PR to its current stage bucket"
```

---

### Task 2: `StageTiles` component

**Files:**
- Create: `web/src/components/StageTiles.tsx`
- Test: `web/src/components/__tests__/StageTiles.test.tsx`

**Interfaces:**
- Consumes: `stageBucketOf`, `STAGE_BUCKETS`, `STAGE_BUCKET_LABELS`, `StageBucket` (Task 1); `PipelinePr`, `StageModel`; `cn` (`@/lib/utils`).
- Produces: `export function StageTiles({ prs, stageModels, active, onToggle }: { prs: PipelinePr[]; stageModels: Record<string, StageModel>; active: Set<StageBucket>; onToggle: (b: StageBucket) => void }): JSX.Element`.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/__tests__/StageTiles.test.tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { StageTiles } from '@/components/StageTiles'
import type { PipelinePr, StageModel } from '@/api/types'

const model: StageModel = { repo: 'o/r', nodes: [
  { id: 'queue', label: 'Queue', kind: 'queue', order: 0, source: 'derived' },
  { id: 'merged', label: 'Merged', kind: 'merged', order: 1, source: 'derived' },
] }
const pr = (n: number, ns: { id: string; state: 'active' | 'done' | 'pending' | 'failed' }[]): PipelinePr =>
  ({ number: n, url: 'u', state: 'open', checks: 'pending', title: 't', deployed: false,
     repo: 'o/r', session: null, node_states: ns } as unknown as PipelinePr)

const prs = [
  pr(1, [{ id: 'queue', state: 'active' }]),
  pr(2, [{ id: 'merged', state: 'done' }]),
  pr(3, [{ id: 'merged', state: 'done' }]),
]

describe('StageTiles', () => {
  it('counts PRs per bucket', () => {
    render(<StageTiles prs={prs} stageModels={{ 'o/r': model }} active={new Set()} onToggle={() => {}} />)
    expect(screen.getByTestId('stage-tile-queue')).toHaveTextContent('1')
    expect(screen.getByTestId('stage-tile-merged')).toHaveTextContent('2')
  })
  it('disables a zero-count tile', () => {
    render(<StageTiles prs={prs} stageModels={{ 'o/r': model }} active={new Set()} onToggle={() => {}} />)
    expect(screen.getByTestId('stage-tile-ci')).toBeDisabled()
  })
  it('marks an active tile pressed and toggles on click', async () => {
    const onToggle = vi.fn()
    const user = userEvent.setup()
    render(<StageTiles prs={prs} stageModels={{ 'o/r': model }} active={new Set(['queue'])} onToggle={onToggle} />)
    expect(screen.getByTestId('stage-tile-queue')).toHaveAttribute('aria-pressed', 'true')
    await user.click(screen.getByTestId('stage-tile-merged'))
    expect(onToggle).toHaveBeenCalledWith('merged')
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run (in `web/`): `npx vitest run src/components/__tests__/StageTiles.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `StageTiles.tsx`**

```tsx
// web/src/components/StageTiles.tsx
import { cn } from '@/lib/utils'
import {
  STAGE_BUCKETS, STAGE_BUCKET_LABELS, stageBucketOf, type StageBucket,
} from '@/session/stageBucket'
import type { PipelinePr, StageModel } from '@/api/types'

export function StageTiles({
  prs, stageModels, active, onToggle,
}: {
  prs: PipelinePr[]
  stageModels: Record<string, StageModel>
  active: Set<StageBucket>
  onToggle: (b: StageBucket) => void
}) {
  const counts: Record<StageBucket, number> = { ci: 0, ready: 0, queue: 0, deploy: 0, merged: 0 }
  for (const p of prs) {
    const b = stageBucketOf(p, stageModels[p.repo ?? ''])
    if (b) counts[b] += 1
  }
  return (
    <div className="flex flex-wrap gap-2" role="group" aria-label="Filter by stage">
      {STAGE_BUCKETS.map((b) => {
        const n = counts[b]
        const on = active.has(b)
        const disabled = n === 0
        return (
          <button
            key={b}
            type="button"
            data-testid={`stage-tile-${b}`}
            aria-pressed={on}
            disabled={disabled}
            onClick={() => onToggle(b)}
            className={cn(
              'flex items-center gap-1.5 rounded border px-2.5 py-1 text-xs',
              disabled && 'opacity-40 cursor-default',
              on
                ? 'border-accent bg-accent/15 text-accent-foreground ring-2 ring-accent/40'
                : 'border-gray-200 dark:border-gray-700 hover:bg-muted/60',
            )}
          >
            <span className="font-medium uppercase tracking-wide text-[10px]">
              {STAGE_BUCKET_LABELS[b]}
            </span>
            <span className="tabular-nums">{n}</span>
          </button>
        )
      })}
    </div>
  )
}
```

- [ ] **Step 4: Run to verify it passes**

Run (in `web/`): `npx vitest run src/components/__tests__/StageTiles.test.tsx && npx tsc -b --force`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/components/StageTiles.tsx web/src/components/__tests__/StageTiles.test.tsx
git commit -m "feat(web): StageTiles — per-stage count tiles with toggle state"
```

---

### Task 3: Wire tiles + filter into `PipelineView` / `PipelineBoard`

**Files:**
- Modify: `web/src/components/PipelineView.tsx`
- Test: `web/src/components/__tests__/PipelineBoard.test.tsx` (extend) + `web/src/components/__tests__/PipelineTab.test.tsx` (if it renders PipelineView — verify still green)

**Interfaces:**
- Consumes: `StageTiles` (Task 2); `stageBucketOf`, `StageBucket` (Task 1).
- Produces: `PipelineBoard` gains a prop `activeStages: Set<StageBucket>`; `PipelineView` owns the `active` state.

- [ ] **Step 1: Write the failing tests**

```tsx
// add to web/src/components/__tests__/PipelineBoard.test.tsx
import type { StageBucket } from '@/session/stageBucket'

it('filters PRs to the active stage buckets and hides empty repos', () => {
  const model = { repo: 'o/r', nodes: [
    { id: 'queue', label: 'Queue', kind: 'queue', order: 0, source: 'derived' },
    { id: 'merged', label: 'Merged', kind: 'merged', order: 1, source: 'derived' },
  ] }
  const prs = [
    { repo: 'o/r', number: 1, url: 'u', state: 'open', checks: 'pending', title: 'q', deployed: false,
      session: null, node_states: [{ id: 'queue', state: 'active' }] },
    { repo: 'o/r', number: 2, url: 'u', state: 'merged', checks: 'passing', title: 'm', deployed: false,
      session: null, node_states: [{ id: 'merged', state: 'done' }] },
    { repo: 'x/y', number: 3, url: 'u', state: 'merged', checks: 'passing', title: 'm2', deployed: false,
      session: null, node_states: [{ id: 'merged', state: 'done' }] },
  ] as unknown as PipelinePr[]
  const models = { 'o/r': model, 'x/y': { ...model, repo: 'x/y' } } as Record<string, StageModel>
  // queue filter → only PR #1 and only repo o/r
  render(<PipelineBoard prs={prs} stageModels={models} activeStages={new Set<StageBucket>(['queue'])} />)
  expect(screen.getByTestId('pipeline-row-1')).toBeInTheDocument()
  expect(screen.queryByTestId('pipeline-row-2')).toBeNull()
  expect(screen.queryByTestId('pipeline-row-3')).toBeNull()
})

it('empty active set shows everything', () => {
  const prs = [{ repo: 'o/r', number: 1, url: 'u', state: 'open', checks: 'pending', title: 't',
    deployed: false, session: null, node_states: [] }] as unknown as PipelinePr[]
  render(<PipelineBoard prs={prs} stageModels={{}} activeStages={new Set()} />)
  expect(screen.getByTestId('pipeline-row-1')).toBeInTheDocument()
})
```

- [ ] **Step 2: Run to verify it fails**

Run (in `web/`): `npx vitest run src/components/__tests__/PipelineBoard.test.tsx`
Expected: FAIL — `PipelineBoard` has no `activeStages` prop / PR #2,#3 still render.

- [ ] **Step 3: Add `activeStages` filtering to `PipelineBoard`**

In `web/src/components/PipelineView.tsx`, add imports:
```tsx
import { StageTiles } from './StageTiles'
import { stageBucketOf, type StageBucket } from '@/session/stageBucket'
```
Change the `PipelineBoard` signature to accept `activeStages` and filter before grouping. Replace lines 12-42 (the props destructure + empty-state + grouping start) so it reads:
```tsx
export function PipelineBoard({
  prs,
  stageModels,
  activeStages,
  onOpenSession,
  onFixPr,
}: {
  prs: PipelinePr[]
  stageModels: Record<string, StageModel>
  activeStages: Set<StageBucket>
  onOpenSession?: (sessionId: string) => void
  onFixPr?: (sessionId: string, number: number) => void
}) {
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({})
  const toggle = (key: string) => setCollapsed((c) => ({ ...c, [key]: !c[key] }))
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  const toggleExpand = (key: string) => setExpanded((e) => ({ ...e, [key]: !e[key] }))

  const shown = activeStages.size === 0
    ? prs
    : prs.filter((p) => {
        const b = stageBucketOf(p, stageModels[p.repo ?? ''])
        return b != null && activeStages.has(b)
      })

  if (shown.length === 0) {
    return (
      <p className="text-sm text-muted-foreground py-8 text-center">
        {activeStages.size > 0 ? 'No PRs match the selected stages.' : 'No open PRs in your repos.'}
      </p>
    )
  }

  // Group PRs by repo.
  const byRepo: Record<string, PipelinePr[]> = {}
  for (const p of shown) {
    const repo = p.repo ?? ''
    if (!byRepo[repo]) byRepo[repo] = []
    byRepo[repo].push(p)
  }
```
(Leave the rest of `PipelineBoard` — the `repos` sort and the JSX — unchanged.)

- [ ] **Step 4: Render `StageTiles` + own the filter state in `PipelineView`**

Replace `PipelineView` (lines 151-164) with:
```tsx
export function PipelineView() {
  const navigate = useNavigate()
  const { data: prs = [] } = useAllPrs()
  const { data: stageModels = {} } = useStageModels()
  const fixPr = useFixPr()
  const [active, setActive] = useState<Set<StageBucket>>(new Set())
  const onToggle = (b: StageBucket) =>
    setActive((prev) => {
      const next = new Set(prev)
      if (next.has(b)) next.delete(b)
      else next.add(b)
      return next
    })
  return (
    <div className="space-y-3">
      <StageTiles prs={prs} stageModels={stageModels} active={active} onToggle={onToggle} />
      <PipelineBoard
        prs={prs}
        stageModels={stageModels}
        activeStages={active}
        onOpenSession={(id) => navigate(`/session/${id}`)}
        onFixPr={(id, number) => fixPr.mutate({ id, number })}
      />
    </div>
  )
}
```

- [ ] **Step 5: Run the targeted tests + fix any existing PipelineBoard test that omits the new prop**

Run (in `web/`): `npx vitest run src/components/__tests__/PipelineBoard.test.tsx src/components/__tests__/PipelineTab.test.tsx`
Expected: PASS. If a pre-existing `PipelineBoard` render in the test file omits `activeStages`, add `activeStages={new Set()}` to it (the prop is required).

- [ ] **Step 6: Full frontend gate + commit**

Run (in `web/`): `npx vitest run && npx tsc -b --force && npx eslint . --max-warnings 0`
Expected: all green.

```bash
git add web/src/components/PipelineView.tsx web/src/components/__tests__/PipelineBoard.test.tsx
git commit -m "feat(web): stage tiles filter the pipeline board (multi-select union)"
```

---

### Task 4: Watch archived-session repos (backend `_pr_repos` async)

**Files:**
- Modify: `server/sessions/orchestrator.py` (`_pr_repos`, ~1135-1137)
- Test: `server/tests/test_orchestrator_pr_repos.py` (new)

**Interfaces:**
- Consumes: `DbSession` (`server/db/models.py`: `repo: str | None`, `status: str`), `db_session` (async ctx manager, already imported in orchestrator).
- Produces: `Orchestrator._pr_repos(self) -> list[str]` becomes **async** (`async def`). Returns sorted distinct repos across live `_records` ∪ persisted `DbSession` rows.

- [ ] **Step 1: Confirm the only caller awaits an awaitable provider**

Run:
```bash
cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/42659d9c
grep -rn "_pr_repos" server/ | grep -v "def _pr_repos"
grep -n "inspect.isawaitable" server/github/pr_population.py
```
Expected: the only use is `self._pr_population.set_repos_provider(self._pr_repos)` (orchestrator.py ~252), and the poller's provider call guards with `inspect.isawaitable(result)` (pr_population.py ~118) — so an async `_pr_repos` is awaited correctly. If any OTHER site calls `_pr_repos()` synchronously, STOP and report it (it would need an `await`).

- [ ] **Step 2: Write the failing test**

```python
# server/tests/test_orchestrator_pr_repos.py
import asyncio


def test_pr_repos_includes_archived_session_repos(orch_with_db):
    """_pr_repos unions live in-memory repos with persisted (incl. archived) session repos."""
    orch, seed_session = orch_with_db
    # one ARCHIVED session in the DB on repo "owner/archived" (no live record)
    seed_session(repo="owner/archived", status="archived")
    # one LIVE in-memory record on repo "owner/live"
    orch._records["s-live"] = _FakeRecord(id="s-live", name="live", repo="owner/live")

    repos = asyncio.run(orch._pr_repos())
    assert "owner/archived" in repos
    assert "owner/live" in repos
    assert repos == sorted(set(repos))  # sorted + distinct


class _FakeRecord:
    def __init__(self, id, name, repo):
        self.id, self.name, self.repo = id, name, repo
```

> **Implementer note:** this repo's orchestrator tests already build an orchestrator over a temp DB. Find the existing fixture (grep `def orch` / `Orchestrator(` / `db_session` in `server/tests/`) and adapt it into an `orch_with_db` fixture that yields `(orch, seed_session)` where `seed_session(repo, status)` inserts a `DbSession` row via `db_session()`. If no reusable fixture exists, build the orchestrator the same way an existing orchestrator test does and insert the row directly with `async with db_session() as db: db.add(DbSession(id=..., name=..., repo=repo, status=status)); await db.commit()`. The assertion (archived repo present) is what matters; match the harness to the file's conventions.

- [ ] **Step 3: Run to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_orchestrator_pr_repos.py -v`
Expected: FAIL — `_pr_repos` is sync (`asyncio.run` on a list raises) and/or the archived repo is absent.

- [ ] **Step 4: Make `_pr_repos` async + union archived repos**

In `server/sessions/orchestrator.py`, replace `_pr_repos` (lines ~1135-1137) with:
```python
    async def _pr_repos(self) -> list[str]:
        """Distinct repos to watch: live in-memory sessions ∪ all persisted sessions
        (incl. archived), so a repo with only an archived session still appears."""
        repos = {r.repo for r in self._records.values() if r.repo}
        from sqlalchemy import select
        from server.db.models import DbSession
        try:
            async with db_session() as db:
                rows = (
                    await db.execute(
                        select(DbSession.repo).where(DbSession.repo.is_not(None)).distinct()
                    )
                ).scalars().all()
            repos.update(r for r in rows if r)
        except Exception:
            log.warning("orchestrator.pr_repos_db_query_failed", exc_info=True)
        return sorted(repos)
```
(If `db_session` / `log` are not already imported at module scope, they are — `_pr_autofix_mode` uses `db_session()` and the module has a `log`. Confirm and reuse; do not add duplicate imports.)

- [ ] **Step 5: Run the new test + the orchestrator/population tests**

Run:
```bash
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest \
  server/tests/test_orchestrator_pr_repos.py server/tests/test_pr_population.py -v
```
Expected: PASS.

- [ ] **Step 6: Lint/type + WHOLE backend suite (signature change)**

```bash
.venv/bin/python -m ruff check server/sessions/orchestrator.py
.venv/bin/python -m mypy server/sessions/orchestrator.py   # ignore pre-existing ccswitch import-untyped
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/ -q
```
Expected: ruff/mypy clean; full suite — no NEW failures vs `origin/master` baseline (the known environmental failures: test_ws StaticPool, xvfb, real-daemon socket). Because this is a sync→async signature change, the whole-suite run is the gate (memory `signature-change-needs-full-suite`).

- [ ] **Step 7: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_orchestrator_pr_repos.py
git commit -m "feat(pipeline): watch archived-session repos in the population set"
```

---

## Self-Review

**Spec coverage:**
- Per-stage tiles (CI/Ready/Queue/Deploy/Merged) with counts → Tasks 1 (bucket) + 2 (tiles). ✅
- Bucket = current track node, fallback to flat stage → Task 1 `stageBucketOf`. ✅
- Multi-select union toggle; empty = all; hide empty repos; counts from unfiltered → Tasks 2 (counts) + 3 (filter). ✅
- Reuse stage palette, no new tokens → Task 2 (accent/muted classes only). ✅
- Archived-session repos watched; `retention_days` unchanged; no empty-track rows → Task 4. ✅
- Signature-change discipline (grep callers + whole suite) → Task 4 Steps 1 + 6. ✅

**Placeholder scan:** Task 4 Step 2's implementer note points at the file's existing orchestrator-test fixture (a "reuse the harness" pointer); the test body + assertion are concrete. No bare TODO/TBD.

**Type consistency:** `StageBucket`, `STAGE_BUCKETS`, `STAGE_BUCKET_LABELS`, `stageBucketOf(pr, model)` are defined in Task 1 and consumed identically in Tasks 2-3; `StageTiles` prop shape `{prs, stageModels, active, onToggle}` matches between Task 2's definition and Task 3's render; `PipelineBoard`'s new `activeStages: Set<StageBucket>` is consistent between Task 3's signature, its filter, and its tests; `_pr_repos` is `async def … -> list[str]` in Task 4 and awaited via the existing isawaitable provider path.
