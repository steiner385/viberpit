# Capacity settings PR 2 — readout depth and links

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every surface that shows capacity says *why* and links to the page that owns the dial: the Delivery → Capacity list column links its rows, the Global row shows its two ceilings, the Accounts row expands to the per-account reasons already on the wire, the intent row surfaces the three silent ways the governor can stop governing, Hosts and Accounts carry the same live-readout card Limits has, queued and capacity-stalled sessions link to Capacity, and a binding constraint lights the Delivery nav badge.

**Architecture:** Almost entirely web. One additive server field (`reservePctByLevel`) replaces the client-side reserve mirror. `capacityStack.ts` stays the single place stack rows are derived (it gains the Global secondary line and a per-account line helper). A new `LiveReadoutCard` chassis component is mounted by `HostedSection` through a new `readout` slot on `settingsDefs`, so Hosts and Accounts get the card without touching `FleetSection`/`AccountsManager` (whose tests hand-mock `@/api/hooks` without a spread). A pure `governorNotice()` helper decides the intent-row banner.

**Tech Stack:** Python FastAPI (one dict key) · React 18 + TypeScript + React Query + react-router · vitest + testing-library + jest-axe · pytest.

**Spec:** `docs/superpowers/specs/2026-09-20-capacity-settings-structure-design.md` — this plan is §8 **PR 2**; it implements D10 (the rest of it) and §5.2 items 1–2, and closes C3, C6, C8, C18, C21, C22. Read §5.2 and §6 before starting. PR 1 (#2080) is merged: `global.{physical,attention,binding}` is on the wire, Limits exists (`LimitsSection.tsx`), `settingsSectionPath` is used by the stack.

## Global Constraints

- **Vocabulary (spec D8):** Global cap · Physical ceiling · Attention ceiling · Host cap · Runner ceiling · Pin · Rank · Push level · Reserve. No new terms.
- **No schema change.** The one server change is an additive JSON key on `GET /api/capacity`.
- **Links go through helpers (D10):** `settingsSectionPath(id)` from `@/shell/navDefs` and `deliveryPath('capacity')` / `automationPath(id)` from `@/features/delivery/deliveryNavState`. Never a hard-coded `/settings/...` or `/delivery...` string in component code.
- **No new polling timers.** `useCapacity({ poll: true })` is owned by `CapacityDetail` only; every other reader uses `useCapacity()` (poll false) and shares the cache.
- **Save contract unchanged:** PR 2 adds no editable field.
- **Test commands (repo root; never leave the shell in another directory — the Bash hook is cwd-relative):**
  - Server, one file: `mkdir -p /tmp/viberpit-test-home && env -i HOME=/tmp/viberpit-test-home PATH="$PATH" CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest server/tests/<file>.py -q`
  - Web (link once per worktree): `[ -e web/node_modules ] || ln -s /home/tony/GitHub/viberpit-deploy/web/node_modules web/node_modules`
  - Web, one file: `(cd web && npx vitest run src/<path>.test.tsx)`
  - Final gate (Task 8): `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/`, `pytest server/tests/ -n auto --dist loadfile`, and in `web/`: `npm run typecheck`, `npm run lint`, `npm run test`, `npm run build`.
- **Commits:** message via `-F` file, ending with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

---

## File structure

| File | Responsibility |
|---|---|
| `server/automations/capacity_readout.py` | `reservePctByLevel` on the readout (from `RESERVE_PCT`). |
| `web/src/api/capacity.ts` | `CapacityReadout.reservePctByLevel`. |
| `web/src/features/delivery/capacity/capacityStack.ts` | Global row `secondary`; Products row `href: '#products'`; `accountRowLine(row)`. |
| `web/src/features/delivery/capacity/governorNotice.ts` (new) | Pure: `governorNotice(readout) → { tone, title, body } \| null` (C18). |
| `web/src/features/delivery/capacity/CapacityScopeBody.tsx` | Rows link (C3). |
| `web/src/features/delivery/capacity/CapacityDetail.tsx` | Push-level descriptions from the wire; governor notice; Accounts expansion (C21); `#products` anchor. |
| `web/src/features/settings/chassis/LiveReadoutCard.tsx` (new) | The "Live readout" group: stack rows, optional highlighted row, binding banner, link. |
| `web/src/features/settings/settingsDefs.ts`, `HostedSection.tsx` | `readout?: 'hosts' \| 'accounts'` slot; Hosts and Accounts opt in. |
| `web/src/features/settings/sections/LimitsSection.tsx` | Uses `LiveReadoutCard`; its inline copy goes. |
| `web/src/features/sessions/list/QueuedSection.tsx`, `SessionCard.tsx` | "See capacity" links (C6). |
| `web/src/shell/useNavBadges.ts` | Delivery badge lights on a binding constraint (C8). |

---

### Task 1: `reservePctByLevel` on the wire; delete the client mirror

**Files:**
- Modify: `server/automations/capacity_readout.py` (the return dict in `build_readout`)
- Modify: `web/src/api/capacity.ts`, `web/src/features/delivery/capacity/__fixtures__/capacity.ts`, `web/src/features/delivery/capacity/capacityStack.test.ts` (the `readout()` helper), `web/src/features/delivery/capacity/CapacityDetail.tsx:49-57`
- Test: `server/tests/test_capacity_readout.py`, `web/src/features/delivery/capacity/CapacityDetail.test.tsx`

**Interfaces:**
- Produces: `GET /api/capacity` → `reservePctByLevel: { conserve: 40, steady: 20, max: 5 }` (from `RESERVE_PCT`); `CapacityReadout.reservePctByLevel: Record<PushLevel, number>`.

- [ ] **Step 1: Failing tests**

Append to `server/tests/test_capacity_readout.py`:

```python
def test_readout_carries_the_reserve_for_every_push_level():
    """spec 2026-09-20 C22: the web no longer mirrors RESERVE_PCT."""
    from server.automations.capacity_plan import RESERVE_PCT

    r = asyncio.run(build_readout(_make_sources()))
    assert r["reservePctByLevel"] == dict(RESERVE_PCT)
    assert set(r["reservePctByLevel"]) == {"conserve", "steady", "max"}
```

In `web/src/features/delivery/capacity/CapacityDetail.test.tsx`, add inside the main `describe` (it uses `setup()` and the `CAPACITY` fixture):

```ts
  it('push-level descriptions come from the wire, not a client constant', () => {
    setup({ readout: { ...CAPACITY, reservePctByLevel: { conserve: 45, steady: 25, max: 7 } } })
    expect(screen.getByText('45% weekly reserve')).toBeInTheDocument()
    expect(screen.getByText('25% weekly reserve')).toBeInTheDocument()
    expect(screen.getByText('7% weekly reserve')).toBeInTheDocument()
  })
```

- [ ] **Step 2: Run to verify they fail**

Run: `env -i HOME=/tmp/viberpit-test-home PATH="$PATH" CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest server/tests/test_capacity_readout.py -q -k reserve_for_every` → FAIL `KeyError: 'reservePctByLevel'`.
Run: `(cd web && npx vitest run src/features/delivery/capacity/CapacityDetail.test.tsx -t "from the wire")` → FAIL (finds `40% weekly reserve`, not 45).

- [ ] **Step 3: Server**

In `build_readout`'s return dict, directly after `"governorMode": mode, "killSwitch": src.kill_switch(),` add:

```python
        # C22: the three reserves ride on the wire so the push-level radio's
        # descriptions can never drift from the planner's RESERVE_PCT.
        "reservePctByLevel": dict(RESERVE_PCT),
```

- [ ] **Step 4: Web type, fixtures, component**

`web/src/api/capacity.ts` — after `killSwitch: boolean` in `CapacityReadout`:

```ts
  /** The weekly reserve each push level keeps (server `RESERVE_PCT`); the radio's descriptions. */
  reservePctByLevel: Record<PushLevel, number>
```

`__fixtures__/capacity.ts` — after `killSwitch: true,`: `reservePctByLevel: { conserve: 40, steady: 20, max: 5 },`.
`capacityStack.test.ts` `readout()` helper — same line after `killSwitch: true,`.

`CapacityDetail.tsx` — delete lines 49–57 (`PUSH_LEVEL_RESERVE_PCT`, `PUSH_LEVEL_LABEL`, `PUSH_LEVEL_OPTIONS` and the comment) and replace with:

```ts
const PUSH_LEVEL_LABEL: Record<PushLevel, string> = { conserve: 'Conserve', steady: 'Steady', max: 'Max' }

/** The radio's options, with each level's reserve read off the readout (C22). */
function pushLevelOptions(reservePctByLevel: Record<PushLevel, number>): RadioOption<PushLevel>[] {
  return (['conserve', 'steady', 'max'] as const).map((v) => ({
    value: v,
    label: PUSH_LEVEL_LABEL[v],
    description: `${reservePctByLevel[v]}% weekly reserve`,
  }))
}
```

and in the JSX replace `options={PUSH_LEVEL_OPTIONS}` with `options={pushLevelOptions(readout.reservePctByLevel)}`.

- [ ] **Step 5: Run**

Server: the readout file, `test_capacity_api.py`, `test_capacity_governor_loop.py` → all PASS; `ruff check server/automations/capacity_readout.py`; `mypy server/automations/capacity_readout.py --ignore-missing-imports`.
Web: `(cd web && npx vitest run src/features/delivery/capacity/ && npm run typecheck)` → PASS. If typecheck names another literal `CapacityReadout` (e.g. under `web/e2e` it will not — those are untyped; under `web/src` it might), add the key there and name the file in the report.

- [ ] **Step 6: Commit**

```bash
git add server/automations/capacity_readout.py server/tests/test_capacity_readout.py web/src/api/capacity.ts web/src/features/delivery/capacity/__fixtures__/capacity.ts web/src/features/delivery/capacity/capacityStack.test.ts web/src/features/delivery/capacity/CapacityDetail.tsx web/src/features/delivery/capacity/CapacityDetail.test.tsx
printf '%s\n' 'feat(capacity): reserve per push level rides on the readout; client mirror deleted' '' 'spec 2026-09-20 C22 (and #2032 item 3).' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/pr2-t1.txt
git commit -F /tmp/pr2-t1.txt
```

---

### Task 2: `capacityStack` — Global secondary, Products anchor, per-account line

**Files:**
- Modify: `web/src/features/delivery/capacity/capacityStack.ts`
- Test: `web/src/features/delivery/capacity/capacityStack.test.ts`

**Interfaces:**
- Produces: Global row `secondary` = `physical <n|∞> · attention <n|none> · <physical binds | attention binds | default floor>` (undefined when `binding` is null); Products row `href: '#products'`; `export function accountRowLine(row: CapacityAccountRow): string`.

- [ ] **Step 1: Failing tests**

Append to `capacityStack.test.ts`:

```ts
describe('buildCapacityStack — Global row (spec 2026-09-20 §5.2)', () => {
  it('names both ceilings and which one binds', () => {
    const r = readout({ global: { cap: 10, active: 5, free: 5, physical: 10, attention: 10, binding: 'attention' } })
    expect(buildCapacityStack(r).find((row) => row.key === 'global')!.secondary)
      .toBe('physical 10 · attention 10 · attention binds')
  })
  it('spells an unbounded physical ceiling and a missing attention ceiling', () => {
    const r = readout({ global: { cap: 5, active: 1, free: 4, physical: null, attention: null, binding: 'default' } })
    expect(buildCapacityStack(r).find((row) => row.key === 'global')!.secondary)
      .toBe('physical ∞ · attention none · default floor')
  })
  it('omits the secondary when the server has no derivation', () => {
    expect(buildCapacityStack(readout()).find((row) => row.key === 'global')!.secondary).toBeUndefined()
  })
  it('Products links to the table on the same page', () => {
    expect(buildCapacityStack(readout()).find((row) => row.key === 'products')!.href).toBe('#products')
  })
})

describe('accountRowLine (spec 2026-09-20 C21)', () => {
  const base = { name: 'acct', engine: 'claude_code', provider: 'anthropic', enabled: true, metered: true, available: true, reason: null, nextResetAt: null, windows: [] }
  it('available', () => { expect(accountRowLine(base)).toBe('acct — available') })
  it('unavailable with a reason', () => {
    expect(accountRowLine({ ...base, available: false, reason: 'at_ceiling' })).toBe('acct — at ceiling')
  })
  it('unavailable with a reason and a reset time', () => {
    const line = accountRowLine({ ...base, available: false, reason: 'at_ceiling', nextResetAt: new Date(Date.now() + 3_600_000).toISOString() })
    expect(line.startsWith('acct — at ceiling · resets ')).toBe(true)
  })
  it('not metered', () => {
    expect(accountRowLine({ ...base, engine: 'codex', metered: false, available: false, reason: 'not_metered' })).toBe('acct — not metered')
  })
  it('disabled', () => {
    expect(accountRowLine({ ...base, enabled: false, available: false, reason: 'disabled' })).toBe('acct — disabled')
  })
})
```

Add `accountRowLine` to the import from `./capacityStack`.

- [ ] **Step 2: Run to verify they fail**

Run: `(cd web && npx vitest run src/features/delivery/capacity/capacityStack.test.ts)` → the new tests FAIL (secondary undefined, href null, `accountRowLine` not exported).

- [ ] **Step 3: Implement**

In `capacityStack.ts`:

```ts
import type { CapacityAccountRow, CapacityBindingLayer, CapacityReadout } from '@/api/capacity'
import { formatResetTime } from '@/lib/formatReset'
```

Add before `buildCapacityStack`:

```ts
const GLOBAL_BINDING_TEXT = { physical: 'physical binds', attention: 'attention binds', default: 'default floor' } as const

/** The Global row's second line (spec 2026-09-20 §5.2): both ceilings and which binds. */
function globalSecondary(g: CapacityReadout['global']): string | undefined {
  if (g.binding === null) return undefined
  return `physical ${g.physical ?? '∞'} · attention ${g.attention ?? 'none'} · ${GLOBAL_BINDING_TEXT[g.binding]}`
}

/** One per-account line for the Accounts row's expansion (C21). Reasons are the
 *  server's snake_case codes from `account_headroom`; spelled out here once. */
export function accountRowLine(row: CapacityAccountRow): string {
  if (row.available) return `${row.name} — available`
  const reason = (row.reason ?? 'unavailable').replace(/_/g, ' ')
  const reset = formatResetTime(row.nextResetAt)
  return reset ? `${row.name} — ${reason} · resets ${reset}` : `${row.name} — ${reason}`
}
```

In the Global row: `secondary: globalSecondary(r.global),`. In the Products row: `href: '#products',` and update the `href` doc comment on `CapacityStackRow` to: `/** The page that owns this row's dial; Products uses an in-page anchor to its own table. */`.

- [ ] **Step 4: Run**

`(cd web && npx vitest run src/features/delivery/capacity/ src/features/settings/sections/LimitsSection.test.tsx && npm run typecheck)` → PASS. (`LimitsSection` renders the stack; the Products row's new href changes nothing it asserts.) Check `formatResetTime`'s output shape in `web/src/lib/formatReset.ts` — if it returns a clock time rather than a relative phrase, the test's `startsWith('acct — at ceiling · resets ')` still holds.

- [ ] **Step 5: Commit**

```bash
git add web/src/features/delivery/capacity/capacityStack.ts web/src/features/delivery/capacity/capacityStack.test.ts
printf '%s\n' 'feat(web): capacity stack — Global row names both ceilings; Products anchors; per-account lines' '' 'spec 2026-09-20 §5.2, C21.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/pr2-t2.txt
git commit -F /tmp/pr2-t2.txt
```

---

### Task 3: List column rows link (C3)

**Files:**
- Modify: `web/src/features/delivery/capacity/CapacityScopeBody.tsx:33-45`
- Test: `web/src/features/delivery/DeliveryListColumn.test.tsx`

- [ ] **Step 1: Failing test**

Append to the `describe` in `DeliveryListColumn.test.tsx` (it already imports `within` and mounts inside a router — `mount({ scope: 'capacity', capacity: CAPACITY })`):

```ts
  it('Capacity scope: every row with an owner page links to it (spec 2026-09-20 C3)', () => {
    mount({ scope: 'capacity', capacity: CAPACITY })
    const list = screen.getByRole('list', { name: 'Capacity' })
    expect(within(list).getByRole('link', { name: 'Global cap' })).toHaveAttribute('href', '/settings/capacity')
    expect(within(list).getByRole('link', { name: 'Hosts' })).toHaveAttribute('href', '/settings/fleet')
    expect(within(list).getByRole('link', { name: 'Accounts' })).toHaveAttribute('href', '/settings/accounts')
    expect(within(list).getByRole('link', { name: 'Products' })).toHaveAttribute('href', '/delivery?scope=capacity#products')
  })
```

(If `mount` does not put the column under a router, look at how the file renders — it asserts `getByRole('link', { name: 'Capacity' })` already, so a router is present.)

- [ ] **Step 2: Run to verify it fails**

`(cd web && npx vitest run src/features/delivery/DeliveryListColumn.test.tsx)` → FAIL, no link named "Global cap".

- [ ] **Step 3: Implement**

In `CapacityScopeBody.tsx`: `import { Link } from 'react-router-dom'` and `import { deliveryPath } from '../deliveryNavState'`. Replace `<span className="truncate">{row.label}</span>` with:

```tsx
                {row.href
                  ? (
                    <Link
                      // An in-page anchor must carry the scope, or the router would
                      // resolve '#products' against '/delivery' and drop the query.
                      to={row.href.startsWith('#') ? `${deliveryPath('capacity')}${row.href}` : row.href}
                      className="truncate text-accent hover:underline"
                    >
                      {row.label}
                    </Link>
                  )
                  : <span className="truncate">{row.label}</span>}
```

Also update the file's doc comment: `read-only (no controls: …)` → `read-only apart from each row's link to the page that owns its dial (no controls: …)`.

- [ ] **Step 4: Run**

`(cd web && npx vitest run src/features/delivery/DeliveryListColumn.test.tsx src/features/delivery/DeliveryPage.test.tsx && npm run typecheck)` → PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/features/delivery/capacity/CapacityScopeBody.tsx web/src/features/delivery/DeliveryListColumn.test.tsx
printf '%s\n' 'feat(web): Delivery capacity list rows link to the page that owns each dial' '' 'spec 2026-09-20 C3.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/pr2-t3.txt
git commit -F /tmp/pr2-t3.txt
```

---

### Task 4: CapacityDetail — governor notice (C18), Accounts expansion (C21), products anchor

**Files:**
- Create: `web/src/features/delivery/capacity/governorNotice.ts`
- Modify: `web/src/features/delivery/capacity/CapacityDetail.tsx`
- Test: `web/src/features/delivery/capacity/governorNotice.test.ts` (new), `web/src/features/delivery/capacity/CapacityDetail.test.tsx`

**Interfaces:**
- Produces: `export function governorNotice(r: Pick<CapacityReadout, 'governorMode' | 'killSwitch' | 'plan' | 'planLive'>): { tone: 'warning' | 'info'; title: string; body: string } | null`.

- [ ] **Step 1: Failing tests**

```ts
// web/src/features/delivery/capacity/governorNotice.test.ts
import { describe, it, expect } from 'vitest'
import { CAPACITY } from './__fixtures__/capacity'
import { governorNotice } from './governorNotice'

describe('governorNotice (spec 2026-09-20 C18)', () => {
  it('kill switch off beats everything: plans are computed but never applied', () => {
    const n = governorNotice({ ...CAPACITY, governorMode: 'act', killSwitch: false })
    expect(n?.tone).toBe('warning')
    expect(n?.title).toBe('Governor kill switch is off')
    expect(n?.body).toContain('VIBERPIT_CAPACITY_GOVERNOR_ENABLED')
  })
  it('mode off: nothing is computed', () => {
    const n = governorNotice({ ...CAPACITY, governorMode: 'off', killSwitch: true })
    expect(n).toEqual({ tone: 'info', title: 'Governor is off', body: 'No plan is computed; the runner uses each product’s stored pin and the stored runner ceiling.' })
  })
  it('monitor: plan shown but not applied', () => {
    const n = governorNotice({ ...CAPACITY, governorMode: 'monitor', killSwitch: true })
    expect(n?.tone).toBe('info')
    expect(n?.title).toBe('Governor is in monitor mode')
  })
  it('act with a live plan: no notice', () => {
    expect(governorNotice({ ...CAPACITY, governorMode: 'act', killSwitch: true, planLive: true, plan: { ...CAPACITY.plan!, applied: true } })).toBeNull()
  })
  it('act with an applied but stale plan', () => {
    const n = governorNotice({ ...CAPACITY, governorMode: 'act', killSwitch: true, planLive: false, plan: { ...CAPACITY.plan!, applied: true } })
    expect(n?.tone).toBe('warning')
    expect(n?.title).toBe('The last plan is stale')
  })
  it('act with no plan yet', () => {
    const n = governorNotice({ ...CAPACITY, governorMode: 'act', killSwitch: true, planLive: false, plan: null })
    expect(n).toEqual({ tone: 'info', title: 'No plan yet', body: 'The governor has not ticked since it was enabled; stored caps apply until it does.' })
  })
})
```

In `CapacityDetail.test.tsx`, add:

```ts
  it('surfaces the governor state above the stack, and one Accounts expansion per account (C18, C21)', () => {
    setup({ readout: { ...CAPACITY, killSwitch: false } })
    expect(screen.getByRole('status', { name: /Governor kill switch is off/ })).toBeInTheDocument()
    const accounts = screen.getByTestId('capacity-stack-accounts')
    const details = within(accounts).getByRole('group')
    expect(within(details).getByText('primary — available')).toBeInTheDocument()
    expect(screen.getByRole('table', { name: 'Products' })).toHaveAttribute('id', 'products')
    expect(screen.getByRole('link', { name: 'Products' })).toHaveAttribute('href', '/delivery?scope=capacity#products')
  })
```

(`<details>` has the implicit ARIA role `group`; `Banner` with `role="status"` is named by its `title`. If `getByRole('status', { name })` cannot resolve the name in this jsdom, use `screen.getByText('Governor kill switch is off')` and say so.)

- [ ] **Step 2: Run to verify they fail**

`(cd web && npx vitest run src/features/delivery/capacity/governorNotice.test.ts src/features/delivery/capacity/CapacityDetail.test.tsx)` → FAIL (module missing; no status/group).

- [ ] **Step 3: The helper**

```ts
// web/src/features/delivery/capacity/governorNotice.ts
import type { CapacityReadout } from '@/api/capacity'

export interface GovernorNotice { tone: 'warning' | 'info'; title: string; body: string }

/**
 * The three silent ways the governor can stop governing (spec 2026-09-20 C18):
 * the kill switch, the mode, and plan staleness — folded into ONE sentence for
 * the intent row, most severe first. Null means "act mode, live plan": nothing
 * to say.
 */
export function governorNotice(
  r: Pick<CapacityReadout, 'governorMode' | 'killSwitch' | 'plan' | 'planLive'>,
): GovernorNotice | null {
  if (!r.killSwitch) {
    return {
      tone: 'warning',
      title: 'Governor kill switch is off',
      body: 'VIBERPIT_CAPACITY_GOVERNOR_ENABLED is false on the server: plans are computed and shown but never applied.',
    }
  }
  if (r.governorMode === 'off') {
    return {
      tone: 'info',
      title: 'Governor is off',
      body: 'No plan is computed; the runner uses each product’s stored pin and the stored runner ceiling.',
    }
  }
  if (r.governorMode === 'monitor') {
    return {
      tone: 'info',
      title: 'Governor is in monitor mode',
      body: 'The plan below is computed every tick but not applied; switch to act to let it set the caps.',
    }
  }
  if (r.plan === null) {
    return { tone: 'info', title: 'No plan yet', body: 'The governor has not ticked since it was enabled; stored caps apply until it does.' }
  }
  if (r.plan.applied && !r.planLive) {
    return {
      tone: 'warning',
      title: 'The last plan is stale',
      body: 'It is older than 10 minutes, so the runner is using stored caps until the governor ticks again.',
    }
  }
  return null
}
```

- [ ] **Step 4: The component**

In `CapacityDetail.tsx`:
- Imports: add `import { accountRowLine, buildCapacityStack } from './capacityStack'` (replacing the existing `buildCapacityStack` import), `import { governorNotice } from './governorNotice'`, and `import { deliveryPath } from '../deliveryNavState'`.
- After the closing `</div>` of the intent row (`<div className="flex flex-wrap items-start gap-8">…</div>`), add:

```tsx
      {(() => {
        const notice = governorNotice(readout)
        return notice && (
          <Banner tone={notice.tone} role="status" title={notice.title}>{notice.body}</Banner>
        )
      })()}
```

- Governor mode select: `value={readout.governorMode}` (was `governor?.mode ?? ''`) — the readout is the one source (spec §5.2 item 1); the automation row still supplies `modes`, `id` and the description.
- Stack rows: the row `Link` `to` becomes `to={row.href.startsWith('#') ? `${deliveryPath('capacity')}${row.href}` : row.href}` (same rule as the list column). Inside the `content` block, after the `{row.secondary && …}` line, add the Accounts expansion:

```tsx
              {row.key === 'accounts' && readout.accounts.rows.length > 0 && (
                <details className="mt-1 text-xs text-ink-muted">
                  <summary className="cursor-pointer select-none">
                    {readout.accounts.available} of {readout.accounts.total} available under the {readout.accounts.reservePct}% reserve
                  </summary>
                  <ul className="mt-1 list-disc space-y-0.5 pl-5">
                    {readout.accounts.rows.map((a) => <li key={a.name}>{accountRowLine(a)}</li>)}
                  </ul>
                </details>
              )}
```

- Products table: `<table id="products" className="w-full text-left" aria-label="Products">`.
- Plan card: delete the `{plan.applied && !readout.planLive && (<Banner tone="warning" title="This plan is stale" …/>)}` block — `governorNotice` now owns that sentence (one source).

- [ ] **Step 5: Run**

`(cd web && npx vitest run src/features/delivery/capacity/ && npm run typecheck && npx eslint --max-warnings 0 src/features/delivery/capacity/CapacityDetail.tsx src/features/delivery/capacity/governorNotice.ts)` → PASS. If an existing `CapacityDetail.test.tsx` case asserted the old "This plan is stale" banner text, change it to assert `governorNotice`'s title `The last plan is stale` instead and say so.

- [ ] **Step 6: Commit**

```bash
git add web/src/features/delivery/capacity/governorNotice.ts web/src/features/delivery/capacity/governorNotice.test.ts web/src/features/delivery/capacity/CapacityDetail.tsx web/src/features/delivery/capacity/CapacityDetail.test.tsx
printf '%s\n' 'feat(web): Capacity page says why the governor is not governing; Accounts row expands' '' 'spec 2026-09-20 C18, C21, §5.2.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/pr2-t4.txt
git commit -F /tmp/pr2-t4.txt
```

---

### Task 5: `LiveReadoutCard` on Limits, Hosts and Accounts

**Files:**
- Create: `web/src/features/settings/chassis/LiveReadoutCard.tsx`
- Modify: `web/src/features/settings/settingsDefs.ts` (`SettingsDefShape.readout`, the `fleet` and `accounts` rows), `web/src/features/settings/HostedSection.tsx`, `web/src/features/settings/sections/LimitsSection.tsx:186-212`
- Test: `web/src/features/settings/chassis/LiveReadoutCard.test.tsx` (new), `web/src/features/settings/HostedSection.test.tsx` (new), `web/src/features/settings/settingsDefs.test.ts`, `web/src/features/settings/sections/LimitsSection.test.tsx`

**Interfaces:**
- Produces: `export function LiveReadoutCard({ highlight }: { highlight?: 'hosts' | 'accounts' })` — reads `useCapacity()` itself; renders `<SettingsGroup legend="Live readout">` with the five rows (the highlighted row gets `aria-current="true"` and `font-medium`), the binding `Banner`, the "Open Delivery → Capacity" link, "Loading the readout…" while loading, and "The readout is unavailable." on error. `SettingsDefShape.readout?: 'hosts' | 'accounts'`; `HostedSection` renders the card after the body when set.

- [ ] **Step 1: Failing tests**

```tsx
// web/src/features/settings/chassis/LiveReadoutCard.test.tsx
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import * as hooks from '@/api/hooks'
import { queryResult, idleQueryResult } from '@/test/reactQuery'
import { CAPACITY } from '@/features/delivery/capacity/__fixtures__/capacity'
import { LiveReadoutCard } from './LiveReadoutCard'

vi.mock('@/api/hooks', () => ({ useCapacity: vi.fn() }))

function mount(highlight?: 'hosts' | 'accounts') {
  const router = createMemoryRouter([{ path: '/', element: <LiveReadoutCard highlight={highlight} /> }])
  return render(<RouterProvider router={router} />)
}

describe('LiveReadoutCard (spec 2026-09-20 D10)', () => {
  beforeEach(() => { vi.mocked(hooks.useCapacity).mockReturnValue(queryResult(CAPACITY) as never) })

  it('renders the five rows, the binding text and the link', () => {
    mount()
    const group = screen.getByRole('group', { name: 'Live readout' })
    expect(within(group).getAllByRole('listitem')).toHaveLength(5)
    expect(within(group).getByText('viberpit: at the fleet-wide product cap')).toBeInTheDocument()
    expect(within(group).getByRole('link', { name: 'Open Delivery → Capacity' })).toHaveAttribute('href', '/delivery?scope=capacity')
  })

  it('marks the highlighted row as current', () => {
    mount('hosts')
    const group = screen.getByRole('group', { name: 'Live readout' })
    const items = within(group).getAllByRole('listitem')
    expect(items[1]).toHaveAttribute('aria-current', 'true')
    expect(items[0]).not.toHaveAttribute('aria-current')
  })

  it('loading and error states', () => {
    vi.mocked(hooks.useCapacity).mockReturnValue(idleQueryResult() as never)
    mount()
    expect(screen.getByText('Loading the readout…')).toBeInTheDocument()
    vi.mocked(hooks.useCapacity).mockReturnValue(idleQueryResult({ isError: true, status: 'error', isPending: false } as never) as never)
    mount()
    expect(screen.getByText('The readout is unavailable.')).toBeInTheDocument()
  })
})
```

```tsx
// web/src/features/settings/HostedSection.test.tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { queryResult } from '@/test/reactQuery'
import { CAPACITY } from '@/features/delivery/capacity/__fixtures__/capacity'
import { HostedSection } from './HostedSection'
import { SETTINGS_BY_ID } from './settingsDefs'

vi.mock('@/api/hooks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/hooks')>()
  return { ...actual, useCapacity: vi.fn(() => queryResult(CAPACITY)) }
})

function mount(id: 'fleet' | 'presets') {
  const def = { ...SETTINGS_BY_ID[id], component: () => <p>body</p> }
  const router = createMemoryRouter([{ path: '/', element: <HostedSection def={def} /> }])
  return render(<RouterProvider router={router} />)
}

describe('HostedSection readout slot (spec 2026-09-20 D10)', () => {
  it('a def with `readout` gets the live readout card after its body', () => {
    mount('fleet')
    expect(screen.getByText('body')).toBeInTheDocument()
    expect(screen.getByRole('group', { name: 'Live readout' })).toBeInTheDocument()
  })
  it('a def without `readout` does not', () => {
    mount('presets')
    expect(screen.queryByRole('group', { name: 'Live readout' })).toBeNull()
  })
})
```

In `settingsDefs.test.ts`, add to the group-order test: `expect(SETTINGS_BY_ID.fleet.readout).toBe('hosts')`, `expect(SETTINGS_BY_ID.accounts.readout).toBe('accounts')`, `expect(SETTINGS_BY_ID.capacity.readout).toBeUndefined()`.

- [ ] **Step 2: Run to verify they fail**

`(cd web && npx vitest run src/features/settings/chassis/LiveReadoutCard.test.tsx src/features/settings/HostedSection.test.tsx src/features/settings/settingsDefs.test.ts)` → FAIL (module missing; `readout` undefined).

- [ ] **Step 3: The card**

```tsx
// web/src/features/settings/chassis/LiveReadoutCard.tsx
import { Link } from 'react-router-dom'
import { useCapacity } from '@/api/hooks'
import { Banner } from '@/ui/Banner'
import { deliveryPath } from '@/features/delivery/deliveryNavState'
import { buildCapacityStack, type CapacityStackRow } from '@/features/delivery/capacity/capacityStack'
import { SettingsGroup } from './SettingsGroup'

/**
 * The live ceiling stack, read-only, on a Settings section (spec 2026-09-20
 * D10): Limits shows it whole; Hosts and Accounts show it with their own row
 * marked current. One component, one rendering — the numbers come from
 * `buildCapacityStack`, never re-derived here. Reads `useCapacity()` without
 * a poll: the Delivery page owns the timer; this shares its cache.
 */
export function LiveReadoutCard({ highlight }: { highlight?: CapacityStackRow['key'] }) {
  const { data: capacity, isError } = useCapacity()
  const stack = capacity ? buildCapacityStack(capacity) : null
  return (
    <SettingsGroup
      legend="Live readout"
      description={(
        <>
          The whole ceiling stack as automation sees it.{' '}
          <Link to={deliveryPath('capacity')} className="text-accent hover:underline">Open Delivery → Capacity</Link>
        </>
      )}
    >
      {stack === null
        ? (
          <p className="py-3 text-sm text-ink-muted">
            {isError ? 'The readout is unavailable.' : 'Loading the readout…'}
          </p>
        )
        : (
          <ul className="divide-y divide-edge">
            {stack.map((row) => {
              const current = row.key === highlight
              return (
                <li
                  key={row.key}
                  aria-current={current ? 'true' : undefined}
                  className="flex items-baseline justify-between gap-3 py-2 text-sm"
                >
                  <span className={row.binding ? 'font-medium text-warning' : current ? 'font-medium text-ink' : 'text-ink'}>
                    {row.label}
                  </span>
                  <span className="tabular-nums text-ink-muted">
                    {`${row.used} / ${row.cap ?? '∞'} · ${row.free ?? '∞'} free`}
                  </span>
                </li>
              )
            })}
          </ul>
        )}
      {capacity?.binding && (
        <Banner tone="warning" role="status" title="Binding constraint">{capacity.binding.text}</Banner>
      )}
    </SettingsGroup>
  )
}
```

- [ ] **Step 4: The slot and the consumers**

`settingsDefs.ts`:
- `SettingsDefShape` gains, after `badge?: SettingsBadgeKey`:
  ```ts
  /** Mount the live capacity readout after the body, with this stack row marked current (spec 2026-09-20 D10). */
  readout?: 'hosts' | 'accounts'
  ```
- `fleet` row: add `readout: 'hosts',`; `accounts` row: add `readout: 'accounts',`.

`HostedSection.tsx`:
```tsx
import { SettingsSection } from './chassis/SettingsSection'
import { LiveReadoutCard } from './chassis/LiveReadoutCard'
import type { SettingsDef } from './settingsDefs'
…
    <SettingsSection id={def.id} title={def.label} description={def.description} scope={def.scope}>
      <Body />
      {def.readout && <LiveReadoutCard highlight={def.readout} />}
    </SettingsSection>
```
and extend its doc comment with one sentence: `A def's \`readout\` mounts the live capacity card after the body (Hosts, Accounts), so those sections gain it without their own components learning a hook.`

`LimitsSection.tsx`: delete the whole `<SettingsGroup legend="Live readout" …>…</SettingsGroup>` block (lines 186–212) and render `<LiveReadoutCard />` in its place; remove the now-unused imports (`Link`? — still used by "Set a cap on Hosts"; `deliveryPath`, `buildCapacityStack`, `useCapacity` and the `stack` variable go). Add `import { LiveReadoutCard } from '../chassis/LiveReadoutCard'`.

- [ ] **Step 5: Run**

`(cd web && npx vitest run src/features/settings/ src/features/accounts/ src/features/delivery/capacity/ && npm run typecheck && npx eslint --max-warnings 0 src/features/settings/chassis/LiveReadoutCard.tsx src/features/settings/HostedSection.tsx src/features/settings/settingsDefs.ts src/features/settings/sections/LimitsSection.tsx)` → PASS. `LimitsSection.test.tsx` already mocks `useCapacity`, so its readout-card assertions keep passing through the shared component. `SettingsPage.test.tsx` runs the real hooks over a faked client — if the fake rejects `/capacity`, the card shows "The readout is unavailable." and no assertion there should mind; if one does, report it.

- [ ] **Step 6: Commit**

```bash
git add web/src/features/settings/chassis/LiveReadoutCard.tsx web/src/features/settings/chassis/LiveReadoutCard.test.tsx web/src/features/settings/HostedSection.tsx web/src/features/settings/HostedSection.test.tsx web/src/features/settings/settingsDefs.ts web/src/features/settings/settingsDefs.test.ts web/src/features/settings/sections/LimitsSection.tsx
printf '%s\n' 'feat(web): one live-readout card on Limits, Hosts and Accounts' '' 'spec 2026-09-20 D10 — mounted through a settingsDefs readout slot so Hosts and Accounts need no hook of their own.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/pr2-t5.txt
git commit -F /tmp/pr2-t5.txt
```

---

### Task 6: Queued and capacity-stalled sessions link to Capacity (C6)

**Files:**
- Modify: `web/src/features/sessions/list/QueuedSection.tsx:43-44`, `web/src/features/sessions/list/SessionCard.tsx` (after the status span, ~line 528)
- Test: `web/src/features/sessions/list/QueuedSection.test.tsx`, `web/src/features/sessions/list/SessionCard.capacityLink.test.tsx` (new)

- [ ] **Step 1: Failing tests**

`QueuedSection.test.tsx`: wrap every `render(<QueuedSection …/>)` in `<MemoryRouter>` (import `{ MemoryRouter } from 'react-router-dom'`) — the section now renders a `Link`. Add:

```ts
  it('says why sessions wait and links to Capacity (spec 2026-09-20 C6)', () => {
    render(<MemoryRouter><QueuedSection sessions={[qs('a', 1)]} onOpen={() => {}} /></MemoryRouter>)
    expect(screen.getByText(/Waiting for a free slot under the global cap/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'See capacity' })).toHaveAttribute('href', '/delivery?scope=capacity')
  })
```

```tsx
// web/src/features/sessions/list/SessionCard.capacityLink.test.tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { SessionCard } from './SessionCard'
import { ViewportProvider } from '@/shell/useViewport'
import type { Session } from '@/api/types'

function makeSession(over: Partial<Session> = {}): Session {
  return {
    id: 's1', name: 'My session', accountName: 'acct', status: 'in_progress',
    createdAt: new Date().toISOString(), prs: [], ...over,
  } as Session
}
function renderCard(session: Session) {
  return render(
    <MemoryRouter>
      <ViewportProvider kiosk={null}>
        <SessionCard session={session} account={undefined}
          onOpen={() => {}} onRename={vi.fn()} onClose={vi.fn()} onDelete={vi.fn()} onQuickResume={vi.fn()} />
      </ViewportProvider>
    </MemoryRouter>,
  )
}

describe('SessionCard capacity link (spec 2026-09-20 C6)', () => {
  it('a usage-stalled session links to Delivery → Capacity', () => {
    renderCard(makeSession({ usageStalled: true }))
    expect(screen.getByRole('link', { name: 'See capacity' })).toHaveAttribute('href', '/delivery?scope=capacity')
  })
  it('a working session shows no capacity link', () => {
    renderCard(makeSession({ usageStalled: false }))
    expect(screen.queryByRole('link', { name: 'See capacity' })).toBeNull()
  })
})
```

- [ ] **Step 2: Run to verify they fail**

`(cd web && npx vitest run src/features/sessions/list/QueuedSection.test.tsx src/features/sessions/list/SessionCard.capacityLink.test.tsx)` → FAIL (no such text/link).

- [ ] **Step 3: Implement**

`QueuedSection.tsx`: `import { Link } from 'react-router-dom'` and `import { deliveryPath } from '@/features/delivery/deliveryNavState'`. Directly after the header `<button>…</button>` (before `{!collapsed && …}`), add:

```tsx
      {/* C6: the queue is the global cap's overflow; say so and point at the stack. */}
      <p className="px-1 pb-1 text-xs text-ink-muted">
        Waiting for a free slot under the global cap.{' '}
        <Link to={deliveryPath('capacity')} className="text-accent hover:underline">See capacity</Link>
      </p>
```

`SessionCard.tsx`: `import { deliveryPath } from '@/features/delivery/deliveryNavState'` (`Link` is already imported). Directly after the status `<span aria-label={\`Status: ${presentation.label}\`} …>{presentation.label}</span>`, add:

```tsx
        {/* C6: a session parked for lack of usage headroom is a capacity fact —
            link the stack rather than leave the pill as a dead end. */}
        {session.usageStalled && (
          <Link
            to={deliveryPath('capacity')}
            className="flex-shrink-0 text-accent hover:underline"
            onClick={(e) => e.stopPropagation()}
          >
            See capacity
          </Link>
        )}
```

(`stopPropagation` keeps the card's own open handler from firing on the link click, the same way the PR links inside the card behave — check `PrList.tsx` for the idiom and match it if it differs.)

- [ ] **Step 4: Run**

`(cd web && npx vitest run src/features/sessions/list/ && npm run typecheck && npx eslint --max-warnings 0 src/features/sessions/list/QueuedSection.tsx src/features/sessions/list/SessionCard.tsx)` → PASS. Any other `SessionCard.*.test.tsx` that renders a `usageStalled: true` session without a router would now throw — the run above surfaces it; wrap that render in `MemoryRouter` and name the file.

- [ ] **Step 5: Commit**

```bash
git add web/src/features/sessions/list/QueuedSection.tsx web/src/features/sessions/list/QueuedSection.test.tsx web/src/features/sessions/list/SessionCard.tsx web/src/features/sessions/list/SessionCard.capacityLink.test.tsx
printf '%s\n' 'feat(web): queued and capacity-stalled sessions link to Delivery → Capacity' '' 'spec 2026-09-20 C6.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/pr2-t6.txt
git commit -F /tmp/pr2-t6.txt
```

---

### Task 7: Delivery nav badge lights on a binding constraint (C8)

**Files:**
- Modify: `web/src/shell/useNavBadges.ts`
- Test: `web/src/shell/useNavBadges.test.tsx`

- [ ] **Step 1: Failing test**

In `useNavBadges.test.tsx`: add `const useCapacity = vi.fn()` beside the other hook fns, `useCapacity: () => useCapacity(),` inside the `vi.mock('@/api/hooks', …)` object, and `useCapacity.mockReturnValue({ data: undefined })` in `beforeEach`. Add:

```ts
  // spec 2026-09-20 C8: "nothing can run" must be visible from any section.
  it('delivery lights an attention dot when the capacity readout has a binding constraint', () => {
    useCapacity.mockReturnValue({ data: { binding: { layer: 'product', code: 'product_capacity_full', text: 'x' } } })
    const { result } = renderHook(() => useNavBadges())
    expect(result.current.delivery).toBe(true)
  })
  it('a count from actionable automations still wins over the capacity dot', () => {
    useAutomations.mockReturnValue({ data: [automation({ actionableCount: 2 })] })
    useCapacity.mockReturnValue({ data: { binding: { layer: 'product', code: 'product_capacity_full', text: 'x' } } })
    const { result } = renderHook(() => useNavBadges())
    expect(result.current.delivery).toBe(2)
  })
```

- [ ] **Step 2: Run to verify it fails**

`(cd web && npx vitest run src/shell/useNavBadges.test.tsx)` → the first new test FAILS (`undefined`).

- [ ] **Step 3: Implement**

In `useNavBadges.ts`: add `useCapacity` to the `@/api/hooks` import. After the `useDeployState()` line:

```ts
  // C8: a binding capacity constraint is "nothing can run" — visible from any
  // section. Read WITHOUT a poll: the Delivery page owns the readout's timer,
  // this only shares the cache (plus react-query's mount/focus refetch), so
  // the badge costs no app-wide request loop of its own.
  const { data: capacity } = useCapacity()
```

and change the `delivery` line to:

```ts
    delivery: automationActions || automationAttention || Boolean(capacity?.binding) || undefined,
```

Extend the doc comment's `DEC-4` paragraph with one sentence: `The Delivery dot also lights while the capacity readout names a binding constraint (spec 2026-09-20 C8).`

- [ ] **Step 4: Run**

`(cd web && npx vitest run src/shell/ && npm run typecheck)` → PASS. Grep for other tests that mock `@/api/hooks` WITHOUT a spread and render the nav (`NavRail`, `NavBar`, `AppShell`, `routes.test`): `grep -rln "useNavBadges\|NavRail\|NavBar" web/src --include=*.test.tsx | xargs grep -L "importOriginal" | xargs grep -l "vi.mock('@/api/hooks'"` — add `useCapacity: vi.fn(() => ({ data: undefined }))` to each such mock and name them in the report. Then run those files.

- [ ] **Step 5: Commit**

```bash
git add web/src/shell/useNavBadges.ts web/src/shell/useNavBadges.test.tsx
printf '%s\n' 'feat(web): Delivery nav badge lights on a binding capacity constraint' '' 'spec 2026-09-20 C8; no new polling timer.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/pr2-t7.txt
git commit -F /tmp/pr2-t7.txt
```

(Add any extra test files the grep forced to the `git add`.)

---

### Task 8: Full gate, then the PR

**Files:** none new.

- [ ] **Step 1: Server gate**

```bash
/home/tony/GitHub/viberpit-deploy/.venv/bin/python -m ruff check src/ server/
mkdir -p /tmp/viberpit-test-home
env -i HOME=/tmp/viberpit-test-home PATH="$PATH" PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m mypy server/ --ignore-missing-imports
env -i HOME=/tmp/viberpit-test-home PATH="$PATH" CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest tests/ -q
/home/tony/GitHub/viberpit-deploy/.venv/bin/python scripts/brand_ratchet.py
```

All clean. Then the full server suite, detached so it survives turn boundaries, with the test-slot acquire loop, trap and pytest in ONE script:

```bash
cat > /tmp/pr2-gate.sh <<'EOF'
cd /home/tony/GitHub/.ccswitch/worktrees/viberpit/46d7ae6d
sid="pr2-gate-$$"; host="$(hostname -s)"; trap 'curl -sf -X POST http://127.0.0.1:8787/api/test-slots/release -H "Content-Type: application/json" -d "{\"sessionId\":\"$sid\"}" >/dev/null 2>&1||true' EXIT
while :; do resp="$(curl -sf -X POST http://127.0.0.1:8787/api/test-slots/acquire -H 'Content-Type: application/json' -d "{\"sessionId\":\"$sid\",\"host\":\"$host\"}" 2>/dev/null)" || { echo "test-slot API unreachable — proceeding (fail-open)"; break; }; echo "$resp" | grep -qE '"granted": *true' && break; echo "$resp" | grep -qE '"degraded": *true' && { echo "degraded — proceeding"; break; }; echo "waiting for test slot"; sleep 15; done
mkdir -p /tmp/viberpit-test-home
env -i HOME=/tmp/viberpit-test-home PATH="$PATH" CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest server/tests/ -n auto --dist loadfile --timeout=300 -q
echo "PYTEST_EXIT=$?"
EOF
setsid nohup bash /tmp/pr2-gate.sh > /tmp/pr2-server-tests.log 2>&1 < /dev/null &
```

Wait inside Bash calls (timeout 600000 each, repeat if needed): `until grep -q "PYTEST_EXIT=" /tmp/pr2-server-tests.log; do sleep 30; done; tail -15 /tmp/pr2-server-tests.log`. Expected `PYTEST_EXIT=0`. A failure only under `-n auto` in `test_ipc_entrypoint.py` / UDS-spawn / reauth / ws-timing / codex-overlay is the known runner flake: re-run the file alone; green alone = pre-existing. Anything else: fix, commit, re-run the file.

- [ ] **Step 2: Web gate (foreground)**

```bash
(cd web && npm run typecheck && npm run lint && npm run test 2>&1 | tee /tmp/pr2-web-tests.log | tail -5; echo "vitest exit=${PIPESTATUS[0]}"; npm run build 2>&1 | tail -3)
```

Check the echoed vitest exit code, not `tail`'s. The full suite matters: Tasks 5–7 added `useCapacity` reads to components that ~47 files hand-mock; the grep in Task 7 Step 4 finds the nav ones, this run finds the rest. Afterwards `rm -rf web/dist` (a built dist breaks two server traversal tests on the next local run).

- [ ] **Step 3: Rebase, push, PR, auto-merge**

`git fetch origin master && git rebase origin/master` (resolve any conflict faithfully; a `.brand-ratchet.toml` ceiling conflict resolves to master's value plus this branch's delta, then re-run the ratchet). Check in-flight PRs on the same files with the GraphQL query in `.agents/instructions/core.md` (path regex `capacity|LiveReadoutCard|HostedSection|settingsDefs|QueuedSection|SessionCard\.tsx|useNavBadges`). Then:

```bash
git push -u origin feat/capacity-pr2-readout-links
gh pr create --title "feat(capacity): readout depth and links (spec PR 2)" --body-file /tmp/pr2-body.md --base master --head feat/capacity-pr2-readout-links
gh pr merge <number> --squash --auto
```

with `/tmp/pr2-body.md`:

```markdown
## Summary

PR 2 of the capacity-settings spec (`docs/superpowers/specs/2026-09-20-capacity-settings-structure-design.md`, §8): readout depth and links. Plan: `docs/superpowers/plans/2026-09-20-capacity-settings-pr2-readout-links.md`. Web-only apart from one additive readout key.

- **Delivery → Capacity list rows link** to the page that owns each dial; the Products row anchors to its own table (C3).
- **Global row names both ceilings** and which binds ("physical 10 · attention 10 · attention binds").
- **Accounts row expands** to the per-account lines already on the wire — which account is at ceiling and when it resets (C21).
- **The intent row says why the governor is not governing**: kill switch off, mode off/monitor, no plan yet, stale plan — one notice from one pure helper; the plan card's separate stale banner is gone (C18).
- **Push-level descriptions come from the wire** (`reservePctByLevel`); the client mirror of `RESERVE_PCT` is deleted (C22, #2032 item 3).
- **One live-readout card on Limits, Hosts and Accounts**, mounted through a `readout` slot on the settings registry so Hosts and Accounts gain it without their components learning a hook (D10).
- **Queued sessions and capacity-stalled sessions link to Capacity** (C6).
- **The Delivery nav badge lights on a binding constraint**, without a new polling timer (C8).

No schema change. No new editable field.

## Verification

- `ruff`, `mypy`, `pytest tests/`, brand ratchet, full `pytest server/tests/ -n auto`, `npm run typecheck`, `npm run lint`, full `npm run test`, `npm run build` — all green (numbers in the plan's ledger).
- Per-task spec + quality review, whole-branch review, one fix wave with a scoped re-review.

Refs #2060 (spec), #2032 (governor follow-ups; item 3 closed here).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

Expected: PR open, non-draft, auto-merge armed.

---

## Self-review

**Spec coverage (PR 2 scope, §8 row 2):** list-column links → Task 3 (C3). Accounts row expansion → Task 4 (C21). Global row's three numbers → Task 2 (secondary) rendered by the existing rows in Tasks 3/4/5. Intent-row banner for C18 → Task 4. Live-readout card on the three Settings sections → Task 5. Queued / waiting-for-capacity links (C6) → Task 6. Nav badge (C8) → Task 7. Delete `PUSH_LEVEL_RESERVE_PCT` mirror → Task 1. §5.2 item 1 "reads `readout.governorMode`" → Task 4. §5.2 item 2's runner "quota: N × M" secondary is NOT delivered: the readout carries neither `sessions_per_account` nor the account-slot product, so it would be a guess; recorded as a follow-up for PR 3 (which touches the runner form and can put `accountCap` on the wire).

**Placeholder scan:** none — every code step carries its code; conditional instructions name the exact grep and the exact edit.

**Type consistency:** `reservePctByLevel: Record<PushLevel, number>` (Task 1) is what `pushLevelOptions` consumes (Task 1). `accountRowLine(row: CapacityAccountRow)` (Task 2) is what Task 4 renders. `CapacityStackRow['key']` is the `highlight` type in Task 5 and matches `settingsDefs.readout`'s `'hosts' | 'accounts'` subset. `governorNotice`'s `Pick<…>` accepts the full `CapacityReadout` in Task 4. `deliveryPath('capacity')` resolves to `/delivery?scope=capacity` everywhere tests assert it; `'#products'` is only ever appended to that.
