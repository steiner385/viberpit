# Settings Tab Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the top-level Accounts, Automation, and Library tabs plus the Settings modal with a single top-level Settings tab (sections: General | Accounts | Automation | Plugins | Skills | Commands | MCP Servers) in both shells.

**Architecture:** A new `SettingsView` component owns an inner section tab strip driven by the `?section=` search param (the pattern `LibraryView` established). The old modal form body becomes an inline `GeneralSection`. The canonical tab model (`tabDefs.ts`) shrinks to five tabs; both shells inherit it. Old routes redirect.

**Tech Stack:** React 19 + TS, react-router-dom, TanStack Query, Tailwind, vitest + testing-library, Playwright (e2e).

**Spec:** `docs/superpowers/specs/2026-07-12-settings-tab-consolidation-design.md`

## Global Constraints

- Working dir: repo root; web commands run in `web/` (`npx vitest run <file>`, `npx tsc --noEmit`, `npx eslint src`).
- Branch: `feat/settings-tab-consolidation` (already created off origin/master). Commit after each task; end commit messages with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- Every commit must keep `npx tsc --noEmit` green (type coupling between shells forces Task 2 to touch both).
- Purge replaced code in the same PR: `SettingsPanel.tsx` and `LibraryView.tsx` (and their test files) must be deleted by the end of Task 2 — no orphaned modules.
- Section ids are exactly: `general`, `accounts`, `automation`, `plugins`, `skills`, `commands`, `mcp` (library values preserved for deep-link compat).
- `TabId` = `'sessions' | 'pipeline' | 'archived' | 'insights' | 'settings'` everywhere.

---

### Task 1: `GeneralSection` + `SettingsView` (new components + tests)

New files only — nothing existing breaks. `SettingsPanel`/`LibraryView` still exist and are deleted in Task 2.

**Files:**
- Create: `web/src/components/settings/GeneralSection.tsx`
- Create: `web/src/components/settings/SettingsView.tsx`
- Create: `web/src/components/settings/__tests__/GeneralSection.test.tsx`
- Create: `web/src/components/settings/__tests__/SettingsView.test.tsx`

**Interfaces:**
- Consumes: `useSettings`, `useUpdateSettings`, `AppSettings` from `@/api/hooks`; `PERMISSION_MODES` from `@/session/permissionModes`; `NotificationsToggle` from `./NotificationsToggle`; `AccountsManager` from `../AccountsManager`; `AutomationView` from `../AutomationView`; `PluginsSection`/`SkillsSection`/`CommandsSection`/`McpSection` from `../library/*`.
- Produces: `export function GeneralSection(): JSX.Element` (no props); `export function SettingsView(props: { onOpenSession?: (id: string) => void; onReviewSession?: (id: string) => void }): JSX.Element`; `export type SettingsSection = 'general' | 'accounts' | 'automation' | 'plugins' | 'skills' | 'commands' | 'mcp'`.

- [ ] **Step 1: Write `GeneralSection.tsx`**

Move the form body out of `SettingsPanel.tsx` (`web/src/components/settings/SettingsPanel.tsx` — read it for the exact JSX), dropping the overlay, focus trap, Escape handler, Close button, and `onClose` prop. Keep: the loaded/loading remount key trick, all four fields, the full-auto warning, `NotificationsToggle`, and the Save button (shows "Saving…"; on success show a `toast('Settings saved')` from `sonner` instead of closing).

```tsx
/**
 * GeneralSection — the app-wide settings form, inline in the Settings tab
 * (formerly the SettingsPanel modal).
 */
import { useState } from 'react'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import { useSettings, useUpdateSettings, type AppSettings } from '@/api/hooks'
import { PERMISSION_MODES } from '@/session/permissionModes'
import { NotificationsToggle } from './NotificationsToggle'
import { toast } from 'sonner'

export function GeneralSection() {
  const { data: settings } = useSettings()
  // Remount the form when settings transition absent → present so it
  // initializes from server values via useState (not setState-in-effect).
  return <GeneralForm key={settings ? 'loaded' : 'loading'} settings={settings} />
}

function GeneralForm({ settings }: { settings: AppSettings | undefined }) {
  const updateSettings = useUpdateSettings()
  const [permissionMode, setPermissionMode] = useState(settings?.defaultPermissionMode ?? 'interactive')
  const [thinkingCollapsed, setThinkingCollapsed] = useState(settings?.thinkingCollapsed ?? false)
  const [repoRoot, setRepoRoot] = useState(settings?.repoRoot ?? '')
  const [toolDetail, setToolDetail] = useState<AppSettings['defaultToolDetail']>(settings?.defaultToolDetail ?? 'auto')

  function handleSave() {
    updateSettings.mutate({
      defaultPermissionMode: permissionMode,
      thinkingCollapsed,
      repoRoot: repoRoot.trim() || null,
      defaultToolDetail: toolDetail,
    }, { onSuccess: () => toast('Settings saved') })
  }

  const inputClass = cn(
    'w-full rounded-md border border-gray-300 dark:border-gray-600',
    'bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100',
    'px-3 py-2 text-sm min-h-[44px]',
    'focus:outline-none focus:ring-2 focus:ring-blue-500',
    'placeholder:text-gray-400 dark:placeholder:text-gray-500',
  )

  return (
    <div className="space-y-4 max-w-md">
      {/* … the exact field JSX from SettingsPanel.tsx lines 112–209: default
          tool approvals select + full-auto warning, transcript display
          (tool-calls select + thinking checkbox), repo clone root input,
          notifications section — copied verbatim … */}
      <div className="flex justify-end pt-2">
        <Button type="button" onClick={handleSave} disabled={updateSettings.isPending} className="min-h-[44px]">
          {updateSettings.isPending ? 'Saving…' : 'Save'}
        </Button>
      </div>
    </div>
  )
}
```

(The comment block above is a placeholder in THIS PLAN only because the JSX is 100 lines; the implementer copies lines 112–209 of `SettingsPanel.tsx` verbatim — same ids `settings-permission-mode`, `settings-tool-detail`, `settings-thinking-collapsed`, `settings-repo-root`.)

- [ ] **Step 2: Write `SettingsView.tsx`**

Same strip pattern as `web/src/components/library/LibraryView.tsx` (read it first — it is deleted in Task 2, this supersedes it):

```tsx
/**
 * SettingsView — the consolidated Settings tab: app settings plus the
 * accounts, automation, and library (plugins/skills/commands/MCP) managers.
 * Section state lives in ?section= so deep links work in both shells.
 * Library section ids (plugins|skills|commands|mcp) are preserved so old
 * /library?section=X links redirect losslessly.
 */
import { useSearchParams } from 'react-router-dom'
import { GeneralSection } from './GeneralSection'
import { AccountsManager } from '../AccountsManager'
import { AutomationView } from '../AutomationView'
import { PluginsSection } from '../library/PluginsSection'
import { SkillsSection } from '../library/SkillsSection'
import { CommandsSection } from '../library/CommandsSection'
import { McpSection } from '../library/McpSection'

export type SettingsSection =
  | 'general' | 'accounts' | 'automation'
  | 'plugins' | 'skills' | 'commands' | 'mcp'

const SECTIONS: { id: SettingsSection; label: string }[] = [
  { id: 'general', label: 'General' },
  { id: 'accounts', label: 'Accounts' },
  { id: 'automation', label: 'Automation' },
  { id: 'plugins', label: 'Plugins' },
  { id: 'skills', label: 'Skills' },
  { id: 'commands', label: 'Commands' },
  { id: 'mcp', label: 'MCP Servers' },
]

export function SettingsView({ onOpenSession, onReviewSession }: {
  onOpenSession?: (id: string) => void
  onReviewSession?: (id: string) => void
} = {}) {
  const [params, setParams] = useSearchParams()
  const raw = params.get('section')
  const section: SettingsSection = SECTIONS.some((s) => s.id === raw) ? (raw as SettingsSection) : 'general'

  return (
    <div className="max-w-3xl mx-auto w-full">
      <div role="tablist" aria-label="Settings sections" className="flex gap-1 mb-4 border-b border-[var(--border)] overflow-x-auto">
        {SECTIONS.map((s) => (
          <button
            key={s.id}
            role="tab"
            aria-selected={section === s.id}
            className={`px-3 py-2 text-sm rounded-t-md min-h-[44px] whitespace-nowrap ${
              section === s.id ? 'font-semibold border-b-2 border-current' : 'opacity-70 hover:opacity-100'
            }`}
            onClick={() => setParams((p) => { p.set('section', s.id); return p }, { replace: true })}
          >
            {s.label}
          </button>
        ))}
      </div>
      {section === 'general' && <GeneralSection />}
      {section === 'accounts' && <AccountsManager />}
      {section === 'automation' && <AutomationView onOpenSession={onOpenSession} onReviewSession={onReviewSession} />}
      {section === 'plugins' && <PluginsSection />}
      {section === 'skills' && <SkillsSection />}
      {section === 'commands' && <CommandsSection />}
      {section === 'mcp' && <McpSection />}
    </div>
  )
}
```

Also in this step: remove the now-wrong top-level tabpanel wiring inside `AutomationView` (`web/src/components/AutomationView.tsx`) — change its root `<div className="space-y-3" role="tabpanel" id="tabpanel-automation" aria-labelledby="tab-automation">` to plain `<div className="space-y-3">` (it is a section now, not a top-level tab panel; the shells own panel semantics).

- [ ] **Step 3: Write failing tests**

`web/src/components/settings/__tests__/GeneralSection.test.tsx` — port the non-modal cases from `web/src/components/settings/__tests__/SettingsPanel.test.tsx` (same `vi.mock('@/api/hooks', …)` block and `SETTINGS`/`NOOP_MUTATION` fixtures — copy them):

```tsx
// …same mocks/fixtures/wrapper as SettingsPanel.test.tsx, minus onClose…
import { GeneralSection } from '../GeneralSection'

describe('GeneralSection', () => {
  beforeEach(() => { /* same useSettings/useUpdateSettings mock setup */ })

  it('renders inline (no dialog role)', () => {
    render(<GeneralSection />, { wrapper })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.getByLabelText(/default tool approvals/i)).toBeInTheDocument()
  })

  it('pre-fills fields from settings', () => {
    render(<GeneralSection />, { wrapper })
    expect((screen.getByLabelText(/default tool approvals/i) as HTMLSelectElement).value).toBe('interactive')
    expect((screen.getByLabelText(/repo clone root/i) as HTMLInputElement).value).toBe('/home/user/repos')
    expect((screen.getByLabelText(/collapse thinking by default/i) as HTMLInputElement).checked).toBe(false)
  })

  it('shows the full-auto warning only when full-auto is selected', async () => {
    const user = userEvent.setup()
    render(<GeneralSection />, { wrapper })
    expect(screen.queryByText(/full auto lets sessions run/i)).not.toBeInTheDocument()
    await user.selectOptions(screen.getByLabelText(/default tool approvals/i), 'full-auto')
    expect(screen.getByText(/full auto lets sessions run/i)).toBeInTheDocument()
  })

  it('saves changed fields', async () => {
    const mutate = vi.fn()
    vi.mocked(hooks.useUpdateSettings).mockReturnValue({ ...NOOP_MUTATION, mutate } as unknown as ReturnType<typeof hooks.useUpdateSettings>)
    const user = userEvent.setup()
    render(<GeneralSection />, { wrapper })
    await user.selectOptions(screen.getByLabelText(/default tool approvals/i), 'full-auto')
    await user.click(screen.getByRole('button', { name: /save/i }))
    expect(mutate).toHaveBeenCalledWith(
      expect.objectContaining({ defaultPermissionMode: 'full-auto', repoRoot: '/home/user/repos' }),
      expect.anything(),
    )
  })
})
```

`web/src/components/settings/__tests__/SettingsView.test.tsx` — model on `web/src/components/library/LibraryView.test.tsx` (copy its `vi.mock('@/api/client', …)` block), render inside `MemoryRouter` + `QueryClientProvider`, and also mock `@/api/hooks` (GeneralSection needs `useSettings`/`useUpdateSettings`; AutomationView needs `useAutomations`) and `@/api/sessionsViewing`:

```tsx
// mocks: @/api/client (library endpoints, from LibraryView.test.tsx),
// @/api/hooks (useSettings, useUpdateSettings, useAutomations, plus the
// AccountsManager hooks: useAccounts, useSessions, useRefreshAccount,
// useUpdateAccount, useDeleteAccount, useReauthAccount, useReauthStatus,
// useCancelReauth, useSubmitReauthCode, useAddCopilotAccount,
// useCopilotAddStatus, useCancelCopilotAdd, QK)
import { SettingsView } from '../SettingsView'

describe('SettingsView', () => {
  it('defaults to the General section', async () => {
    renderView()  // MemoryRouter initialEntries=['/settings']
    expect(await screen.findByLabelText(/default tool approvals/i)).toBeInTheDocument()
  })

  it('honors ?section= deep links', async () => {
    renderView('/settings?section=plugins')
    expect(await screen.findByText('alpha')).toBeInTheDocument()  // plugin fixture
  })

  it('switches sections via the strip', async () => {
    renderView()
    await userEvent.click(screen.getByRole('tab', { name: /skills/i }))
    expect(await screen.findByText('my-skill')).toBeInTheDocument()
  })

  it('renders all seven section tabs', () => {
    renderView()
    for (const name of [/general/i, /accounts/i, /automation/i, /plugins/i, /skills/i, /commands/i, /mcp servers/i]) {
      expect(screen.getByRole('tab', { name })).toBeInTheDocument()
    }
  })
})
```

- [ ] **Step 4: Run the new tests + typecheck**

Run (in `web/`): `npx vitest run src/components/settings/__tests__/GeneralSection.test.tsx src/components/settings/__tests__/SettingsView.test.tsx && npx tsc --noEmit`
Expected: PASS (components were written in Steps 1–2; strict TDD red-first is impractical here since components and tests land together — verify tests fail if you comment out a section mapping, then restore).

- [ ] **Step 5: Commit**

```bash
git add web/src/components/settings web/src/components/AutomationView.tsx
git commit -m "feat(web): SettingsView + inline GeneralSection (settings tab foundation)"
```

---

### Task 2: Tab model + both shells rewired; purge modal + LibraryView wrapper

**Files:**
- Modify: `web/src/components/tabDefs.ts`
- Modify: `web/src/components/Dashboard.tsx`
- Modify: `web/src/components/DesktopShell.tsx`
- Delete: `web/src/components/settings/SettingsPanel.tsx`, `web/src/components/settings/__tests__/SettingsPanel.test.tsx`, `web/src/components/library/LibraryView.tsx`, `web/src/components/library/LibraryView.test.tsx`
- Modify (tests): `web/src/components/__tests__/DashboardTabs.test.tsx`, `web/src/components/__tests__/DashboardSettings.test.tsx`, `web/src/components/__tests__/DesktopShell.test.tsx`

**Interfaces:**
- Consumes: `SettingsView` from Task 1.
- Produces: `TabId = 'sessions' | 'pipeline' | 'archived' | 'insights' | 'settings'` (exported from `Dashboard.tsx`); `TabCounts { sessions?, pipeline?, archived?, settingsAttention? }`; Dashboard prop `dialog?: 'new' | null` and `onDialogChange?: (dialog: 'new' | null) => void`.

- [ ] **Step 1: `tabDefs.ts`**

```ts
export const TAB_DEFS: { id: TabId; label: string }[] = [
  { id: 'sessions', label: 'Sessions' },
  { id: 'pipeline', label: 'Pipeline' },
  { id: 'archived', label: 'Archived' },
  { id: 'insights', label: 'Insights' },
  { id: 'settings', label: 'Settings' },
]

export interface TabCounts {
  sessions?: number
  pipeline?: number
  archived?: number
  /** Amber dot on Settings — an account needs re-auth (Accounts section). */
  settingsAttention?: boolean
}

export function buildTabs(counts: TabCounts): TabDef[] {
  return TAB_DEFS.map((t) => ({
    ...t,
    count: counts[t.id as keyof TabCounts] as number | undefined,
    attention: t.id === 'settings' ? counts.settingsAttention : undefined,
  }))
}
```

- [ ] **Step 2: `Dashboard.tsx`**

- `export type TabId = 'sessions' | 'pipeline' | 'archived' | 'insights' | 'settings'`.
- Props: `dialog?: 'new' | null`, `onDialogChange?: (dialog: 'new' | null) => void` (drop `'settings'` from the union).
- Remove imports: `SettingsPanel`, `AccountsManager`, `ArchivedManager` stays, `AutomationView`, `LibraryView`. Add `SettingsView` import.
- Remove state `showSettings`, `settingsOpen`, `settingsButtonRef` focus restore; the header gear button becomes a tab shortcut:

```tsx
<Button size="sm" variant="outline" className="min-h-[44px]"
  onClick={() => setActiveTab('settings')} aria-label="Settings">
  <Settings size={14} aria-hidden="true" className="mr-1" />
  Settings
</Button>
```

- `buildTabs({ sessions: orderedSessions.length, pipeline: inFlightPrCount(prs), archived: archivedSessions.length, settingsAttention: hasNeedsReauth })`.
- Replace the accounts/automation/library panels with one settings panel (own scroll region, like siblings):

```tsx
{activeTab === 'settings' && (
  <div
    id="tabpanel-settings"
    role="tabpanel"
    aria-labelledby="tab-settings"
    className="flex-1 min-h-0 overflow-y-auto pb-4 px-4"
  >
    <SettingsView
      onOpenSession={onOpenSession ? (id) => onOpenSession(id) : undefined}
      onReviewSession={onReviewSession ? handleReview : undefined}
    />
  </div>
)}
```

- Remove the `{settingsOpen && <SettingsPanel …/>}` mount.
- Toast copy: `'No usable accounts — check Settings → Accounts to re-authenticate.'`
- `useAccounts` is still needed (session list, reauth attention); `AccountsManager` import goes away.

- [ ] **Step 3: `DesktopShell.tsx`**

- Local `type TabId` → same 5-value union.
- Remove imports `SettingsPanel`, `AccountsManager`, `AutomationView`, `LibraryView`; add `SettingsView`.
- Remove `showSettings` state and modal mount; gear icon `onClick={() => setTab('settings')}`; drop `settingsButtonRef` (no focus restore needed) — keep the button.
- `mainPane` branches: delete `accounts`/`automation`/`library`; add:

```tsx
} else if (tab === 'settings') {
  mainPane = <div className="p-4 overflow-y-auto h-full"><SettingsView onOpenSession={focusSession} /></div>
}
```

- `buildTabs({ sessions: …, pipeline: …, archived: …, settingsAttention: hasNeedsReauth })`.
- Toast copy: `'No usable accounts — check Settings → Accounts to re-authenticate.'`

- [ ] **Step 4: Delete the replaced files**

```bash
git rm web/src/components/settings/SettingsPanel.tsx \
       web/src/components/settings/__tests__/SettingsPanel.test.tsx \
       web/src/components/library/LibraryView.tsx \
       web/src/components/library/LibraryView.test.tsx
```

Then `grep -rn "SettingsPanel\|LibraryView" web/src` — expect zero hits (except SettingsView's own name).

- [ ] **Step 5: Update the three component test files**

All three now need a `MemoryRouter` wrapper (SettingsView uses `useSearchParams`) — wrap the existing `QueryClientProvider` wrapper's children in `<MemoryRouter>`.

`DashboardTabs.test.tsx`: the "Sessions/Accounts tabs" suite becomes "Sessions/Settings tabs":
- "renders both Sessions and Accounts tabs" → asserts `tab` named /sessions/i and /settings/i.
- "clicking Accounts tab switches to the Accounts panel" → click `/settings/i` tab, then click the inner `role="tab"` named /accounts/i in the "Settings sections" strip, assert account name "work" visible. (Query the top strip via `within(screen.getByRole('tablist', { name: /dashboard sections/i }))` to avoid colliding with the inner strip.)
- Arrow-key tests: ArrowRight from Sessions now lands on Pipeline — update names.
- needs-reauth badge tests: expect `tab` named /settings.*needs attention/i.
- aria-controls test: unchanged logic (sessions panel), still passes.
- Add hook mocks the settings sections need when mounted (`useAutomations` already mocked in DashboardSettings-style files; add `useSubmitReauthCode`, `useAddCopilotAccount`, `useCopilotAddStatus`, `useCancelCopilotAdd` if missing — check the file's existing mock list and extend).

`DashboardSettings.test.tsx`: the three dialog tests become tab tests:

```tsx
it('renders a Settings button', () => { /* unchanged */ })

it('switches to the Settings tab when the Settings button is clicked', async () => {
  const user = userEvent.setup()
  render(<Dashboard />, { wrapper })
  await user.click(screen.getByRole('button', { name: /^settings$/i }))
  const within_ = within(screen.getByRole('tablist', { name: /dashboard sections/i }))
  expect(within_.getByRole('tab', { name: /settings/i })).toHaveAttribute('aria-selected', 'true')
  // General section form renders inline
  expect(screen.getByLabelText(/default tool approvals/i)).toBeInTheDocument()
})
```

(Drop the "closes the Settings dialog" test — there is no dialog. Keep the New-Session-dialog tool-approvals tests unchanged.) The `@/api/hooks` mock in this file needs `useSubmitReauthCode` added (SettingsView → AccountsManager imports it) — mirror the DashboardTabs mock entries.

`DesktopShell.test.tsx`:
- "Accounts tab renders the accounts manager in the main pane" → click `/settings/i` top tab, then inner /accounts/i section tab, assert `heading /^accounts$/i`.
- Parity test: expect `/insights/i` and `/settings/i` tabs present; automation is now a section (optionally assert it inside SettingsView).
- Wrap renders in `MemoryRouter`; extend the hooks mock as above.
- Update the reauth-toast test string if it asserts the toast copy.

- [ ] **Step 6: Run tests + typecheck + lint**

Run (in `web/`):
`npx vitest run src/components/__tests__/DashboardTabs.test.tsx src/components/__tests__/DashboardSettings.test.tsx src/components/__tests__/DesktopShell.test.tsx src/components/settings && npx tsc --noEmit && npx eslint src`
Expected: PASS, no type or lint errors.

- [ ] **Step 7: Commit**

```bash
git add -A web/src
git commit -m "feat(web): consolidate Accounts/Automation/Library tabs + settings modal into a Settings tab"
```

---

### Task 3: Routing — `/settings` route, back-compat redirects, dialog param

**Files:**
- Modify: `web/src/App.tsx`
- Modify: `web/src/App.test.tsx`, `web/src/App.dialogs.test.tsx`

**Interfaces:**
- Consumes: `TabId` (5-value) from `Dashboard.tsx`.
- Produces: routes `/settings` (+ redirects from `/accounts`, `/automation`, `/library`).

- [ ] **Step 1: `App.tsx`**

- In `DashboardRoute`: `const dialog = rawDialog === 'new' ? 'new' : null`, and redirect legacy settings-dialog links before rendering:

```tsx
if (rawDialog === 'settings') return <Navigate to="/settings" replace />
```

- `onDialogChange` type follows Dashboard's (`'new' | null`).
- Routes:

```tsx
<Route path="/sessions" element={<DashboardRoute tab="sessions" />} />
<Route path="/pipeline" element={<DashboardRoute tab="pipeline" />} />
<Route path="/archived" element={<DashboardRoute tab="archived" />} />
<Route path="/insights" element={<DashboardRoute tab="insights" />} />
<Route path="/settings" element={<DashboardRoute tab="settings" />} />
{/* Legacy top-level tabs — now Settings sections. */}
<Route path="/accounts" element={<Navigate to="/settings?section=accounts" replace />} />
<Route path="/automation" element={<Navigate to="/settings?section=automation" replace />} />
<Route path="/library" element={<LibraryRedirect />} />
```

with, above `DashboardRoute`:

```tsx
// /library?section=X → /settings?section=X (section ids were preserved).
function LibraryRedirect() {
  const [params] = useSearchParams()
  const section = params.get('section') ?? 'plugins'
  return <Navigate to={`/settings?section=${section}`} replace />
}
```

- Update the route comment block at the top of the file to describe the new map.

- [ ] **Step 2: `App.test.tsx`**

- "deep-links to the accounts tab at /accounts" → renderAt('/accounts'), expect the Settings top tab selected AND the accounts section active:

```tsx
it('redirects /accounts to the Settings tab accounts section', async () => {
  renderAt('/accounts')
  await waitFor(() => {
    const strip = screen.getByRole('tablist', { name: /dashboard sections/i })
    expect(within(strip).getByRole('tab', { name: /settings/i })).toHaveAttribute('aria-selected', 'true')
  })
  expect(await screen.findByRole('tablist', { name: /settings sections/i })).toBeInTheDocument()
})
```

- "clicking the Accounts tab updates the URL to /accounts" → click the Settings tab, expect pathname `/settings`.
- Scan the file for other `/accounts` `/library` `/automation` route usages and update similarly.

- [ ] **Step 3: `App.dialogs.test.tsx`**

Replace the `?dialog=settings` dialog test with a redirect test:

```tsx
it('redirects legacy ?dialog=settings to the Settings tab', async () => {
  renderAt('/sessions?dialog=settings')
  await waitFor(() => {
    expect(screen.getByTestId('search')).not.toHaveTextContent('dialog=settings')
  })
  expect(await screen.findByRole('tablist', { name: /settings sections/i })).toBeInTheDocument()
})
```

(The `LocationProbe` in that file only shows `location.search`; add a pathname probe if needed: `<div data-testid="pathname">{location.pathname}</div>`.)

- [ ] **Step 4: Run tests + typecheck**

Run (in `web/`): `npx vitest run src/App.test.tsx src/App.dialogs.test.tsx && npx tsc --noEmit`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/App.tsx web/src/App.test.tsx web/src/App.dialogs.test.tsx
git commit -m "feat(web): /settings route + legacy tab and dialog redirects"
```

---

### Task 4: e2e spec + full verification

**Files:**
- Modify: `web/e2e/dashboard.spec.ts:163-198` (the "mobile: all header tabs are reachable" test)

- [ ] **Step 1: Update the e2e tab assertions**

```ts
// All five sections render as tabs in the strip — nothing is dropped on a
// narrow viewport.
const tablist = page.getByRole('tablist', { name: /dashboard sections/i })
await expect(tablist.getByRole('tab')).toHaveCount(5)
for (const name of [/sessions/i, /pipeline/i, /archived/i, /insights/i, /settings/i]) {
  await expect(tablist.getByRole('tab', { name })).toHaveCount(1)
}

// Count badges follow the "Archived 111" pattern: 1 live session.
await expect(tablist.getByRole('tab', { name: /sessions \(1\)/i })).toBeVisible()
```

(Drop the `accounts \(1\)` badge line.) The off-screen-tab reachability check clicks the LAST tab in the strip — update `/insights/i` → `/settings/i` there so it still exercises the strip's end:

```ts
const settings = tablist.getByRole('tab', { name: /settings/i })
await settings.scrollIntoViewIfNeeded()
await settings.click()
await expect(settings).toHaveAttribute('aria-selected', 'true')
```

Grep the whole e2e dir for other `/accounts|automation|library/` tab clicks and update: `grep -rn "tab.*accounts\|tab.*automation\|tab.*library" web/e2e/`.

- [ ] **Step 2: Full web verification**

Run (in `web/`): `npx vitest run && npx tsc --noEmit && npx eslint src`
Expected: entire suite PASS, 0 type errors, 0 lint errors. (e2e runs in CI only — it is not a required check, but keep the spec consistent.)

- [ ] **Step 3: Commit**

```bash
git add web/e2e/dashboard.spec.ts
git commit -m "test(e2e): five-tab strip after settings consolidation"
```

---

### Task 5: PR

- [ ] Push and open the PR per the repo completion workflow:

```bash
git push -u origin feat/settings-tab-consolidation
gh pr create --title "feat(web): consolidate Accounts/Automation/Library + settings modal into a Settings tab" --body "…summary + test plan…

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
gh pr merge --squash --auto
```
