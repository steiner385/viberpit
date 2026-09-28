# Automations detail: Needs you tab, stat tiles, phone Back, status labels — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every automation's actionable items a Needs you tab (with a three-row Overview preview), render `status.counts` as stat tiles above the strip, add a Back link on phone/tablet to the Delivery and Roadmap detail pages, and label session statuses in the Sessions tab.

**Architecture:** All web work sits in `web/src/features/delivery/automation/` on the existing `AutomationDetail` pane (`ui/Tabs` + `?tab=` URL state); `StatTiles` moves to `web/src/ui/` because two features now share it. One server change trims the runner's status text (`server/automations/backlog_runner.py`) because the counts it spelled out become tiles. The Back link is a `PageHeader.back` prop gated on `useViewport().mode !== 'desktop'` in the two list/detail pages.

**Tech Stack:** React 19 + TypeScript + Tailwind v4 (tokens only — `viberpit/no-raw-palette` lint rule), vitest + testing-library + jest-axe, Playwright e2e; Python/FastAPI server with pytest.

**Spec:** `docs/superpowers/specs/2026-09-28-automations-needs-you-tiles-back-design.md`

## Global Constraints

- Branch: `feat/automations-needs-you-tiles-back` (already created off `origin/master`; the spec is committed on it). Never commit to `master`.
- Web commands run as `(cd web && …)` subshells — a bare `cd web` wedges the Bash hook for the rest of the session. `web/node_modules` is a symlink to `/home/tony/GitHub/viberpit-deploy/web/node_modules`.
- Web gates before pushing: `npm run typecheck`, `npm run lint` (`--max-warnings 0`), full `npm run test` (not a changed-files run: ~47 test files mock `@/api/hooks` by hand), `npm run build`, then `rm -rf web/dist web/test-results`.
- Server gates: `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, and the touched pytest files. Run pytest with the deploy venv: `/home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest …`, with `env -u VIBERPIT_TAILNET_ALLOWLIST -u VIBERPIT_SESSIOND_CERT -u VIBERPIT_SESSIOND_KEY -u VIBERPIT_SESSIOND_CA -u VIBERPIT_DAEMON_ROLE VIBERPIT_DEV=1` in front.
- Labels: the §3 tab word is `TIER_LABEL['needs-you']` (`'Needs you'`) from `web/src/features/products/vocab.ts` — never a string literal.
- Tab ids live in `AUTOMATION_TAB` in `vocab.ts`; the id for the new tab is `'needs-you'` (kebab, like the roadmap's).
- Copy: empty Needs you tab reads exactly `Nothing needs you.`; Overview preview button reads exactly `See all N` (N = list length).
- Commit messages go in a file and are passed with `-F`; end every commit with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Every test file that mounts `DeliveryPage` or `RoadmapPage` must wrap in `<ViewportProvider kiosk={null}>` after Task 5 (they call `useViewport()` from then on).

## Review Focus

1. `actionableCount` is `null` on the wire for an automation whose count is unknown (the fixture's Backlog Runner). The Needs you tab must render with no badge and no attention dot, not `NaN` or a crash — Task 2 pins this.
2. The Needs you tab must exist for an operator-triggered automation (PM classify, `modes: []`) even though Schedule is hidden for it — Task 2 pins this.
3. `status.counts` with more than six keys (gh-reconcile publishes eight) must show exactly the first six in server order, never a seventh — Task 3 pins this.
4. A `status.counts` value that is not a finite number (a defensive server `null` in a `Record<string, number>` typed field) must be skipped rather than rendered as `null` — Task 3 pins this.
5. The Back link must not appear on desktop, where the list column is still beside the page, and must not appear on the phone list view (nothing open) — Task 5 pins both.

---

### Task 1: `AutomationActions` grows `preview` and `full` variants

**Files:**
- Modify: `web/src/features/delivery/automation/actions.tsx` (the `AutomationActions` component at the bottom of the file, ~line 129)
- Test: `web/src/features/delivery/automation/actions.test.tsx`

**Interfaces:**
- Consumes: `useAutomationActionable(id, enabled)` from `@/api/hooks` (returns `{ data?: ActionableItem[] }`), `SectionHeading` from `@/ui/SectionHeading`, `Button` from `@/ui/Button` (variants include `'ghost'`, sizes include `'sm'`).
- Produces: `AutomationActions({ automationId, enabled, variant, onSeeAll })` with
  `variant: 'preview' | 'full'` and `onSeeAll?: () => void`. `'preview'`: heading "Actionable items", first `PREVIEW_ROWS` (3) rows, a ghost `See all N` button calling `onSeeAll` when the list is longer than three, renders `null` when empty. `'full'`: no heading, every row, no inner scroll box, and the sentence `Nothing needs you.` when empty. Exported constant `PREVIEW_ROWS = 3`.

- [ ] **Step 1: Write the failing tests**

Append to `web/src/features/delivery/automation/actions.test.tsx` (inside the existing `describe`), and change the existing two `render(<AutomationActions … />)` calls to pass `variant="full"`:

```tsx
  it('preview: three rows, a "See all N" button that hands off, and nothing at all when empty', () => {
    const onSeeAll = vi.fn()
    items = [1, 2, 3, 4, 5].map((n) => ({ id: `o/r#${n}`, title: `item ${n}`, actions: [] , status: 'waiting' }))
    render(<AutomationActions automationId="pr-doctor" enabled variant="preview" onSeeAll={onSeeAll} />)
    expect(screen.getByRole('heading', { name: 'Actionable items' })).toBeInTheDocument()
    expect(screen.getAllByRole('listitem')).toHaveLength(3)
    expect(screen.queryByText('item 4')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'See all 5' }))
    expect(onSeeAll).toHaveBeenCalledTimes(1)
  })

  it('preview: no "See all" when the list fits, and null when it is empty', () => {
    items = [{ id: 'o/r#1', title: 'only', actions: [], status: 'waiting' }]
    const { unmount } = render(<AutomationActions automationId="pr-doctor" enabled variant="preview" />)
    expect(screen.queryByRole('button', { name: /See all/ })).toBeNull()
    unmount()
    items = []
    const { container } = render(<AutomationActions automationId="pr-doctor" enabled variant="preview" />)
    expect(container).toBeEmptyDOMElement()
  })

  it('full: every row, no heading, no scroll box, and a sentence when empty', () => {
    items = [1, 2, 3, 4, 5].map((n) => ({ id: `o/r#${n}`, title: `item ${n}`, actions: [], status: 'waiting' }))
    const { unmount, container } = render(<AutomationActions automationId="pr-doctor" enabled variant="full" />)
    expect(screen.getAllByRole('listitem')).toHaveLength(5)
    expect(screen.queryByRole('heading')).toBeNull()
    expect(container.querySelector('.overflow-y-auto')).toBeNull()
    unmount()
    items = []
    render(<AutomationActions automationId="pr-doctor" enabled variant="full" />)
    expect(screen.getByText('Nothing needs you.')).toBeInTheDocument()
  })
```

Add `fireEvent` to the testing-library import at the top of the test file.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `(cd web && npx vitest run src/features/delivery/automation/actions.test.tsx)`
Expected: FAIL — TypeScript/vitest complains `variant` is not a prop, and the preview/full assertions fail.

- [ ] **Step 3: Implement the variants**

Replace the `AutomationActions` function at the bottom of `actions.tsx` with:

```tsx
/** Rows the Overview preview shows before handing off to the Needs you tab
 *  (docs/PAGE-TABS.md §5: a preview is at most three rows + "See all N"). */
export const PREVIEW_ROWS = 3

export function AutomationActions({ automationId, enabled, variant, onSeeAll }: {
  automationId: string
  enabled: boolean
  /** `preview`: the Overview's notice — heading, three rows, "See all N",
   *  nothing at all when empty. `full`: the Needs you tab — every row on the
   *  page scroll, a sentence when empty. */
  variant: 'preview' | 'full'
  /** Preview only: switch to the tab that owns the full list. */
  onSeeAll?: () => void
}) {
  const actionable = useAutomationActionable(automationId, enabled)
  const list = actionable.data ?? []
  if (list.length === 0) {
    return variant === 'full' ? <p className="text-sm text-ink-muted">Nothing needs you.</p> : null
  }
  const rows = variant === 'preview' ? list.slice(0, PREVIEW_ROWS) : list
  const items = (
    <ul className="space-y-1.5 text-sm">
      {rows.map((item) => (
        <li key={item.id} className="flex items-center gap-2">
          <span className="flex-1 truncate">
            <span className="text-ink-muted tabular-nums">{item.id.split('#')[1] ? `#${item.id.split('#')[1]}` : ''}</span>{' '}
            {item.title}
          </span>
          {item.actions.length === 0 && item.status && (
            <span className="text-xs text-ink-muted">{item.status}</span>
          )}
          {item.actions.map((action) => (
            <ActionButton key={action.kind} item={item} action={action} automationId={automationId} />
          ))}
        </li>
      ))}
    </ul>
  )
  if (variant === 'full') return items
  return (
    <section className="space-y-2">
      <SectionHeading>Actionable items</SectionHeading>
      <div className="rounded-md border border-edge p-2">{items}</div>
      {list.length > PREVIEW_ROWS && (
        <div>
          <Button size="sm" variant="ghost" onClick={onSeeAll}>See all {list.length}</Button>
        </div>
      )}
    </section>
  )
}
```

Update the file's header comment: it is now "the actionable-items list: the Overview's preview and the Needs you tab's full list".

- [ ] **Step 4: Run the tests to verify they pass**

Run: `(cd web && npx vitest run src/features/delivery/automation/actions.test.tsx)`
Expected: PASS (5 tests). `AutomationDetail.test.tsx` and `DeliveryPage.test.tsx` will fail to typecheck until Task 2 passes `variant` — that is expected here.

- [ ] **Step 5: Commit**

```bash
git add web/src/features/delivery/automation/actions.tsx web/src/features/delivery/automation/actions.test.tsx
printf 'feat(automations): AutomationActions preview/full variants for the Needs you tab\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c1.txt && git commit -F /tmp/c1.txt
```

---

### Task 2: Needs you tab

**Files:**
- Modify: `web/src/features/products/vocab.ts` (`AUTOMATION_TAB`, ~line 183)
- Modify: `web/src/features/delivery/automation/AutomationDetail.tsx`
- Modify: `docs/PAGE-TABS.md` (§8 table)
- Modify: `web/e2e/delivery.spec.ts` (the `registry mode effects…` test, ~line 192)
- Test: `web/src/features/delivery/automation/AutomationDetail.test.tsx`

**Interfaces:**
- Consumes: `AutomationActions` from Task 1 (`variant`, `onSeeAll`), `TIER_LABEL['needs-you']`, `Automation.actionableCount?: number | null`.
- Produces: `AUTOMATION_TAB` keyed `overview | 'needs-you' | queue | schedule | config | events | sessions` in that order; the tab strip item `{ id: 'needs-you', label: 'Needs you', count, attention }`.

- [ ] **Step 1: Write the failing tests**

In `AutomationDetail.test.tsx`, replace the first test (`'Overview: description, oversight and actionable items — no queue surface'`) with these three, and add the fourth:

```tsx
  it('Overview: description, oversight and a three-row actionable preview — no queue surface', async () => {
    mount()
    expect(screen.getByRole('tab', { name: 'Overview', selected: true })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Actionable items' })).toBeInTheDocument()
    expect(screen.getByText('CI red on #1')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Fix' }))
    await waitFor(() => expect(trigger).toHaveBeenCalledWith({ item_id: 'o/r#1', kind: 'fix' }))
    // The consolidation panel is the Queue tab's, not the Overview's.
    expect(screen.queryByTestId('consolidate')).not.toBeInTheDocument()
  })
  it('Needs you: every automation has the tab; the badge is actionableCount with attention above zero', () => {
    // pr-doctor fixture: actionableCount 0 → bare label; runner: 2 → count + attention; pm-classify: 0.
    mount()
    expect(screen.getByRole('tab', { name: 'Needs you' })).toBeInTheDocument()
    mount(AUTOMATIONS[1], '/delivery/automations/backlog-runner')
    expect(screen.getByRole('tab', { name: 'Needs you (2, needs attention)' })).toBeInTheDocument()
    // Review focus 2: operator-triggered still has the question.
    mount(AUTOMATIONS[2], '/delivery/automations/pm-classify')
    expect(screen.getAllByRole('tab', { name: 'Needs you' }).length).toBeGreaterThan(0)
  })
  it('Needs you: an unknown actionableCount is a bare tab, not NaN', () => {
    // Review focus 1.
    mount({ ...prDoctor, actionableCount: null }, '/delivery/automations/pr-doctor')
    expect(screen.getByRole('tab', { name: 'Needs you' })).toBeInTheDocument()
    expect(screen.queryByRole('tab', { name: /NaN/ })).toBeNull()
  })
  it('Needs you: ?tab=needs-you selects it and shows the full list; the order is Overview · Needs you · Queue …', () => {
    mount(prDoctor, '/delivery/automations/pr-doctor?tab=needs-you')
    expect(screen.getByRole('tab', { name: 'Needs you', selected: true })).toBeInTheDocument()
    expect(screen.getByText('CI red on #1')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Actionable items' })).toBeNull()
    const names = screen.getAllByRole('tab').map((t) => t.getAttribute('aria-label'))
    expect(names.slice(0, 3)).toEqual(['Overview', 'Needs you', 'Queue'])
  })
```

Also add to the existing `'Queue: the runner’s products table, groups panel and custom dispatch'` test nothing; leave it.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `(cd web && npx vitest run src/features/delivery/automation/AutomationDetail.test.tsx)`
Expected: FAIL — no tab named "Needs you"; `AutomationActions` missing `variant` (type error surfaces in vitest as a runtime render of the old component).

- [ ] **Step 3: Add the tab id and label**

In `vocab.ts`, change `AUTOMATION_TAB` to:

```ts
export const AUTOMATION_TAB = {
  overview: TAB_LABEL.overview, 'needs-you': TIER_LABEL['needs-you'], queue: TAB_LABEL.queue,
  schedule: TAB_LABEL.schedule, config: TAB_LABEL.config, events: TAB_LABEL.events,
  sessions: TAB_LABEL.sessions,
} as const
```

and extend its doc comment: "`needs-you` is offered to every automation (the question exists for all of them; §4)". `TIER_LABEL` is declared above `TAB_LABEL` in the same file, so no import is needed.

- [ ] **Step 4: Wire the tab in `AutomationDetail.tsx`**

In `AutomationDetail`, build the strip items with the badge:

```tsx
      <Tabs
        label="Automation"
        items={items.map((id) => ({
          id,
          label: AUTOMATION_TAB[id],
          count: id === 'sessions' ? sessions.data?.length
            : id === 'needs-you' ? a.actionableCount ?? undefined
            : undefined,
          attention: id === 'needs-you' && (a.actionableCount ?? 0) > 0,
        }))}
```

Replace the Overview's `<AutomationActions automationId={a.id} enabled />` with:

```tsx
            {/* The Overview's "needs you" notice (standard §5): a three-row
                preview that renders nothing at zero and hands off to the tab. */}
            <AutomationActions automationId={a.id} enabled variant="preview" onSeeAll={() => setTab('needs-you')} />
```

Add the panel branch after the Overview branch:

```tsx
        {tab === 'needs-you' && <AutomationActions automationId={a.id} enabled variant="full" />}
```

Update the file's header comment to list seven tabs: "Overview · Needs you · Queue · Schedule · Config · Events · Sessions".

- [ ] **Step 5: Run the tests to verify they pass**

Run: `(cd web && npx vitest run src/features/delivery)`
Expected: PASS. If `DeliveryPage.test.tsx`'s `'an open automation names itself…'` test fails on tab order, it only asserts Overview is selected — it should still pass.

- [ ] **Step 6: Record the exception and extend the e2e spec**

In `docs/PAGE-TABS.md` §8 table, add a row after "Product Capabilities":

```
| Automation with a Queue (Backlog Runner, PR Doctor) | 7th tab past the 6-tab cap (§4): Needs you (decisions) and Queue (dispatchable work) are different questions, and Schedule stays its own tab while its editor saves as a form beside Config's live-patching fields. | when the schedule editor patches live — fold Schedule into Config then |
```

In `web/e2e/delivery.spec.ts`, inside the `registry mode effects and relationships explain the workflow` test, after the `Execute and finish work` assertion add:

```ts
  // Every automation has a Needs you tab; the intercept answers [] for the
  // actionable list, so the tab is a sentence, and the badge is the payload's
  // actionableCount (2 on the fixture's PR doctor).
  await page.getByRole('tab', { name: 'Needs you (2, needs attention)' }).click()
  await expect(page).toHaveURL(/tab=needs-you$/)
  await expect(page.getByText('Nothing needs you.')).toBeVisible()
  await page.getByRole('tab', { name: 'Overview' }).click()
```

Run: `(cd web && npm run build >/dev/null 2>&1 && npx playwright test e2e/delivery.spec.ts --reporter=line 2>&1 | grep -E "passed|failed" ; rm -rf dist test-results)`
Expected: `3 passed`.

- [ ] **Step 7: Commit**

```bash
git add web/src/features/products/vocab.ts web/src/features/delivery/automation/AutomationDetail.tsx web/src/features/delivery/automation/AutomationDetail.test.tsx docs/PAGE-TABS.md web/e2e/delivery.spec.ts
printf 'feat(automations): Needs you tab on every automation, three-row Overview preview\n\nRecorded in docs/PAGE-TABS.md §8 as the 7-tab exception for the two automations with a Queue.\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c2.txt && git commit -F /tmp/c2.txt
```

---

### Task 3: Stat tiles from `status.counts`

**Files:**
- Move: `web/src/features/roadmap/detail/StatTiles.tsx` → `web/src/ui/StatTiles.tsx` (`git mv`), update the four importers: `web/src/features/roadmap/InitiativePage.tsx:40`, `web/src/features/roadmap/detail/CapabilityDetail.tsx:19`, `web/src/features/roadmap/detail/ProductDetail.tsx:26`, `web/src/features/roadmap/detail/PortfolioDetail.tsx:8` → `import { StatTiles } from '@/ui/StatTiles'`.
- Create: `web/src/features/delivery/automation/countTiles.ts`
- Create: `web/src/features/delivery/automation/countTiles.test.ts`
- Modify: `web/src/features/delivery/automation/AutomationDetail.tsx`
- Test: `web/src/features/delivery/automation/AutomationDetail.test.tsx`

**Interfaces:**
- Consumes: `StatTiles({ tiles: StatTile[] })` where `StatTile = { label: string; value: number; tone?: Tone; onClick?: () => void }`; `AutomationStatus.counts: Record<string, number> | null`.
- Produces: `countTiles(counts: Record<string, number> | null | undefined): StatTile[]` and `humanizeKey(key: string): string` in `countTiles.ts`; `MAX_COUNT_TILES = 6`.

- [ ] **Step 1: Write the failing helper tests**

`web/src/features/delivery/automation/countTiles.test.ts`:

```ts
import { describe, it, expect } from 'vitest'
import { countTiles, humanizeKey, MAX_COUNT_TILES } from './countTiles'

describe('countTiles', () => {
  it('humanizes snake_case keys into a capitalized phrase', () => {
    expect(humanizeKey('needs_human')).toBe('Needs human')
    expect(humanizeKey('gh_projects')).toBe('Gh projects')
    expect(humanizeKey('failing')).toBe('Failing')
  })
  it('keeps the server order and caps at six tiles', () => {
    // Review focus 3: gh-reconcile publishes eight keys.
    const counts = { products: 6, planned: 1, applied: 1, failed: 0, parked: 0, skipped_stale: 2, halted_cap: 0, dry_run: 0 }
    const tiles = countTiles(counts)
    expect(tiles).toHaveLength(MAX_COUNT_TILES)
    expect(tiles.map((t) => t.label)).toEqual(['Products', 'Planned', 'Applied', 'Failed', 'Parked', 'Skipped stale'])
    expect(tiles[0]).toEqual({ label: 'Products', value: 6 })
  })
  it('skips non-finite values and answers nothing for null or empty counts', () => {
    // Review focus 4.
    expect(countTiles({ ok: 1, bad: null as unknown as number, nan: Number.NaN })).toEqual([{ label: 'Ok', value: 1 }])
    expect(countTiles(null)).toEqual([])
    expect(countTiles(undefined)).toEqual([])
    expect(countTiles({})).toEqual([])
  })
})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `(cd web && npx vitest run src/features/delivery/automation/countTiles.test.ts)`
Expected: FAIL — module not found.

- [ ] **Step 3: Move `StatTiles` and write the helper**

```bash
git mv web/src/features/roadmap/detail/StatTiles.tsx web/src/ui/StatTiles.tsx
```

Edit the four importers listed above to `import { StatTiles } from '@/ui/StatTiles'`. In `web/src/ui/StatTiles.tsx` change `import { TONE_TEXT, type Tone } from '@/ui/Badge'` to `from './Badge'` and update the header comment to say it is shared by the roadmap details and the automation detail.

`web/src/features/delivery/automation/countTiles.ts`:

```ts
import type { StatTile } from '@/ui/StatTiles'

/** Tiles above the automation's tab strip (docs/PAGE-TABS.md §6): six is the
 *  grid's two full rows on a phone, and past six the band stops being a
 *  glance. The server's key order is kept — it is the automation's own
 *  ordering of what matters. */
export const MAX_COUNT_TILES = 6

/** `needs_human` → "Needs human". The server's count keys are snake_case
 *  identifiers; this is the one place they become words. */
export function humanizeKey(key: string): string {
  const words = key.replaceAll('_', ' ').trim()
  return words.charAt(0).toUpperCase() + words.slice(1)
}

/** `status.counts` as stat tiles: untoned and unclickable, because the counts
 *  do not map to tabs and a tile that navigates nowhere must not look like one
 *  that does. A value that is not a finite number is skipped, not shown. */
export function countTiles(counts: Record<string, number> | null | undefined): StatTile[] {
  if (!counts) return []
  return Object.entries(counts)
    .filter(([, value]) => typeof value === 'number' && Number.isFinite(value))
    .slice(0, MAX_COUNT_TILES)
    .map(([key, value]) => ({ label: humanizeKey(key), value }))
}
```

- [ ] **Step 4: Run the helper tests to verify they pass**

Run: `(cd web && npx vitest run src/features/delivery/automation/countTiles.test.ts src/features/roadmap)`
Expected: PASS (roadmap suites still green after the move).

- [ ] **Step 5: Write the failing detail test**

Add to `AutomationDetail.test.tsx`:

```tsx
  it('renders status.counts as stat tiles above the strip, and no band when counts is null', () => {
    const runner = { ...AUTOMATIONS[1], status: { ...AUTOMATIONS[1].status, counts: { in_flight: 2, eligible: 14, blocked: 63 } } }
    const { unmount } = mount(runner, '/delivery/automations/backlog-runner')
    const band = screen.getByRole('term', { name: 'In flight' }).closest('dl')!
    expect(band.compareDocumentPosition(screen.getByRole('tablist'))).toBe(Node.DOCUMENT_POSITION_FOLLOWING)
    expect(screen.getByText('14')).toBeInTheDocument()
    unmount()
    mount(prDoctor) // counts: null
    expect(document.querySelector('dl')).toBeNull()
  })
```

(`mount` must return the render result: change it to `const r = render(…); return { ...r, onOpenSession }`.)

- [ ] **Step 6: Render the tiles**

In `AutomationDetail.tsx` add `import { StatTiles } from '@/ui/StatTiles'` and `import { countTiles } from './countTiles'`, compute `const tiles = countTiles(a.status.counts)` and render, immediately before `<Tabs`:

```tsx
      {/* Above the strip (standard §6): the automation's own counts, visible on
          every tab. The runner's used to be spelled out in status.text. */}
      {tiles.length > 0 && <div className="mb-4"><StatTiles tiles={tiles} /></div>}
```

- [ ] **Step 7: Run the tests, typecheck, and commit**

Run: `(cd web && npx vitest run src/features/delivery src/features/roadmap && npm run typecheck && npm run lint)`
Expected: all PASS, lint clean. (`getByRole('term', …)` — `dt` has the `term` role; if the accessible-name lookup is flaky, use `screen.getByText('In flight').closest('dl')` instead.)

```bash
git add -A web/src/ui/StatTiles.tsx web/src/features/roadmap web/src/features/delivery
printf 'feat(automations): status.counts as stat tiles above the strip; StatTiles moves to ui/\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c3.txt && git commit -F /tmp/c3.txt
```

---

### Task 4: Server — the runner's status text stops spelling out the counts

**Files:**
- Modify: `server/automations/backlog_runner.py:409-416` (the `text = (…)` expression in `status()`)
- Test: `server/tests/test_backlog_runner_automation.py:224-229`, `server/tests/test_backlog_runner_loop.py` (assertions at ~4866, ~4905, ~4924 keep passing unchanged — they assert `"N work in flight" in text`), `server/tests/test_automations_api.py:74` (unchanged), `server/tests/test_backlog_runner_e2e.py:594` (unchanged).

**Interfaces:**
- Produces: `AutomationStatus.text` for the runner = `"{n} work in flight"` + (`" · queue counts unavailable"` when `"eligible"` not in counts) + existing `" · {k} unattributed"` + existing product segments + existing pick. `counts` is unchanged and still carries `eligible`, `blocked`, `untriaged`, `needs_human`.

- [ ] **Step 1: Update the pinned test and add the counts guarantee**

In `test_backlog_runner_automation.py`, change `test_status_without_the_products_cache_is_unchanged` to:

```python
def test_status_without_the_products_cache_is_unchanged():
    """A tick that never reached the count seam leaves no ``backlog_products``;
    the card must keep rendering the summary rather than claiming nothing is on.
    The counts themselves are tiles on the web (spec 2026-09-28 §2), so the text
    names only the in-flight number and keeps every count in ``counts``."""
    from types import SimpleNamespace

    from server.automations.backlog_runner import BacklogRunner
    ctx = SimpleNamespace(backlog_counts={"in_flight": 1, "eligible": 2, "blocked": 0})
    status = BacklogRunner().status(ctx)
    assert status.text == "1 work in flight"
    assert status.counts == {"in_flight": 1, "eligible": 2, "blocked": 0}


def test_status_names_missing_queue_counts_rather_than_hiding_them():
    from types import SimpleNamespace

    from server.automations.backlog_runner import BacklogRunner
    ctx = SimpleNamespace(backlog_counts={"in_flight": 1, "needs_human": 3})
    assert BacklogRunner().status(ctx).text == "1 work in flight · queue counts unavailable"
```

- [ ] **Step 2: Run to verify the first fails**

Run: `env -u VIBERPIT_TAILNET_ALLOWLIST -u VIBERPIT_SESSIOND_CERT -u VIBERPIT_SESSIOND_KEY -u VIBERPIT_SESSIOND_CA -u VIBERPIT_DAEMON_ROLE VIBERPIT_DEV=1 /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest server/tests/test_backlog_runner_automation.py -q -k "products_cache or missing_queue"`
Expected: 2 failed (old text `"1 work in flight, 2 eligible"`; `", "` vs `" · "`).

- [ ] **Step 3: Trim the text**

Replace the `text = (…)` expression (the one building `"{n} work in flight, "` + eligible/blocked/untriaged/needs-human) with:

```python
        # The fleet counts (eligible, blocked, untriaged, needs-human) are NOT
        # spelled out here any more: the web renders ``counts`` as stat tiles
        # above the tab strip (spec 2026-09-28 §2), and a fixed header that
        # repeats them was the audit's finding #2. Only the absence of the
        # queue read is still a sentence, because a missing tile is invisible.
        text = f"{n} work in flight"
        if "eligible" not in counts:
            text += " · queue counts unavailable"
```

Keep the comment block about `untriaged` above it only if it still reads true; otherwise delete it (it explains a clause that no longer exists — leave no dead prose).

- [ ] **Step 4: Run the server tests that read the status**

Run: `env -u VIBERPIT_TAILNET_ALLOWLIST -u VIBERPIT_SESSIOND_CERT -u VIBERPIT_SESSIOND_KEY -u VIBERPIT_SESSIOND_CA -u VIBERPIT_DAEMON_ROLE VIBERPIT_DEV=1 /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest server/tests/test_backlog_runner_automation.py server/tests/test_automations_api.py server/tests/test_backlog_runner_e2e.py server/tests/test_backlog_runner_loop.py server/tests/test_backlog_count_in_flight.py -q -x 2>&1 | tail -5`
Expected: all pass. Then `ruff check src/ server/` and `mypy server/ --ignore-missing-imports` (use the deploy venv's `ruff`/`mypy` binaries under `/home/tony/GitHub/viberpit-deploy/.venv/bin/`).

- [ ] **Step 5: Commit**

```bash
git add server/automations/backlog_runner.py server/tests/test_backlog_runner_automation.py
printf 'feat(backlog): runner status text names only the in-flight count; the rest are web tiles\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c4.txt && git commit -F /tmp/c4.txt
```

---

### Task 5: Back link below desktop on the Delivery and Roadmap detail pages

**Files:**
- Modify: `web/src/features/delivery/DeliveryPage.tsx` (the `<PageHeader` call, ~line 77)
- Modify: `web/src/features/roadmap/RoadmapPage.tsx` (the `<PageHeader` call, ~line 232)
- Test: `web/src/features/delivery/DeliveryPage.test.tsx`, `web/src/features/roadmap/RoadmapPage.test.tsx`

**Interfaces:**
- Consumes: `useViewport()` from `@/shell/useViewport` (`{ mode: 'phone' | 'tablet' | 'desktop' | 'kiosk' }`), `PageHeader.back?: { to: string; label: string }`, `deliveryPath(scope)`, `roadmapPath('map')`, `DELIVERY.automations | DELIVERY.ci`, `ROADMAP.map`; in tests `ViewportProvider` from `@/shell/useViewport` and `stubMatchMedia`, `LAYOUT_MODE_QUERIES` from `@/test/matchMedia`.
- Produces: nothing new for later tasks.

- [ ] **Step 1: Wrap both page test harnesses in `ViewportProvider`**

In `DeliveryPage.test.tsx`, in both `mount` and `mountWithNav`, wrap the `RouterProvider` in `<ViewportProvider kiosk={null}>…</ViewportProvider>` (import from `@/shell/useViewport`). In `RoadmapPage.test.tsx`, wrap the `MemoryRouter` in `mount` (line 105) and in the `rerender` at line 273 the same way. Add at the top of each file `import { LAYOUT_MODE_QUERIES, stubMatchMedia } from '@/test/matchMedia'` and a `beforeEach(() => stubMatchMedia(LAYOUT_MODE_QUERIES.desktop))` so every existing test runs at desktop (the shape they were written for; without `matchMedia` the provider defaults to phone).

Run: `(cd web && npx vitest run src/features/delivery/DeliveryPage.test.tsx src/features/roadmap/RoadmapPage.test.tsx)`
Expected: PASS (no behaviour change yet).

- [ ] **Step 2: Write the failing tests**

`DeliveryPage.test.tsx`:

```tsx
  it('below desktop an open automation gets a Back link to its list scope; desktop and the bare list do not', () => {
    // Review focus 5.
    stubMatchMedia(LAYOUT_MODE_QUERIES.phone)
    mount('/delivery/automations/pr-doctor')
    expect(screen.getByRole('link', { name: 'Back to Automations' })).toHaveAttribute('href', '/delivery')
    cleanup()
    mount('/delivery/ci/o/r')
    expect(screen.getByRole('link', { name: 'Back to CI health' })).toHaveAttribute('href', '/delivery?scope=ci')
    cleanup()
    mount('/delivery')
    expect(screen.queryByRole('link', { name: /^Back to/ })).toBeNull()
    cleanup()
    stubMatchMedia(LAYOUT_MODE_QUERIES.desktop)
    mount('/delivery/automations/pr-doctor')
    expect(screen.queryByRole('link', { name: /^Back to/ })).toBeNull()
  })
```

(`cleanup` from `@testing-library/react`.)

`RoadmapPage.test.tsx`:

```tsx
  it('below desktop an open product or capability gets a Back link to the map; desktop does not', () => {
    stubMatchMedia(LAYOUT_MODE_QUERIES.tablet)
    mount('/roadmap/kindash')
    expect(screen.getByRole('link', { name: 'Back to Map' })).toHaveAttribute('href', '/roadmap')
    cleanup()
    stubMatchMedia(LAYOUT_MODE_QUERIES.desktop)
    mount('/roadmap/kindash')
    expect(screen.queryByRole('link', { name: 'Back to Map' })).toBeNull()
  })
```

(Use whatever product slug the file's `MAP` fixture has for its first product — read the fixture; `kindash` is a guess to replace.)

- [ ] **Step 3: Run to verify they fail**

Run: `(cd web && npx vitest run src/features/delivery/DeliveryPage.test.tsx src/features/roadmap/RoadmapPage.test.tsx)`
Expected: the two new tests FAIL (no Back link).

- [ ] **Step 4: Pass `back` below desktop**

`DeliveryPage.tsx`: `import { useViewport } from '@/shell/useViewport'`, then inside the component:

```tsx
  // Below desktop the list column hides behind an open item (`app.list` in the
  // shell's matrix), so the page carries the way back to the scope's list. On
  // desktop the list is beside the page and a Back link would be a second
  // copy of it. Never from kiosk: this section is `showInKiosk: false`.
  const { mode } = useViewport()
  const back = item && mode !== 'desktop'
    ? {
      to: deliveryPath(item.kind === 'ciRepo' ? 'ci' : 'automations'),
      label: `Back to ${item.kind === 'ciRepo' ? DELIVERY.ci : DELIVERY.automations}`,
    }
    : undefined
```

and pass `back={back}` to `<PageHeader`.

`RoadmapPage.tsx` (`RoadmapPageContent`): same import, and

```tsx
  const { mode } = useViewport()
  const back = opened && mode !== 'desktop'
    ? { to: roadmapPath('map'), label: `Back to ${ROADMAP.map}` }
    : undefined
```

passed as `back={back}`. (`ROADMAP` from `@/features/products/vocab`; `roadmapPath` is already imported in the file — check, and add if not.)

- [ ] **Step 5: Run the tests, then the shell tests (the AppShell renders these pages), and commit**

Run: `(cd web && npx vitest run src/features/delivery src/features/roadmap src/shell && npm run typecheck)`
Expected: PASS. If any other test file mounts `DeliveryPage`/`RoadmapPage` without a provider it will throw `useViewport must be used within a ViewportProvider` — wrap it the same way.

```bash
git add web/src/features/delivery/DeliveryPage.tsx web/src/features/delivery/DeliveryPage.test.tsx web/src/features/roadmap/RoadmapPage.tsx web/src/features/roadmap/RoadmapPage.test.tsx
printf 'feat(shell): Back link on Delivery and Roadmap detail pages below desktop\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c5.txt && git commit -F /tmp/c5.txt
```

---

### Task 6: Session status labels in the Sessions tab

**Files:**
- Modify: `web/src/features/delivery/automation/SessionsList.tsx`
- Test: `web/src/features/delivery/automation/AutomationDetail.test.tsx`

**Interfaces:**
- Consumes: `statusPresentation(state: string): StatusPresentation` (`.label`) from `@/session/statusTier`.

- [ ] **Step 1: Write the failing test**

In `AutomationDetail.test.tsx`'s `'Sessions tab links live sessions…'` test add:

```tsx
    // The status is the app's label for it, not the wire key (spec 2026-09-28 §4).
    expect(screen.getByText('· Working…')).toBeInTheDocument()
    expect(screen.getByText('· Ended')).toBeInTheDocument()
    expect(screen.queryByText('· in_progress')).toBeNull()
```

- [ ] **Step 2: Run to verify it fails**

Run: `(cd web && npx vitest run src/features/delivery/automation/AutomationDetail.test.tsx -t "Sessions tab")`
Expected: FAIL — `· in_progress` is rendered.

- [ ] **Step 3: Use the presentation label**

In `SessionsList.tsx` add `import { statusPresentation } from '@/session/statusTier'` and change `<span className="text-ink-muted">· {s.status}</span>` to `<span className="text-ink-muted">· {statusPresentation(s.status).label}</span>`.

- [ ] **Step 4: Run and commit**

Run: `(cd web && npx vitest run src/features/delivery/automation/AutomationDetail.test.tsx)`
Expected: PASS.

```bash
git add web/src/features/delivery/automation/SessionsList.tsx web/src/features/delivery/automation/AutomationDetail.test.tsx
printf 'fix(automations): Sessions tab speaks the session status labels, not wire keys\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c6.txt && git commit -F /tmp/c6.txt
```

---

### Task 7: Gates, visual check, PR

**Files:** none new.

- [ ] **Step 1: Web gates**

Run: `(cd web && npm run typecheck && npm run lint && npm run test 2>&1 | tail -6 && npm run build >/dev/null 2>&1 && npx playwright test e2e/delivery.spec.ts --reporter=line 2>&1 | grep -E "passed|failed"; rm -rf dist test-results)`
Expected: typecheck clean, lint clean, all vitest files pass, `3 passed` for the e2e spec.

- [ ] **Step 2: Server gates**

Run: `/home/tony/GitHub/viberpit-deploy/.venv/bin/ruff check src/ server/ && /home/tony/GitHub/viberpit-deploy/.venv/bin/mypy server/ --ignore-missing-imports 2>&1 | tail -2` and the Task 4 pytest command.
Expected: clean.

- [ ] **Step 3: Visual check at phone and desktop**

Start `(cd web && setsid nohup npx vite --port 5199 --strictPort > /tmp/vite.log 2>&1 &)`, wait for `curl -s -o /dev/null http://127.0.0.1:5199/delivery`, and with a throwaway Playwright script in `web/` (see memory `screenshot-branch-against-live-loopback-api`) screenshot `/delivery/automations/backlog-runner` at 375 and 1100 wide and `/delivery/automations/backlog-runner?tab=needs-you` at 375. Confirm: tiles above the strip, "Needs you" second in the strip with a count, a Back link on the phone header. Stop vite with `kill $(pgrep -f 'vite --port 51[9]9')`, delete the script.

- [ ] **Step 4: Push and open the PR**

Write the body to `/tmp/pr-body.md` (summary of the four pieces, the §8 exception, the server text change, verification lines, ending with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`), then:

```bash
git push -u origin feat/automations-needs-you-tiles-back
gh pr create --title "feat(automations): Needs you tab, stat tiles from status.counts, phone Back link, session status labels" --body-file /tmp/pr-body.md
gh pr merge <number> --squash --auto
```

Confirm `isDraft: false` and `autoMergeRequest` set via `gh pr view <number> --json isDraft,autoMergeRequest`.
