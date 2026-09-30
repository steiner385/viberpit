# Decision escalation PR 5 — Inbox: impact-ranked decisions, one-tap accept, starvation notice, proposal deep link — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Where the operator already looks — the Roadmap Inbox — pending decisions are ordered by what they block, wear a "Blocks N" chip, can be answered with one tap on the recommendation, and the list is headed by a one-line notice when a product's runner is starving. The disposition dialog's decision link lands on the exact proposal.

**Architecture:** Server-side product scoping for decisions (`fetchDecisions(projectSlug?, { impact })` → PR 2's `?projectSlug=&impact=true`), a `useHolds` hook over PR 2's `GET /api/pm/holds` (the only new fetcher), and small edits to four inbox components. The mutation cache updates move from the bare `QK.decisions` key to a prefix `setQueriesData`, so scoped and unscoped lists both stay optimistic. No new server code.

**Tech Stack:** React 18 + TanStack Query + Tailwind/shadcn; vitest + Testing Library; Playwright (`web/e2e/roadmap.spec.ts`, not a required CI check — run locally).

**Spec:** `docs/superpowers/specs/2026-09-30-decision-escalation-design.md` §8, decisions 6–7, §10 PR 5.

**Depends on:** PR 2 merged (`/pm/holds`, `/pm/decisions?projectSlug=&impact=`). Verify: `grep -n '"/holds"' server/api/pm.py` on the branch base.

## Global Constraints

- Branch off `origin/master` (`git fetch && git checkout -b feat/inbox-decision-impact origin/master`); `web/node_modules` symlinked from the main checkout.
- Web gates: `(cd web && npm run typecheck)`, `(cd web && npm run lint)` (`--max-warnings 0`), the FULL `(cd web && npm run test)` (≈47 files mock `@/api/hooks`; adding `useHolds` to a shared component breaks files you never touched), `(cd web && npm run build)`, and `(cd web && npx playwright test e2e/roadmap.spec.ts)` locally. Always the `(cd web && …)` subshell form. Server: `ruff` + `mypy` unchanged (no server edits) — still run the four `lint-and-test` steps before pushing.
- `useDecisions` becomes `useDecisions(projectSlug?: string, opts?: { enabled?, poll?, impact? })` mirroring `useTriage(projectSlug?, opts)`; query key `[...QK.decisions, projectSlug ?? null, impact]`. Every existing caller is updated (`useRoadmapWorkspace.ts:46`, `RoadmapPage.tsx:78`, `shell/useNavBadges.ts:60`). The nav badge keeps the unscoped, no-impact, polling entry.
- Optimistic decision mutations (`useDecisionMutation`, `api/hooks.ts:2674-2701`) use `qc.setQueriesData({ queryKey: QK.decisions }, updater)` / `qc.cancelQueries({ queryKey: QK.decisions })` over the prefix — never the bare key.
- `invalidateAfterDecision` also invalidates `QK.holds`.
- Ordering: within every bucket, a decision's `downstream` (default 0) leads, then the active comparator; triage rows have no `downstream` and keep their order relative to each other.
- Copy: chip `Blocks N` (only when N > 0); button `Accept recommendation` (only when `recommendedKey`); notice title `"<slug>: <dispatchable> dispatchable, <heldCount> held — the runner is waiting on you"`; tone `warning`; `role="status"` (Banner default).
- The holds hook tolerates the e2e catch-all's `[]` (`data.products ?? []`), and `web/e2e/roadmap.spec.ts` gains an explicit `/api/pm/holds` branch.
- No `text-[Npx]`; Badge/Banner/Button from `@/ui`; labels via `TIER_LABEL`/existing vocab where one exists.
- Deep link: the dialog resolves the pending proposal id CLIENT-SIDE from `useDecisions()` (cached by the nav badge) by `workItemId === data.id`; no server change. Falls back to the plain inbox link when none is found.

## Review Focus

1. Accepting a decision from the scoped inbox list must remove the card from BOTH the scoped list and the nav badge count without waiting for a refetch — the prefix `setQueriesData` is what makes that true. Pinned in Task 1.
2. A product chip set to a product with no starving state must render NO banner (not an empty one), and the unfiltered inbox with two starving products renders one line per product (max 3). Pinned in Task 3.
3. "Accept recommendation" must be disabled while `accept.isPending` and absent when `recommendedKey` is null — a proposal with no recommendation cannot be one-tapped. Pinned in Task 2.
4. A decision with `downstream` undefined (server older than PR 2, or `impact` off) sorts as 0 and shows no chip — never `NaN`, never "Blocks undefined". Pinned in Task 2.
5. The dialog for a `decision` hold whose proposal is not in the cached list (e.g. the badge query has not resolved) must still show the working inbox link, not nothing. Pinned in Task 4.

---

### Task 1: Server-side scoping + impact for decisions; `useHolds`

**Files:**
- Modify: `web/src/api/triage.ts` (`fetchDecisions`, `DecisionProposal.downstream?`, `.downstreamDispatchable?`)
- Modify: `web/src/api/roadmap.ts` (`HoldsView`, `HeldItem`, `fetchHolds`)
- Modify: `web/src/api/hooks.ts` (`QK.holds`, `useDecisions`, `useHolds`, `useDecisionMutation`, `invalidateAfterDecision`)
- Modify: `web/src/features/roadmap/useRoadmapWorkspace.ts:42-54`, `web/src/features/roadmap/RoadmapPage.tsx:78-93`, `web/src/shell/useNavBadges.ts:60`
- Test: `web/src/api/__tests__/triageFetchers.test.ts`, `web/src/api/__tests__/triageHooks.test.tsx`, `web/src/features/roadmap/useRoadmapWorkspace.test.tsx`, `web/src/shell/useNavBadges.test.tsx`

**Interfaces:**
```ts
// api/triage.ts
export function fetchDecisions(projectSlug?: string, opts: { impact?: boolean } = {}): Promise<DecisionProposal[]>
// DecisionProposal gains: downstream?: number; downstreamDispatchable?: number
// api/roadmap.ts
export interface HoldsProduct { slug: string; dispatchable: number; skipped: Record<string, number>; heldCount: number; starving: boolean; oldestHoldHours: number | null }
export interface HeldItem { ref: string; repo: string; number: number; product: string; title: string | null; url: string | null; holdKind: 'owner_action' | 'decision'; holdReason: string; holdBy: string; holdSetAt: string | null; ageHours: number | null; proposalId: string | null; options: { key: string; label: string; detail?: string | null }[]; recommendedKey: string | null; downstream: number; downstreamDispatchable: number; downstreamRefs: string[]; dispatchableOnRelease: boolean; releasedBy: string }
export interface HoldsView { products: HoldsProduct[]; held: HeldItem[]; truncated: number; generatedAt: string }
export function fetchHolds(product?: string): Promise<HoldsView>
// api/hooks.ts
QK.holds = ['pm', 'holds'] as const
export function useDecisions(projectSlug?: string, { enabled = true, poll = true, impact = false } = {})
export function useHolds(product?: string, { enabled = true } = {})   // staleTime 5_000, refetchInterval 30_000
```

- [ ] **Step 1: Write the failing tests**

`api/__tests__/triageFetchers.test.ts` (beside the existing `fetchDecisions` test; use the file's `api.get` mock):
```ts
it('fetchDecisions scopes by projectSlug and asks for impact only when told', async () => {
  await fetchDecisions('kindash')
  expect(get).toHaveBeenLastCalledWith('/pm/decisions?projectSlug=kindash')
  await fetchDecisions(undefined, { impact: true })
  expect(get).toHaveBeenLastCalledWith('/pm/decisions?impact=true')
  await fetchDecisions('kindash', { impact: true })
  expect(get).toHaveBeenLastCalledWith('/pm/decisions?projectSlug=kindash&impact=true')
  await fetchDecisions()
  expect(get).toHaveBeenLastCalledWith('/pm/decisions')
})

it('fetchHolds scopes by product and tolerates a bare array', async () => {
  get.mockResolvedValueOnce({ products: [], held: [], truncated: 0, generatedAt: 't' })
  await fetchHolds('kindash')
  expect(get).toHaveBeenLastCalledWith('/pm/holds?product=kindash')
  get.mockResolvedValueOnce([])           // the e2e catch-all shape
  const v = await fetchHolds()
  expect(v.products).toEqual([]) ; expect(v.held).toEqual([])
})
```

`api/__tests__/triageHooks.test.tsx` — update the two seeds at lines ~310/316 from `QK.decisions` to `[...QK.decisions, null, false]`, and add:
```tsx
it('accepting a decision removes it from every cached decisions list (scoped and unscoped)', async () => {
  const qc = new QueryClient()
  qc.setQueryData([...QK.decisions, null, false], [decision('d1'), decision('d2')])
  qc.setQueryData([...QK.decisions, 'kindash', true], [decision('d1')])
  const { result } = renderHook(() => useAcceptDecision(), { wrapper: wrapperFor(qc) })
  act(() => result.current.mutate({ id: 'd1', chosenKey: 'a' }))
  await waitFor(() => expect(qc.getQueryData([...QK.decisions, null, false])).toHaveLength(1))
  expect(qc.getQueryData([...QK.decisions, 'kindash', true])).toHaveLength(0)
})
```
(Reuse the file's existing `decision()` builder / wrapper helpers; if the names differ, use theirs.)

`features/roadmap/useRoadmapWorkspace.test.tsx` — the path assertions: with `?scope=inbox&product=kindash` expect a `/pm/decisions?projectSlug=kindash&impact=true` call and NO client-side filtering; with no product expect `/pm/decisions?impact=true`; add `/pm/holds…` to any exact `paths()` equality (the workspace does not fetch holds itself — only the notice does, in Task 3 — so no holds path here yet).

`shell/useNavBadges.test.tsx` — unchanged assertions; confirm the mocked `useDecisions` is still called with no product (`expect(useDecisions).toHaveBeenCalledWith()` or with `undefined`).

- [ ] **Step 2: Run** — `(cd web && npx vitest run src/api/__tests__/triageFetchers.test.ts src/api/__tests__/triageHooks.test.tsx src/features/roadmap/useRoadmapWorkspace.test.tsx src/shell/useNavBadges.test.tsx)`; Expected: FAIL.

- [ ] **Step 3: Implement**

`api/triage.ts`:
```ts
/** Scoped server-side (spec 2026-09-30 §4.3): `projectSlug` narrows like `/pm/triage`;
 *  `impact` adds `downstream` / `downstreamDispatchable` from the escalation read model —
 *  opt-in because the nav badge polls this list every 15 s and the impact costs the
 *  queue read. */
export function fetchDecisions(projectSlug?: string, { impact = false }: { impact?: boolean } = {}): Promise<DecisionProposal[]> {
  const qs = new URLSearchParams()
  if (projectSlug) qs.set('projectSlug', projectSlug)
  if (impact) qs.set('impact', 'true')
  const suffix = qs.size ? `?${qs}` : ''
  return api.get<Omit<DecisionProposal, 'proposalKind'>[]>(`/pm/decisions${suffix}`)
    .then((rows) => rows.map((r) => ({ ...r, proposalKind: 'decision' as const })))
}
```
(`URLSearchParams` encodes `&`; the test expects `?projectSlug=kindash&impact=true` — set in that order.)

`api/roadmap.ts`: the types above and
```ts
export function fetchHolds(product?: string): Promise<HoldsView> {
  const q = product ? `?product=${encodeURIComponent(product)}` : ''
  return api.get<HoldsView | unknown[]>(`/pm/holds${q}`).then((v) =>
    // The e2e catch-all answers `[]` for any unstubbed collection; read it as empty.
    Array.isArray(v) ? { products: [], held: [], truncated: 0, generatedAt: '' } : v)
}
```

`api/hooks.ts`: `holds: ['pm', 'holds'] as const` in `QK`; rewrite `useDecisions` to the `useTriage` shape with `queryKey: [...QK.decisions, projectSlug ?? null, impact]` and `queryFn: () => fetchDecisions(projectSlug, { impact })`; add `useHolds` beside `useQueue`; in `useDecisionMutation` replace `qc.cancelQueries({ queryKey: QK.decisions })` (keep — prefix match), `qc.getQueryData(QK.decisions)` + `qc.setQueryData(QK.decisions, …)` with `qc.setQueriesData<DecisionProposal[]>({ queryKey: QK.decisions }, (rows) => rows?.filter((r) => r.id !== vars.id))` and, on error, `qc.invalidateQueries({ queryKey: QK.decisions })` (rollback by refetch — simpler than snapshotting N keys; note it in the hook's comment); add `qc.invalidateQueries({ queryKey: QK.holds })` to `invalidateAfterDecision`.

Callers: `useRoadmapWorkspace.ts` → `const decisions = useDecisions(product ?? undefined, { enabled: active, poll: product !== null, impact: true })` and drop the `.filter(...)` (and its stale comment); `RoadmapPage.tsx:78-93` likewise with `inboxProduct`; `useNavBadges.ts:60` stays `useDecisions()`.

- [ ] **Step 4: Run** — the four files, then `(cd web && npm run typecheck)`; Expected: PASS.
- [ ] **Step 5: Commit** — `feat(web): decisions scoped server-side with opt-in impact; useHolds over /pm/holds`.

---

### Task 2: Impact-first ordering, "Blocks N" chip, "Accept recommendation"

**Files:**
- Modify: `web/src/features/roadmap/inbox/inboxOrdering.ts`, `ProposalCard.tsx` (`DecisionRow`), `ProposalDetail.tsx` (`DecisionDetail`)
- Test: `inboxOrdering.test.ts`, `ProposalCard.test.tsx`, `ProposalDetail.test.tsx`

- [ ] **Step 1: Write the failing tests**

`inboxOrdering.test.ts` (use the file's `decision()` / `triage()` builders):
```ts
it('a decision that blocks more work leads its bucket under every sort; triage rows keep their order', () => {
  const rows = [triage({ id: 't1', createdAt: '2026-01-03' }), decision({ id: 'd-low', downstream: 1, createdAt: '2026-01-02' }),
                decision({ id: 'd-high', downstream: 7, createdAt: '2026-01-01' }), decision({ id: 'd-none', createdAt: '2026-01-04' })]
  for (const sortBy of ['newest', 'confidence', 'title'] as const) {
    const ids = [...rows].sort(INBOX_ORDERING_SPEC.sorts[sortBy]).map((p) => p.id)
    expect(ids.slice(0, 2)).toEqual(['d-high', 'd-low'])
  }
  const newest = [...rows].sort(INBOX_ORDERING_SPEC.sorts.newest).map((p) => p.id)
  expect(newest).toEqual(['d-high', 'd-low', 'd-none', 't1'])   // undefined downstream = 0; then newest
})
```
(Read the spec's real property names — `sorts` may be spelled differently; assert through whatever the file exports.)

`ProposalCard.test.tsx`:
```tsx
it('a decision row shows how much it blocks, and nothing when it blocks nothing', () => {
  render(<ProposalCard proposal={decisionFixture({ downstream: 7 })} … />)
  expect(screen.getByText('Blocks 7')).toBeInTheDocument()
  cleanup()
  render(<ProposalCard proposal={decisionFixture({ downstream: 0 })} … />)
  expect(screen.queryByText(/^Blocks/)).toBeNull()
  cleanup()
  render(<ProposalCard proposal={decisionFixture({})} … />)         // undefined
  expect(screen.queryByText(/Blocks/)).toBeNull()
})
```

`ProposalDetail.test.tsx` (inside `describe('ProposalDetail — a decision proposal')`, using `mountDecision`):
```tsx
it('Accept recommendation accepts the recommended option in one tap', async () => {
  mountDecision({ recommendedKey: 'queue' })
  await userEvent.click(screen.getByRole('button', { name: 'Accept recommendation' }))
  expect(acceptDecision).toHaveBeenCalledWith({ id: 'd1', chosenKey: 'queue' })
  expect(onDone).toHaveBeenCalled()
})
it('Accept recommendation is absent without a recommendation', () => {
  mountDecision({ recommendedKey: null })
  expect(screen.queryByRole('button', { name: 'Accept recommendation' })).toBeNull()
  expect(screen.getByRole('button', { name: 'Accept' })).toBeDisabled()   // existing behaviour kept
})
```
(Use the file's existing spies `acceptDecision` / `onDone` names.)

- [ ] **Step 2: Run** — Expected: FAIL.

- [ ] **Step 3: Implement**

`inboxOrdering.ts`:
```ts
/** What a row blocks (spec 2026-09-30 §8): a decision's `downstream` from the
 *  escalation read model; a triage row blocks nothing. Leads every sort so the
 *  loudest decision is first in its bucket whatever the operator sorts by. */
const downstreamOf = (p: InboxProposal) => (isDecisionProposal(p) ? p.downstream ?? 0 : 0)
const impactFirst = (cmp: (a: InboxProposal, b: InboxProposal) => number) =>
  (a: InboxProposal, b: InboxProposal) => downstreamOf(b) - downstreamOf(a) || cmp(a, b)
```
wrap the three comparators with `impactFirst(...)`.

`ProposalCard.tsx` `DecisionRow.trailing`:
```tsx
trailing={
  <span className="flex items-center gap-2">
    {(p.downstream ?? 0) > 0 && <Badge tone="warning">Blocks {p.downstream}</Badge>}
    <Badge tone={TIER_TONE[DECISION_TIER]}>{p.options.length} options</Badge>
  </span>
}
```
(Check `Badge`'s `tone` union for the right warning value; use the same tone the hygiene banner uses.)

`ProposalDetail.tsx` `DecisionDetail` — beside the existing Accept/Reject buttons (spec decision 7: one tap by a person; the radio group stays for a different choice):
```tsx
{p.recommendedKey && (
  <Button type="button" size="lg" disabled={accept.isPending}
          onClick={() => { accept.mutate({ id: p.id, chosenKey: p.recommendedKey! }); onDone() }}>
    Accept recommendation
  </Button>
)}
```
Order the buttons: Accept recommendation (primary), Accept (selected option), Reject (ghost). Keep the existing `type="submit"` Accept so its tests hold.

- [ ] **Step 4: Run** — the three test files + typecheck + lint; Expected: PASS.
- [ ] **Step 5: Commit** — `feat(web): inbox decisions lead by what they block; Blocks N chip; one-tap Accept recommendation`.

---

### Task 3: Starvation notice above the inbox

**Files:**
- Create: `web/src/features/roadmap/inbox/InboxStarvationNotice.tsx`
- Modify: `web/src/features/roadmap/inbox/InboxScopeBody.tsx` (mount before the `isError` banner, ~line 380)
- Modify: `web/src/features/roadmap/inbox/InboxScopeBody.test.tsx` (mock `useHolds`), `web/src/features/roadmap/RoadmapPage.test.tsx` (mock `useHolds` at the `@/api/hooks` mock ~line 35), `web/e2e/roadmap.spec.ts` (`/api/pm/holds` branch near line 240)
- Test: `web/src/features/roadmap/inbox/InboxStarvationNotice.test.tsx` (new)

- [ ] **Step 1: Write the failing tests**

```tsx
import { render, screen } from '@testing-library/react'
import { vi } from 'vitest'
const useHolds = vi.fn()
vi.mock('@/api/hooks', () => ({ useHolds: (...a: unknown[]) => useHolds(...a) }))
import { InboxStarvationNotice } from './InboxStarvationNotice'

const product = (slug: string, starving: boolean, dispatchable = 0, heldCount = 12) =>
  ({ slug, starving, dispatchable, heldCount, skipped: {}, oldestHoldHours: null })

it('renders one line per starving product (max 3), and nothing when none is starving', () => {
  useHolds.mockReturnValue({ data: { products: [product('kindash', true, 0, 12), product('viberpit', false, 9, 1),
                                                 product('a', true), product('b', true), product('c', true)] } })
  render(<InboxStarvationNotice product={null} />)
  const status = screen.getByRole('status')
  expect(status).toHaveTextContent('kindash: 0 dispatchable, 12 held — the runner is waiting on you')
  expect(status.textContent).not.toContain('viberpit')
  expect(screen.getAllByText(/waiting on you/)).toHaveLength(3)
  useHolds.mockReturnValue({ data: { products: [product('kindash', false, 5, 0)] } })
  const { container } = render(<InboxStarvationNotice product="kindash" />)
  expect(container.querySelectorAll('[role="status"]')).toHaveLength(0)
})

it('passes the product chip through to the hook and tolerates no data', () => {
  useHolds.mockReturnValue({ data: undefined })
  const { container } = render(<InboxStarvationNotice product="kindash" />)
  expect(useHolds).toHaveBeenLastCalledWith('kindash', expect.anything())
  expect(container).toBeEmptyDOMElement()
})
```

- [ ] **Step 2: Run** — Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

```tsx
import { useHolds } from '@/api/hooks'
import { Banner } from '@/ui/Banner'

const MAX_LINES = 3

/** Spec 2026-09-30 §8: when a product's runner is starving (nothing dispatchable while
 *  items are held), say so above the list — one line per product, the shape of the
 *  hygiene notice (#2540). Reads the escalation read model's per-product summary. */
export function InboxStarvationNotice({ product }: { product: string | null }) {
  const { data } = useHolds(product ?? undefined, { enabled: true })
  const starving = (data?.products ?? []).filter((p) => p.starving).slice(0, MAX_LINES)
  if (starving.length === 0) return null
  return (
    <div className="flex flex-col gap-1">
      {starving.map((p) => (
        <Banner key={p.slug} tone="warning"
                title={`${p.slug}: ${p.dispatchable} dispatchable, ${p.heldCount} held — the runner is waiting on you`} />
      ))}
    </div>
  )
}
```
Mount in `InboxScopeBody.tsx` immediately before `{isError && <Banner tone="danger" …/>}` as `<InboxStarvationNotice product={product ?? null} />` (the body already has `product` in scope). Add `useHolds: () => ({ data: undefined })` to the `vi.mock('@/api/hooks', …)` blocks in `InboxScopeBody.test.tsx` and `RoadmapPage.test.tsx`. In `web/e2e/roadmap.spec.ts` add `else if (path === '/api/pm/holds') { route.fulfill({ json: { products: [], held: [], truncated: 0, generatedAt: '' } }) }` beside the triage branch.

- [ ] **Step 4: Run** — the new test, `InboxScopeBody.test.tsx`, `RoadmapPage.test.tsx`, then the FULL `(cd web && npm run test)`; Expected: PASS.
- [ ] **Step 5: Commit** — `feat(web): starvation notice above the inbox from /pm/holds`.

---

### Task 4: Disposition dialog deep link

**Files:**
- Modify: `web/src/features/roadmap/detail/IssueDispositionDialog.tsx:196-203`
- Test: `web/src/features/roadmap/detail/IssueDispositionDialog.test.tsx:212-224`

- [ ] **Step 1: Write the failing test** — in the decision-hold test, make the stubbed fetch also answer `/pm/decisions` (the file uses a global `fetchMock`; add a branch returning `[{ id: 'dp-9', workItemId: <the issue's id>, status: 'pending', proposalKind: 'decision', … }]`) and assert the link `href` is `/roadmap?scope=inbox&proposal=dp-9`. Add a second case where `/pm/decisions` returns `[]` and assert the href is `/roadmap?scope=inbox` (the fallback — Review Focus 5).

- [ ] **Step 2: Run** — Expected: FAIL (href unchanged).

- [ ] **Step 3: Implement** — in the dialog: `const { data: decisions } = useDecisions()` (the badge's cached entry); `const proposalId = decisions?.find((d) => d.workItemId === data.id && d.status === 'pending')?.id ?? null`; `<Link to={inboxPath({ proposal: proposalId })}>` (`inboxPath` omits the param when null — hold-ladder §14 deep-link follow-up closed). Import `useDecisions` from `@/api/hooks`; if the dialog file's test mocks `@/api/hooks` rather than fetch, add `useDecisions` to that mock instead.

- [ ] **Step 4: Run** — the dialog test + typecheck; Expected: PASS. **Step 5: Commit** — `fix(roadmap): a decision hold links to its proposal`.

---

### Task 5: Gate, docs, PR

- [ ] `docs/product/features/product-management-layer.md` Holds/Inbox section: three sentences on impact ordering, the "Blocks N" chip and Accept recommendation, the starvation notice. Spec §14-style note in `2026-09-18-hold-ladder-design.md` open follow-ups: mark the "decisions unscoped on the wire" and "deep link precision" items closed by PRs 2/5.
- [ ] Gates: `(cd web && npm run typecheck && npm run lint && npm run test && npm run build)`; `(cd web && npx playwright test e2e/roadmap.spec.ts)`; the four `lint-and-test` server steps (no server change — still run `ruff`, `mypy`, `pytest tests/ -q` and the full `-m "not slow"` server run → `/tmp/pr5-full.log`).
- [ ] In-flight PR check for `web/src/api/hooks.ts|web/src/features/roadmap/inbox|IssueDispositionDialog`.
- [ ] Push `feat/inbox-decision-impact`; `gh pr create --title "feat(web): inbox decisions ranked by what they block, one-tap accept, starvation notice, proposal deep link" --body-file /tmp/pr5-body.md` (`Refs …decision-escalation-design.md §8 (PR 5 of 6)`; trailer); `gh pr merge <#> --squash --auto`.
