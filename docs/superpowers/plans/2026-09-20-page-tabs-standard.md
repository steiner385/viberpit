# Page Tabs Standard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put the product detail, the automation detail and the session body on the "one tab, one question" standard in `docs/PAGE-TABS.md`, and make the single-tablist rule self-enforcing.

**Architecture:** Every change is web-only. The two detail panes keep their `ui/Tabs` + `?tab=` shape and move the lumped blocks out of their first tab into new tabs (Automation on the product, Queue on the automation). The session body swaps its hand-rolled tablist for `ui/Tabs` and stores its view in `?view=`. A source-scan test pins `ui/Tabs.tsx` as the only `role="tablist"`.

**Tech Stack:** React 19 + TypeScript, react-router-dom (`useSearchParams`), vitest + Testing Library, Tailwind tokens. Run web commands as `(cd web && npm run …)` subshells — a persisted `cd web` breaks the repo's Bash hook.

**Spec:** `docs/superpowers/specs/2026-09-20-page-tabs-standard-design.md` (change record) and `docs/PAGE-TABS.md` (the standard).

## Global Constraints

- `web/src/ui/Tabs.tsx` is the only component that renders `role="tablist"` (standard §7).
- Tab ids live in a `const TABS = [...] as const` tuple; shared labels come from `web/src/features/products/vocab.ts`.
- Tab state is the URL through `useSearchParamState` (`web/src/lib/useSearchParamState.ts`), replace semantics.
- Overview holds one identity block + at most three summary blocks; never a form with Save, a table, or a copy of another tab's list (standard §5).
- At most six tabs per page; hide a tab only by object kind (standard §4).
- Never write the legacy product name (the one the brand ratchet counts) in new prose or identifiers; say ViberPit.
- Gate before pushing: `(cd web && npm run typecheck && npm run lint && npm run test && npm run build)`. The full `npm run test`, not a changed-files run.
- Node modules: `web/node_modules` is a symlink to the main checkout's; do not `npm install`.

---

### Task 1: Automation detail — Overview + Queue tabs

**Files:**
- Modify: `web/src/features/products/vocab.ts:169-174` (`AUTOMATION_TAB`)
- Modify: `web/src/features/delivery/automation/AutomationDetail.tsx`
- Modify: `web/src/features/delivery/automation/AutomationDetail.test.tsx`
- Modify: `web/src/features/delivery/DeliveryPage.test.tsx:167-179` (the "shows Status" assertion)
- Modify: `docs/product/features/autopilot-backlog-runner.md:486` ("on the Status tab")

**Interfaces:**
- Consumes: `Tabs` (`@/ui/Tabs`), `useSearchParamState<T>(name, allowed, fallback)`, fixtures `AUTOMATIONS[0]` (pr-doctor), `[1]` (backlog-runner), `[2]` (pm-classify) in `web/src/features/delivery/__fixtures__/automations.ts`.
- Produces: `AUTOMATION_TAB` keys `overview | queue | schedule | events | sessions | config`; `AutomationTab` type; the exported `QUEUE_AUTOMATIONS: ReadonlySet<string>` from `AutomationDetail.tsx` (used by Task 1's tests only).

- [ ] **Step 1: Write the failing tests**

In `AutomationDetail.test.tsx`, change the first test's title and assertion and add three tests. Replace the test at the top of the `describe` block:

```tsx
  it('Overview: description, oversight and actionable items — no queue surface', async () => {
    mount()
    expect(screen.getByRole('tab', { name: 'Overview', selected: true })).toBeInTheDocument()
    expect(screen.getByText('CI red on #1')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Fix' }))
    await waitFor(() => expect(trigger).toHaveBeenCalledWith({ item_id: 'o/r#1', kind: 'fix' }))
    // The consolidation panel is the Queue tab's, not the Overview's.
    expect(screen.queryByTestId('consolidate')).not.toBeInTheDocument()
  })
  it('Queue: the PR doctor consolidation panel', () => {
    mount(prDoctor, '/delivery/automations/pr-doctor?tab=queue')
    expect(screen.getByRole('tab', { name: 'Queue', selected: true })).toBeInTheDocument()
    expect(screen.getByTestId('consolidate')).toBeInTheDocument()
  })
  it('Queue: the runner’s products table, groups panel and custom dispatch', () => {
    const runner = {
      ...AUTOMATIONS[1],
      status: {
        ...AUTOMATIONS[1].status,
        products: [{ slug: 'viberpit', dispatch: 'auto', inFlight: 1, cap: 1, eligible: 12, bounced: 2 }],
      },
    }
    mount(runner, '/delivery/automations/backlog-runner?tab=queue')
    expect(screen.getByRole('link', { name: 'viberpit' })).toHaveAttribute('href', '/roadmap/viberpit')
    expect(screen.getByTestId('groups')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Custom dispatch…' })).toBeInTheDocument()
  })
  it('an automation with no dispatchable work has no Queue tab, and ?tab=status lands on Overview', () => {
    mount(AUTOMATIONS[2], '/delivery/automations/pm-classify?tab=status')
    expect(screen.queryByRole('tab', { name: 'Queue' })).not.toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Overview', selected: true })).toBeInTheDocument()
  })
```

Then update the three existing tests that assert the products table from the default tab so they mount at `?tab=queue`:

- `'Status tab: a per-product table when status.products is non-empty, linking to the roadmap'` → title `'Queue: a per-product table when status.products is non-empty, linking to the roadmap'`, `mount(runner, '/delivery/automations/backlog-runner?tab=queue')`.
- `'shows active dispatch-off work and explicitly accounts for unassigned work'` — this one asserts BOTH the table (`'off'`, `'1/2'`) and the Overview note (`'2 in-flight work items…'`). Split it: keep the note assertion on the default mount, and move the two table assertions into a second `mount(…, '?tab=queue')` inside the same test (mount twice; `render` appends, so use `screen.getAllByText` for nothing — the two mounts render disjoint text).
- `'Status tab: no per-product table when status.products is null'` → title `'Queue: no per-product table when status.products is null'`, mount `prDoctor` at `?tab=queue`.
- `'Status tab: no per-product section at all when status.products is empty'` → title `'Queue: …'`, mount at `?tab=queue`.

In `DeliveryPage.test.tsx`, change the assertion at line 179 to `{ name: 'Overview', selected: true }` and the test title's "shows Status" to "shows Overview".

- [ ] **Step 2: Run the tests to verify they fail**

Run: `(cd web && npx vitest run src/features/delivery)`
Expected: FAIL — `Unable to find role="tab" and name "Overview"` and `"Queue"`.

- [ ] **Step 3: Update the vocabulary**

In `web/src/features/products/vocab.ts` replace the `AUTOMATION_TAB` block:

```ts
/** The automation detail's tabs, in order (`docs/PAGE-TABS.md` §3: one tab,
 *  one question). `queue` is offered only to automations that have
 *  dispatchable work (`QUEUE_AUTOMATIONS` in `AutomationDetail.tsx`). */
export const AUTOMATION_TAB = {
  overview: 'Overview', queue: 'Queue', schedule: 'Schedule', events: 'Events', sessions: 'Sessions', config: 'Config',
} as const
export type AutomationTab = keyof typeof AUTOMATION_TAB
export const AUTOMATION_TABS = Object.keys(AUTOMATION_TAB) as readonly AutomationTab[]
```

- [ ] **Step 4: Split the Status panel into Overview and Queue**

Replace `AutomationDetail.tsx` from the `export function AutomationDetail` line to the end with:

```tsx
/** Automations whose work can be inspected and dispatched by hand — the only
 *  ones that get a Queue tab (standard §4: hide a tab by object kind, never
 *  because it is empty right now). */
export const QUEUE_AUTOMATIONS: ReadonlySet<string> = new Set(['backlog-runner', 'pr-doctor'])

export function AutomationDetail({ automation: a, automations = [], onOpenSession }: {
  automation: Automation
  automations?: readonly Automation[]
  onOpenSession: (id: string) => void
}) {
  const operatorTriggered = a.operatorTriggered ?? a.modes.length === 0
  const items = AUTOMATION_TABS.filter((t) =>
    !(t === 'queue' && !QUEUE_AUTOMATIONS.has(a.id))
    && !(t === 'schedule' && operatorTriggered)
    && !(t === 'config' && a.configSchema.length === 0))
  // `?tab=status` (the pre-standard name of the first tab) is not in `items`,
  // so an old bookmark falls back to Overview — no redirect needed.
  const [tab, setTab] = useSearchParamState<AutomationTab>('tab', items, 'overview')
  const [customDispatch, setCustomDispatch] = useState(false)

  return (
    <>
      <Tabs
        label="Automation"
        items={items.map((id) => ({ id, label: AUTOMATION_TAB[id] }))}
        value={tab}
        onChange={(id) => setTab(id as AutomationTab)}
        panelId={(id) => `automation-panel-${id}`}
        className="mb-4"
      />
      <div role="tabpanel" id={`automation-panel-${tab}`} aria-labelledby={`tab-${tab}`} className="space-y-4">
        {tab === 'overview' && (
          <>
            {a.description && <p className="text-sm text-ink-muted">{a.description}</p>}
            {/* The tier dot and `status.text` are NOT repeated here: the page
                header carries them (R9), where they stay visible on every tab
                rather than only on this one. */}
            {a.oversight ? (
              <div className="space-y-2 text-sm">
                <p><strong>Responsibility: </strong>{a.oversight.responsibility.label}</p>
                <p><strong>Scope: </strong>{a.oversight.scope}</p>
                {a.oversight.relationships.length > 0 && (
                  <ul className="space-y-2" aria-label="Relationships">
                    {a.oversight.relationships.map((relationship) => {
                      const target = automations.find((item) => item.id === relationship.targetId)
                      return (
                        <li key={`${relationship.kind}:${relationship.targetId}`}>
                          <span className="capitalize">{relationship.kind.replaceAll('_', ' ')}</span>{' '}
                          <Link className="text-accent hover:underline" to={automationPath(relationship.targetId)}>
                            {target?.name ?? relationship.targetId}
                          </Link>
                          <p className="text-ink-muted">{relationship.description}</p>
                        </li>
                      )
                    })}
                  </ul>
                )}
              </div>
            ) : <p className="text-sm text-ink-muted">Responsibility and scope unavailable.</p>}
            {a.id === 'backlog-runner' && (
              <div className="text-sm text-ink-muted">
                {a.status.products == null && <p>Product workload unavailable.</p>}
                {a.status.counts?.unassigned_in_flight == null
                  ? <p>Unassigned workload unknown.</p>
                  : a.status.counts.unassigned_in_flight > 0 && <p>{a.status.counts.unassigned_in_flight} in-flight work items are not assigned to a product.</p>}
              </div>
            )}
            {/* The Overview's "needs you" notice: renders nothing when there is
                nothing actionable (standard §5). */}
            <AutomationActions automationId={a.id} enabled />
          </>
        )}
        {tab === 'queue' && (
          <>
            {a.status.products && a.status.products.length > 0 && (
              <CheckTable<AutomationStatusProduct>
                title="Products"
                columns={PRODUCT_COLUMNS}
                rows={a.status.products}
                empty="No products yet."
              />
            )}
            {a.id === 'pr-doctor' && <ConsolidatePanel enabled onOpenSession={onOpenSession} />}
            {a.id === 'backlog-runner' && (
              <>
                <div className="flex justify-end">
                  <Button size="sm" variant="secondary" onClick={() => setCustomDispatch(true)}>Custom dispatch…</Button>
                </div>
                <BacklogGroupsPanel enabled onOpenSession={onOpenSession} />
              </>
            )}
          </>
        )}
        {tab === 'schedule' && <SchedulePanel automation={a} />}
        {tab === 'events' && <EventsList automationId={a.id} onOpenSession={onOpenSession} />}
        {tab === 'sessions' && <SessionsList automation={a} />}
        {tab === 'config' && <ConfigPanel automation={a} />}
      </div>
      {/* Outside the tab panel (standard §6): a tab change must not unmount a
          half-filled dispatch form. */}
      {a.id === 'backlog-runner' && (
        <ManualDispatchDialog open={customDispatch} onClose={() => setCustomDispatch(false)} onOpenSession={onOpenSession} />
      )}
    </>
  )
}
```

Update the file's header comment: "five tabs" → "Overview · Queue · Schedule · Events · Sessions · Config (`docs/PAGE-TABS.md`)". Check `AutomationActions` renders nothing when `useAutomationActionable` returns an empty list (read `actions.tsx`); if it renders a heading with no rows, wrap it so the heading only shows with rows — the Overview's notices self-hide.

- [ ] **Step 5: Update the feature doc**

In `docs/product/features/autopilot-backlog-runner.md` line 486, change "renders it as a table on the Status tab" to "renders it as a table on the Queue tab (`docs/PAGE-TABS.md`)".

- [ ] **Step 6: Run the tests to verify they pass**

Run: `(cd web && npx vitest run src/features/delivery && npm run typecheck)`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add web/src/features/products/vocab.ts web/src/features/delivery docs/product/features/autopilot-backlog-runner.md
git commit -F /tmp/commit-task1.txt
```
with `/tmp/commit-task1.txt`:
```
feat(delivery): automation detail — Overview + Queue tabs on the page-tabs standard

The Status tab held description, oversight, actionable items, the products
table and two runner control surfaces. Overview keeps identity + the
actionable notice; Queue (Backlog Runner, PR Doctor only) takes the table,
custom dispatch, backlog groups and consolidation. ?tab=status falls back
to Overview.

Refs docs/PAGE-TABS.md
```

---

### Task 2: Product detail — Overview budget + Automation tab

**Files:**
- Modify: `web/src/features/roadmap/detail/ProductDetail.tsx`
- Modify: `web/src/features/roadmap/detail/ProductDetail.test.tsx`
- Modify: `docs/product/features/product-management-layer.md:206,348`

**Interfaces:**
- Consumes: `useTabParam`, `inFlightInitiatives`, `programTitleOf`, `AutomationPolicyCard({ product, project })`, `InitiativeTrack`, `Badge`, `Button`.
- Produces: product `TABS = ['overview', 'in-flight', 'needs-you', 'ranked', 'dependencies', 'automation']`; `OVERVIEW_PREVIEW_ROWS = 3`.

- [ ] **Step 1: Write the failing tests**

Read `AutomationPolicyCard.tsx:141-161` for the card's heading text (call it `<HEADING>` below; it is the `h2`/`h3` the card renders once `project` and the schema are present — with `usePolicySchema` mocked to `[]` in this file it still renders its heading; if it does not, mock `usePolicySchema` to return one field `{ key: 'dispatch', label: 'Dispatch', type: 'select', options: ['off','on'], default: 'off' }` matching `ConfigField` from `@/api/automations`). Add to `ProductDetail.test.tsx`:

```tsx
  it('Overview: previews at most three in-flight initiatives and links to the rest', () => {
    // Four in flight: the fixture's three plus one more leaf in an in-flight tier.
    const extra = { ...product.initiatives[0]!, slug: 'fourth', title: 'Fourth thing', hasChildren: false }
    render(
      <ViewportProvider kiosk={null}>
        <MemoryRouter initialEntries={['/roadmap/kindash']}>
          <ProductDetail product={{ ...product, initiatives: [...product.initiatives, extra] }} />
        </MemoryRouter>
      </ViewportProvider>,
    )
    const preview = screen.getByRole('region', { name: 'Initiatives in flight' })
    expect(within(preview).getAllByRole('link')).toHaveLength(3)
    fireEvent.click(screen.getByRole('button', { name: 'See all 4 in flight' }))
    expect(screen.getByRole('tab', { name: /In flight/, selected: true })).toBeInTheDocument()
  })

  it('Automation: the policy card lives on its own tab, not on the Overview', () => {
    mount()
    expect(screen.queryByText('<HEADING>')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: 'Automation' }))
    expect(screen.getByText('<HEADING>')).toBeInTheDocument()
  })
```

Import `within` from `@testing-library/react`. If the `InitiativeNode` fixture's first initiative is not in an in-flight tier, pick one that is (`inFlightInitiatives([product])[0]`) as the base for `extra`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `(cd web && npx vitest run src/features/roadmap/detail/ProductDetail.test.tsx)`
Expected: FAIL — no `Automation` tab; the preview region renders four links.

- [ ] **Step 3: Add the tab and shrink the Overview**

In `ProductDetail.tsx`:

```ts
const TABS = ['overview', 'in-flight', 'needs-you', 'ranked', 'dependencies', 'automation'] as const
/** The Overview previews this many in-flight initiatives; the In-flight tab
 *  lists them all (`docs/PAGE-TABS.md` §5). */
const OVERVIEW_PREVIEW_ROWS = 3
```

Tabs `items` gains, last: `{ id: 'automation', label: 'Automation' }`.

In the `overview` panel: delete the `<AutomationPolicyCard …/>` line. Replace the in-flight `<section>` with:

```tsx
            <section aria-label={`${INITIATIVE.plural} in flight`} className="flex flex-col gap-3">
              {inFlight.length === 0
                ? <p className="text-sm text-ink-muted">Nothing in flight.</p>
                : inFlight.slice(0, OVERVIEW_PREVIEW_ROWS).map((i) => {
                  const programTitle = programTitleFor(i)
                  return (
                    <div key={i.slug} className="rounded-md border border-edge bg-surface p-3">
                      <div className="flex flex-wrap items-center gap-2">
                        <Link to={initiativePath(i.slug)} className="font-medium text-ink hover:underline">
                          {i.title ?? i.slug}
                        </Link>
                        {programTitle && (
                          <Badge tone="neutral" title={`Part of ${programTitle}`} aria-label={`Part of ${programTitle}`}>
                            {programTitle}
                          </Badge>
                        )}
                      </div>
                      <InitiativeTrack stage={i.stage} status={i.status} epic={i.epic} />
                    </div>
                  )
                })}
              {inFlight.length > OVERVIEW_PREVIEW_ROWS && (
                <div>
                  <Button size="sm" variant="ghost" onClick={() => setTab('in-flight')}>
                    See all {inFlight.length} in flight
                  </Button>
                </div>
              )}
            </section>
```

`<section aria-label>` is exposed as `role="region"` — that is what the test queries. Add the panel:

```tsx
        {tab === 'automation' && (isLoading && !project
          ? <Skeleton lines={3} />
          : project && <AutomationPolicyCard product={product} project={project} />)}
```

Update the file's header comment to name the six tabs and cite `docs/PAGE-TABS.md`.

- [ ] **Step 4: Update the feature doc**

`docs/product/features/product-management-layer.md`: line 206 → "one product: overview, in flight, needs you, ranked, dependencies, automation."; line 348 → "It is edited from the product's **Automation** tab (`web/src/features/roadmap/detail/AutomationPolicyCard.tsx`, mounted by `ProductDetail.tsx` at `?tab=automation`),".

- [ ] **Step 5: Run the tests to verify they pass**

Run: `(cd web && npx vitest run src/features/roadmap && npm run typecheck)`
Expected: PASS, including the existing `'a phase in flight names the program…'` test (the fixture's in-flight count is ≤3, so the preview still shows `01 Restore`).

- [ ] **Step 6: Commit**

```bash
git add web/src/features/roadmap/detail docs/product/features/product-management-layer.md
git commit -F /tmp/commit-task2.txt
```
`/tmp/commit-task2.txt`:
```
feat(roadmap): product detail — Overview on budget, policy card on an Automation tab

Overview held eight blocks including the 299-line policy editor and a
full copy of the In-flight list. It now keeps identity + self-hiding
notices + a three-row in-flight preview with "See all"; the policy card is
the product's Automation tab.

Refs docs/PAGE-TABS.md
```

---

### Task 3: Session body — `ui/Tabs` + `?view=`

**Files:**
- Modify: `web/src/features/sessions/SessionBody.tsx:9-12,52,116-156,293-311`
- Modify: `web/src/features/sessions/SessionBody.browser.test.tsx`
- Modify (router wrapper only, if their renders throw without one): `SessionBody.test.tsx`, `SessionBody.modelSwitch.test.tsx`, `SessionBody.condensing.test.tsx`, `MainPaneSession.meta.test.tsx`, `MainPaneSession.actions.test.tsx` in `web/src/features/sessions/`, and `web/src/shell/sessionWorkspace.test.tsx`.

**Interfaces:**
- Consumes: `Tabs` (`@/ui/Tabs`), `useSearchParamState`.
- Produces: `SESSION_VIEWS = ['conversation', 'browser'] as const`; the `?view=` param.

- [ ] **Step 1: Write the failing tests**

In `SessionBody.browser.test.tsx`, wrap every render in a router. Change `renderWithQuery`:

```tsx
import { MemoryRouter } from 'react-router-dom'

function renderWithQuery(ui: React.ReactElement, path = '/sessions/s1') {
  return render(
    <QueryClientProvider client={makeQueryClient()}>
      <MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  )
}
```

Add two tests to the describe block:

```tsx
  it('the strip is the shared Tabs primitive: roving tabindex, ?view=browser deep-links', async () => {
    vi.spyOn(global, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ active_displays: 1, total_sessions: 1 }), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      }),
    )
    renderWithQuery(<SessionBody rt={makeRuntime()} />, '/sessions/s1?view=browser')
    await waitFor(() => expect(screen.getByRole('tab', { name: /browser/i })).toHaveAttribute('aria-selected', 'true'))
    expect(screen.getByRole('tab', { name: /conversation/i })).toHaveAttribute('tabindex', '-1')
    expect(screen.getByTestId('browser-tab-stub')).toBeInTheDocument()
  })

  it('?view=browser is ignored while the Browser view is unavailable', async () => {
    vi.spyOn(global, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ enabled: false }), { status: 200, headers: { 'Content-Type': 'application/json' } }),
    )
    renderWithQuery(<SessionBody rt={makeRuntime()} />, '/sessions/s1?view=browser')
    await waitFor(() => expect(screen.queryByRole('tab', { name: /browser/i })).toBeNull())
    expect(screen.queryByTestId('browser-tab-stub')).toBeNull()
  })
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `(cd web && npx vitest run src/features/sessions/SessionBody.browser.test.tsx)`
Expected: the two new tests FAIL (`tabindex` missing; the deep link is ignored).

- [ ] **Step 3: Swap the tablist for `ui/Tabs` and store the view in the URL**

Imports: add `import { Tabs } from '@/ui/Tabs'` and `import { useSearchParamState } from '@/lib/useSearchParamState'`. Below the imports:

```ts
/** The session's two views (`docs/PAGE-TABS.md` §2: a Workspace view toggle,
 *  stored in `?view=` rather than `?tab=` because it is not the page's
 *  partition by question). */
const SESSION_VIEWS = ['conversation', 'browser'] as const
type SessionView = (typeof SESSION_VIEWS)[number]
```

Replace the `activeTab` state line with:

```ts
  const [view, setView] = useSearchParamState<SessionView>('view', SESSION_VIEWS, 'conversation')
```

Replace the hand-rolled strip (`{browserEnabled && (<div role="tablist" …>…</div>)}`) with:

```tsx
      {/* Conversation | Browser — only rendered when the X display is available. */}
      {browserEnabled && (
        <Tabs
          label="Session view"
          items={[{ id: 'conversation', label: 'Conversation' }, { id: 'browser', label: 'Browser' }]}
          value={view}
          onChange={(id) => setView(id as SessionView)}
          panelId={(id) => `tab-panel-${id}`}
          className="shrink-0 border-b border-edge bg-canvas px-2"
        />
      )}
```

Change `if (browserEnabled && activeTab === 'browser')` to `if (browserEnabled && view === 'browser')`. The panel ids and `aria-labelledby` values already match what `Tabs` emits (`tab-conversation`, `tab-browser`). Remove the now-unused `cn` import only if nothing else in the file uses it (it does — keep it).

- [ ] **Step 4: Run every suite that mounts the session body**

Run: `(cd web && npx vitest run src/features/sessions src/shell/sessionWorkspace.test.tsx)`
Expected: any file that renders `SessionBody` without a router fails with `useSearchParams() may be used only in the context of a <Router>`. For each such file, wrap that file's render in `<MemoryRouter initialEntries={['/sessions/s1']}>` (inside the existing providers). Do not touch files that already pass. Re-run until green.

- [ ] **Step 5: Typecheck and lint**

Run: `(cd web && npm run typecheck && npm run lint)`
Expected: clean (`--max-warnings 0`).

- [ ] **Step 6: Commit**

```bash
git add web/src/features/sessions web/src/shell/sessionWorkspace.test.tsx
git commit -F /tmp/commit-task3.txt
```
`/tmp/commit-task3.txt`:
```
refactor(sessions): session view strip on ui/Tabs, view in ?view=

The Conversation | Browser strip was the only role="tablist" outside the
shared primitive (constitution VI), with its state in useState. It now
renders through ui/Tabs (roving tabindex, arrow keys) and deep-links via
?view=browser, ignored while the X display is unavailable.

Refs docs/PAGE-TABS.md
```

---

### Task 4: Guard — `ui/Tabs.tsx` is the only tablist

**Files:**
- Create: `web/src/__tests__/tablistSingleSource.test.ts`

**Interfaces:**
- Consumes: nothing from earlier tasks except that Task 3 removed the last violation.

- [ ] **Step 1: Write the test**

```ts
// @vitest-environment node
import { readdirSync, readFileSync, statSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, it, expect } from 'vitest'

/**
 * `docs/PAGE-TABS.md` §7: `web/src/ui/Tabs.tsx` is the ONLY component that
 * renders `role="tablist"`. A second tablist is a parallel implementation
 * (constitution VI) that drifts from the primitive's keyboard model and
 * badge semantics — the session body carried one for months before the
 * standard caught it. Tests are excluded: a test may assert the role by
 * name. Paths resolve from THIS FILE via `import.meta.url`, the rule shared
 * by every guard in this directory.
 */
const WEB_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')
const SRC_ROOT = path.resolve(WEB_ROOT, 'src')
const ALLOWED = path.join(SRC_ROOT, 'ui', 'Tabs.tsx')
const SKIP_DIRS = new Set(['node_modules', 'dist'])

function sourceFiles(dir: string): string[] {
  const out: string[] = []
  for (const name of readdirSync(dir)) {
    if (SKIP_DIRS.has(name)) continue
    const full = path.join(dir, name)
    if (statSync(full).isDirectory()) out.push(...sourceFiles(full))
    else if (/\.tsx?$/.test(name) && !/\.test\.tsx?$/.test(name)) out.push(full)
  }
  return out
}

// JSX attribute, object-literal prop, and `role: 'tablist' as const` shapes.
const TABLIST = /role\s*[:=]\s*["'`]tablist["'`]/

export function tablistOffenders(files: string[] = sourceFiles(SRC_ROOT)): string[] {
  const hits: string[] = []
  for (const file of files) {
    if (file === ALLOWED) continue
    const text = readFileSync(file, 'utf-8')
    const lines = text.split('\n')
    lines.forEach((line, i) => {
      if (TABLIST.test(line)) hits.push(`${path.relative(WEB_ROOT, file)}:${i + 1}`)
    })
  }
  return hits
}

describe('one tablist primitive', () => {
  it('no role="tablist" outside ui/Tabs.tsx', () => {
    const hits = tablistOffenders()
    expect(hits, `render tabs through ui/Tabs (docs/PAGE-TABS.md §7):\n${hits.join('\n')}`).toEqual([])
  })

  it('the scan sees a tablist when one exists', () => {
    // The primitive itself is the fixture: it must match, or the guard is blind.
    expect(readFileSync(ALLOWED, 'utf-8')).toMatch(TABLIST)
  })
})
```

- [ ] **Step 2: Run it**

Run: `(cd web && npx vitest run src/__tests__/tablistSingleSource.test.ts)`
Expected: PASS (Task 3 removed the only offender). If it lists `SessionBody.tsx`, Task 3 is incomplete — fix there, not here.

- [ ] **Step 3: Commit**

```bash
git add web/src/__tests__/tablistSingleSource.test.ts
git commit -m "test(web): guard — ui/Tabs is the only role=tablist (docs/PAGE-TABS.md §7)"
```

---

### Task 5: Full gate and PR

- [ ] **Step 1: Full web gate**

Run: `(cd web && npm run typecheck && npm run lint && npm run test 2>&1 | tee /tmp/page-tabs-web-test.log | tail -20 && npm run build)`
Expected: all four green. `npm run test` is the FULL suite (shared-hook mocks in ~47 files).

- [ ] **Step 2: Brand ratchet**

Run: `/home/tony/GitHub/viberpit/.venv/bin/python scripts/brand_ratchet.py`
Expected: no new hits (the new docs and code say ViberPit).

- [ ] **Step 3: Push and open the PR** (branch already created off `origin/master` before Task 1; non-draft; `gh pr merge --squash --auto`).
