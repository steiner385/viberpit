# Persist tool-fold as a Settings default — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the session transcript's tool-fold mode from ephemeral per-session state to a persisted `AppSettings` default controlled only from the Settings panel; remove the in-session toggle; and wire up the currently-dead `thinkingCollapsed` setting, grouping both under a "Transcript display" section.

**Architecture:** Reuse the existing server-backed `AppSettings` singleton + `/api/settings` + React Query `useSettings`/`useUpdateSettings`. Add a nullable `default_tool_detail` column. The runtime derives `toolDetailMode` (and `thinkingCollapsed`) live from settings instead of holding local state, giving cross-session propagation for free. The top-bar button and `c` keyboard shortcut are removed; per-`ToolGroup` header clicks remain the in-session granular control.

**Tech Stack:** Python FastAPI + SQLAlchemy (SQLite, async) backend · React + Vite + TypeScript frontend · React Query · pytest · vitest.

## Global Constraints

- **New DB columns MUST be nullable.** `create_all` never `ALTER`s an existing table; `init_db` backfills only missing *nullable* columns. A NOT-NULL column would be absent on the live SQLite deploy and break every read. (CLAUDE.md dev-DB note.)
- **`ToolDetailMode`** is `'auto' | 'all-open' | 'all-closed'`, defined in and imported from `@/session/groupBlocks`. Valid set verbatim: `{"auto", "all-closed", "all-open"}`.
- **`thinkingCollapsed` semantics:** `true` ⇒ thinking blocks start collapsed (open=false). DB default is `True`.
- **Tooling in a fresh worktree has no built env.** Run server tests with the main checkout's prebuilt venv; symlink `web/node_modules` from the main checkout before web tests.
  - Server: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/<file> -v`
  - Web (from `web/`): `npx vitest run <file>`, `npx tsc --noEmit`, `npx eslint <files>`
- **Commit trailer** on every commit: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

## Task 0: Worktree test-env setup

**Files:** none (environment only)

- [ ] **Step 1: Symlink web/node_modules from the main checkout**

Run:
```bash
cd /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.claude/worktrees/persist-tool-detail-setting/web
[ -e node_modules ] || ln -s /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/web/node_modules node_modules
ls node_modules/.bin/vitest
```
Expected: prints the vitest path (symlink resolves).

- [ ] **Step 2: Verify the server venv runs pytest**

Run:
```bash
/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest --version
```
Expected: prints a pytest version line.

---

## Task 1: Backend — persist `default_tool_detail`

**Files:**
- Modify: `server/db/models.py` (the `AppSettings` class, ~lines 82-90)
- Modify: `server/api/settings.py`
- Test: `server/tests/test_settings.py`

**Interfaces:**
- Produces: GET `/api/settings` returns key `"defaultToolDetail"` (string, one of the valid set, defaulting to `"auto"`). PUT `/api/settings` accepts optional `defaultToolDetail`; invalid values are ignored (row unchanged), mirroring `defaultPermissionMode`.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_settings.py`:

```python
# ---------------------------------------------------------------------------
# defaultToolDetail
# ---------------------------------------------------------------------------


def test_get_settings_default_tool_detail_is_auto():
    """GET /api/settings returns defaultToolDetail 'auto' by default."""
    with temp_env():
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            _, client = _make_app_and_client()
            with client:
                resp = client.get("/api/settings", headers=IDENTITY_HEADERS)

    assert resp.status_code == 200
    assert resp.json()["defaultToolDetail"] == "auto"


def test_put_default_tool_detail_round_trips():
    """PUT {defaultToolDetail: 'all-closed'} then GET → 'all-closed'."""
    with temp_env():
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            _, client = _make_app_and_client()
            with client:
                put_resp = client.put(
                    "/api/settings",
                    json={"defaultToolDetail": "all-closed"},
                    headers=IDENTITY_HEADERS,
                )
                assert put_resp.status_code == 200
                assert put_resp.json()["defaultToolDetail"] == "all-closed"

                get_resp = client.get("/api/settings", headers=IDENTITY_HEADERS)
                assert get_resp.json()["defaultToolDetail"] == "all-closed"


def test_put_rejects_invalid_tool_detail():
    """PUT {defaultToolDetail: 'bogus'} → value silently stays 'auto'."""
    with temp_env():
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            _, client = _make_app_and_client()
            with client:
                put_resp = client.put(
                    "/api/settings",
                    json={"defaultToolDetail": "bogus"},
                    headers=IDENTITY_HEADERS,
                )
                assert put_resp.status_code == 200
                assert put_resp.json()["defaultToolDetail"] == "auto"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:
```bash
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_settings.py -v -k tool_detail
```
Expected: FAIL — `KeyError: 'defaultToolDetail'` (key not in response).

- [ ] **Step 3: Add the model column**

In `server/db/models.py`, inside `class AppSettings`, add after the `repo_root` line:

```python
    default_tool_detail: Mapped[str | None] = mapped_column(String, nullable=True)
```

(Nullable so `init_db` backfills it on the live DB — see Global Constraints.)

- [ ] **Step 4: Add the API field, validation, and serialization**

In `server/api/settings.py`:

Add the valid-set constant under `_VALID_MODES`:
```python
_VALID_TOOL_DETAIL = {"auto", "all-closed", "all-open"}
```

Add the body field to `SettingsBody`:
```python
class SettingsBody(BaseModel):
    defaultPermissionMode: str | None = None
    thinkingCollapsed: bool | None = None
    repoRoot: str | None = None
    defaultToolDetail: str | None = None
```

Add to `_to_dict` (with read-time default):
```python
def _to_dict(s: AppSettings) -> dict:
    return {
        "defaultPermissionMode": s.default_permission_mode,
        "thinkingCollapsed": s.thinking_collapsed,
        "repoRoot": s.repo_root,
        "defaultToolDetail": s.default_tool_detail or "auto",
    }
```

Add the PUT handling after the `repoRoot` branch in `put_settings_row`:
```python
    if body.defaultToolDetail is not None and body.defaultToolDetail in _VALID_TOOL_DETAIL:
        row.default_tool_detail = body.defaultToolDetail
```

- [ ] **Step 5: Run the tests to verify they pass**

Run:
```bash
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_settings.py -v
```
Expected: PASS (all settings tests, including the three new ones).

- [ ] **Step 6: Commit**

```bash
git add server/db/models.py server/api/settings.py server/tests/test_settings.py
git commit -m "feat(settings): persist defaultToolDetail in AppSettings

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Frontend — `AppSettings` type gains `defaultToolDetail`

**Files:**
- Modify: `web/src/api/hooks.ts` (the `AppSettings` interface, ~lines 83-87)
- Test: `web/src/api/__tests__/hooks.settings.test.ts`

**Interfaces:**
- Produces: `AppSettings.defaultToolDetail: ToolDetailMode`. Consumers (Tasks 3, 5) read `settings.defaultToolDetail`.

- [ ] **Step 1: Update the test fixture and add an assertion**

In `web/src/api/__tests__/hooks.settings.test.ts`, extend the `SETTINGS` fixture:
```ts
const SETTINGS = {
  defaultPermissionMode: 'interactive',
  thinkingCollapsed: false,
  repoRoot: '/home/user/repos',
  defaultToolDetail: 'auto',
}
```

Add an assertion inside the existing `it('exposes defaultPermissionMode, thinkingCollapsed, and repoRoot', ...)` body:
```ts
    expect(result.current.data?.defaultToolDetail).toBe('auto')
```

- [ ] **Step 2: Run the test to verify it fails (type error)**

Run (from `web/`):
```bash
npx tsc --noEmit
```
Expected: FAIL — `defaultToolDetail` not assignable / missing on `AppSettings`.

- [ ] **Step 3: Add the field to the type**

In `web/src/api/hooks.ts`, import the mode type at the top (with the other imports):
```ts
import type { ToolDetailMode } from '@/session/groupBlocks'
```
Extend the interface:
```ts
export interface AppSettings {
  defaultPermissionMode: string
  thinkingCollapsed: boolean
  repoRoot: string | null
  defaultToolDetail: ToolDetailMode
}
```

- [ ] **Step 4: Run tsc and the test to verify they pass**

Run (from `web/`):
```bash
npx tsc --noEmit && npx vitest run src/api/__tests__/hooks.settings.test.ts
```
Expected: tsc clean; test PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/api/hooks.ts web/src/api/__tests__/hooks.settings.test.ts
git commit -m "feat(settings): add defaultToolDetail to AppSettings type

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Source tool-fold from settings; remove the in-session toggle

This task lands together because the runtime change and the removal of its consumers (button, keyboard, shortcut help) must compile as one unit.

**Files:**
- Modify: `web/src/session/useSessionRuntime.ts` (imports ~21; interface ~28-31; state ~73-79; return ~225)
- Modify: `web/src/components/session/SessionView.tsx` (comment ~61; keydown ~67-71; label ~94-101; button ~127-139)
- Modify: `web/src/components/DesktopShell.tsx` (`MainPaneSession`, ~52-88)
- Modify: `web/src/components/ShortcutsHelp.tsx` (~line 32)
- Test: `web/src/components/session/__tests__/SessionView.toolDetail.test.tsx` (full rewrite)

**Interfaces:**
- Consumes: `useSettings()` → `AppSettings` (Task 2).
- Produces: `SessionRuntime` gains `thinkingCollapsed: boolean`, **loses** `toggleToolDetail`. `toolDetailMode` is now derived (still a `ToolDetailMode`). Task 5 consumes `rt.thinkingCollapsed`.

- [ ] **Step 1: Rewrite the SessionView tool-detail test to assert the toggle is gone**

Replace the entire body of `web/src/components/session/__tests__/SessionView.toolDetail.test.tsx` with:

```tsx
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { SessionView } from '../SessionView'

const stableMockSocket = {
  attach: vi.fn(), detach: vi.fn(), interrupt: vi.fn(), prompt: vi.fn(),
  sendInterrupt: vi.fn(), permissionResponse: vi.fn(), answerQuestion: vi.fn(),
  setPermissionMode: vi.fn(), cancelQueued: vi.fn(),
  on: vi.fn(), off: vi.fn(), connectionState: 'connected' as const, socket: null,
}

vi.mock('@/ws/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/ws/client')>()
  return { ...actual, useSessionSocket: () => stableMockSocket }
})

function wrapper({ children }: { children: React.ReactNode }) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
}

describe('SessionView tool-detail control removed from the top bar', () => {
  beforeEach(() => vi.clearAllMocks())

  it('renders no tool-detail toggle button in the session top bar', () => {
    render(<SessionView sessionId="s1" onBack={vi.fn()} />, { wrapper })
    expect(screen.queryByRole('button', { name: /tool calls/i })).not.toBeInTheDocument()
  })

  it('pressing "c" does not produce a tool-detail toggle button', async () => {
    const user = userEvent.setup()
    render(<SessionView sessionId="s1" onBack={vi.fn()} />, { wrapper })
    await user.keyboard('c')
    expect(screen.queryByRole('button', { name: /tool calls/i })).not.toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run (from `web/`):
```bash
npx vitest run src/components/session/__tests__/SessionView.toolDetail.test.tsx
```
Expected: FAIL — the button still exists, so `queryByRole(... /tool calls/i)` is non-null.

- [ ] **Step 3: Update the runtime to source from settings and drop the toggle**

In `web/src/session/useSessionRuntime.ts`:

Add `useSettings` to the existing `@/api/hooks` import (it currently imports `useSessions, useAccounts`):
```ts
import { useSessions, useAccounts, useSettings } from '@/api/hooks'
```

In the `SessionRuntime` interface, replace these two lines:
```ts
  toolDetailMode: ToolDetailMode
  toggleToolDetail: () => void
```
with:
```ts
  toolDetailMode: ToolDetailMode
  thinkingCollapsed: boolean
```

Remove the local state + toggle (the block at ~73-79):
```ts
  const [toolDetailMode, setToolDetailMode] = useState<ToolDetailMode>('auto')
  const toggleToolDetail = useCallback(
    () => setToolDetailMode((m) =>
      m === 'auto' ? 'all-closed' : m === 'all-closed' ? 'all-open' : 'auto',
    ),
    [],
  )
```
Delete that block entirely.

Near the other derived-data hooks (next to `useSessions()`/`useAccounts()`), add:
```ts
  const { data: appSettings } = useSettings()
  const toolDetailMode: ToolDetailMode = appSettings?.defaultToolDetail ?? 'auto'
  const thinkingCollapsed = appSettings?.thinkingCollapsed ?? true
```

In the returned object, replace `toggleToolDetail,` with `thinkingCollapsed,` (keep `toolDetailMode,`).

If `useState`/`useCallback` become unused after this edit, remove them from the React import to keep eslint clean. (Check: other code in the file still uses them — `mode`, `armedQuestionId` use `useState`; leave the import members that remain in use.)

- [ ] **Step 4: Remove the SessionView button and keyboard branch**

In `web/src/components/session/SessionView.tsx`:

Delete the `toolToggleText`/`toolToggleAria` block (the `const toolToggleText = ...` through `: 'Auto-fold tool calls'`).

Remove the `c`/`C` branch from the keydown handler — delete:
```ts
      if (!typing && (e.key === 'c' || e.key === 'C') && !e.metaKey && !e.ctrlKey && !e.altKey) {
        e.preventDefault()
        toggleToolDetail()
        return
      }
```
Also remove `toggleToolDetail` from the destructure `const { hasOpenAction, onSetMode, toggleToolDetail } = rt` → `const { hasOpenAction, onSetMode } = rt`, remove the now-unused `typing`/`ae` lines **only if** they are no longer referenced (they are used only by the deleted branch — delete the two lines computing `ae` and `typing` at the top of `onKey`), and drop `toggleToolDetail` from the effect dependency array.

Update the comment block above the effect: delete the `c/C → toggle tool-detail mode ...` line.

Delete the entire toggle `<button>` element (the one with `onClick={toggleToolDetail}` / `{toolToggleText}`).

> Note: after removing the button, the title `<span>` should still sit left and `StatusBar` right. Add `ml-auto` to the `StatusBar` wrapper `<div>` so it stays right-aligned:
> ```tsx
> <div className="ml-auto">
>   <StatusBar status={rt.state.status} connectionState={rt.connectionState} />
> </div>
> ```

- [ ] **Step 5: Remove the DesktopShell button**

In `web/src/components/DesktopShell.tsx`, `MainPaneSession`:

Delete the `toolToggleText` and `toolToggleAria` `const` blocks. Delete the `<button>` element (with `onClick={rt.toggleToolDetail}` / `{toolToggleText}`). The header `<div>` now contains only the title span; give the span `mr-auto` is unnecessary — leave as-is (single child). Resulting header:
```tsx
      <div className={cn(
        'flex items-center gap-3 px-3 py-2 flex-shrink-0',
        'bg-gray-100 dark:bg-gray-900 border-b border-gray-200 dark:border-gray-700',
      )}>
        <span className="text-sm font-semibold text-gray-900 dark:text-gray-100 truncate">{rt.title}</span>
      </div>
```

- [ ] **Step 6: Remove the shortcut from ShortcutsHelp**

In `web/src/components/ShortcutsHelp.tsx`, delete the line:
```ts
  { keys: 'c', description: 'Collapse / expand tool calls' },
```

- [ ] **Step 7: Run tsc, lint, and the reworked test**

Run (from `web/`):
```bash
npx tsc --noEmit && npx eslint src/session/useSessionRuntime.ts src/components/session/SessionView.tsx src/components/DesktopShell.tsx src/components/ShortcutsHelp.tsx && npx vitest run src/components/session/__tests__/SessionView.toolDetail.test.tsx
```
Expected: tsc clean, eslint clean (no unused vars), test PASS.

- [ ] **Step 8: Run the broader session/desktop test suites for regressions**

Run (from `web/`):
```bash
npx vitest run src/components/__tests__/DesktopShell.test.tsx src/components/session
```
Expected: PASS. If any test references the removed button or `toggleToolDetail`, update it to match the new behavior (button absent), then re-run.

- [ ] **Step 9: Commit**

```bash
git add web/src/session/useSessionRuntime.ts web/src/components/session/SessionView.tsx web/src/components/DesktopShell.tsx web/src/components/ShortcutsHelp.tsx web/src/components/session/__tests__/SessionView.toolDetail.test.tsx
git commit -m "refactor(session): source tool-fold from settings, remove in-session toggle

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Wire `thinkingCollapsed` into `ThinkingBlock`

**Files:**
- Modify: `web/src/components/session/ThinkingBlock.tsx`
- Modify: `web/src/components/session/Turn.tsx` (Props + ThinkingBlock render)
- Modify: `web/src/components/session/TranscriptView.tsx` (Props + Turn render)
- Modify: `web/src/components/session/SessionBody.tsx` (TranscriptView render)
- Modify: `web/src/components/ReviewView.tsx` (TranscriptView render — pass default)
- Test: `web/src/components/session/__tests__/ThinkingBlock.test.tsx`

**Interfaces:**
- Consumes: `rt.thinkingCollapsed` (Task 3).
- Produces: `ThinkingBlock` accepts `defaultCollapsed: boolean`; `Turn`/`TranscriptView` accept and thread `thinkingCollapsed: boolean`.

- [ ] **Step 1: Write the failing tests**

In `web/src/components/session/__tests__/ThinkingBlock.test.tsx`, add (or create the file if absent, with the standard imports):

```tsx
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ThinkingBlock } from '../ThinkingBlock'

describe('ThinkingBlock defaultCollapsed', () => {
  it('starts collapsed when defaultCollapsed is true', () => {
    render(<ThinkingBlock text="secret reasoning" defaultCollapsed={true} />)
    expect(screen.getByRole('button', { name: /thinking/i })).toHaveAttribute('aria-expanded', 'false')
  })

  it('starts expanded when defaultCollapsed is false', () => {
    render(<ThinkingBlock text="secret reasoning" defaultCollapsed={false} />)
    expect(screen.getByRole('button', { name: /thinking/i })).toHaveAttribute('aria-expanded', 'true')
  })

  it('a manual toggle overrides the default', async () => {
    const user = userEvent.setup()
    render(<ThinkingBlock text="secret reasoning" defaultCollapsed={true} />)
    await user.click(screen.getByRole('button', { name: /thinking/i }))
    expect(screen.getByRole('button', { name: /thinking/i })).toHaveAttribute('aria-expanded', 'true')
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run (from `web/`):
```bash
npx vitest run src/components/session/__tests__/ThinkingBlock.test.tsx
```
Expected: FAIL — `ThinkingBlock` does not accept `defaultCollapsed`; first test sees `aria-expanded="false"` only by luck and the `defaultCollapsed={false}` test fails (still false).

- [ ] **Step 3: Update ThinkingBlock**

Replace the top of `web/src/components/session/ThinkingBlock.tsx`:

```tsx
import { useState, useId } from 'react'
import { ChevronDown, ChevronRight } from 'lucide-react'
import { cn } from '@/lib/utils'

interface Props { text: string; defaultCollapsed: boolean }

export function ThinkingBlock({ text, defaultCollapsed }: Props) {
  // Honor the persisted default until the user manually toggles, then stick.
  const [userToggled, setUserToggled] = useState<boolean | null>(null)
  const open = userToggled ?? !defaultCollapsed
  const regionId = useId()
```

Change the toggle handler from `onClick={() => setOpen((v) => !v)}` to:
```tsx
        onClick={() => setUserToggled(!open)}
```

(Leave the rest of the component unchanged — `aria-expanded={open}` and `hidden={!open}` already read `open`.)

- [ ] **Step 4: Thread the prop through Turn**

In `web/src/components/session/Turn.tsx`:

Add to `interface Props`:
```ts
  thinkingCollapsed: boolean
```
Add `thinkingCollapsed` to the destructured params:
```ts
  turn, permissionMode, isLastTurn, toolDetailMode, thinkingCollapsed, streamingBlockId = null,
  onDecision, onAnswer,
```
Update the thinking render:
```tsx
          case 'thinking':
            return <ThinkingBlock key={block.id} text={block.text} defaultCollapsed={thinkingCollapsed} />
```

- [ ] **Step 5: Thread the prop through TranscriptView**

In `web/src/components/session/TranscriptView.tsx`:

Add to `interface Props`:
```ts
  thinkingCollapsed: boolean
```
Add `thinkingCollapsed` to the destructured function params, and pass it to `<Turn ...>`:
```tsx
            thinkingCollapsed={thinkingCollapsed}
```

- [ ] **Step 6: Pass from SessionBody and ReviewView**

In `web/src/components/session/SessionBody.tsx`, add to the `<TranscriptView ...>` props:
```tsx
          thinkingCollapsed={rt.thinkingCollapsed}
```

In `web/src/components/ReviewView.tsx`, add to its `<TranscriptView ...>` props (review is read-only; default to collapsed):
```tsx
            thinkingCollapsed={true}
```

- [ ] **Step 7: Run tsc, lint, and the ThinkingBlock test**

Run (from `web/`):
```bash
npx tsc --noEmit && npx eslint src/components/session/ThinkingBlock.tsx src/components/session/Turn.tsx src/components/session/TranscriptView.tsx src/components/session/SessionBody.tsx src/components/ReviewView.tsx && npx vitest run src/components/session/__tests__/ThinkingBlock.test.tsx
```
Expected: tsc clean, eslint clean, test PASS.

- [ ] **Step 8: Run the session suite for regressions**

Run (from `web/`):
```bash
npx vitest run src/components/session
```
Expected: PASS. Fix any test constructing `<Turn>`/`<TranscriptView>` without the new required `thinkingCollapsed` prop by passing `thinkingCollapsed={true}`.

- [ ] **Step 9: Commit**

```bash
git add web/src/components/session/ThinkingBlock.tsx web/src/components/session/Turn.tsx web/src/components/session/TranscriptView.tsx web/src/components/session/SessionBody.tsx web/src/components/ReviewView.tsx web/src/components/session/__tests__/ThinkingBlock.test.tsx
git commit -m "fix(session): make ThinkingBlock honor the persisted thinkingCollapsed setting

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: SettingsPanel — "Transcript display" group

**Files:**
- Modify: `web/src/components/settings/SettingsPanel.tsx`
- Test: `web/src/components/settings/__tests__/SettingsPanel.test.tsx`

**Interfaces:**
- Consumes: `AppSettings.defaultToolDetail` (Task 2), `useUpdateSettings` mutate.
- Produces: a labeled select "Tool calls default" wired to `defaultToolDetail`; `handleSave` includes `defaultToolDetail`.

- [ ] **Step 1: Add failing tests**

In `web/src/components/settings/__tests__/SettingsPanel.test.tsx`:

Extend the `SETTINGS` fixture with `defaultToolDetail: 'auto'`:
```ts
const SETTINGS = {
  defaultPermissionMode: 'interactive',
  thinkingCollapsed: false,
  repoRoot: '/home/user/repos',
  defaultToolDetail: 'auto',
}
```

Add two tests inside the `describe('SettingsPanel', ...)` block:
```tsx
  it('renders the tool calls default select pre-filled with current value', () => {
    render(<SettingsPanel onClose={vi.fn()} />, { wrapper })
    const select = screen.getByLabelText(/tool calls default/i)
    expect(select).toBeInTheDocument()
    expect((select as HTMLSelectElement).value).toBe('auto')
  })

  it('saves defaultToolDetail when changed', async () => {
    const mutate = vi.fn()
    vi.mocked(hooks.useUpdateSettings).mockReturnValue({
      ...NOOP_MUTATION,
      mutate,
    } as unknown as ReturnType<typeof hooks.useUpdateSettings>)

    const user = userEvent.setup()
    render(<SettingsPanel onClose={vi.fn()} />, { wrapper })

    await user.selectOptions(screen.getByLabelText(/tool calls default/i), 'all-closed')
    await user.click(screen.getByRole('button', { name: /save/i }))

    expect(mutate).toHaveBeenCalledWith(
      expect.objectContaining({ defaultToolDetail: 'all-closed' }),
      expect.anything(),
    )
  })
```

- [ ] **Step 2: Run the tests to verify they fail**

Run (from `web/`):
```bash
npx vitest run src/components/settings/__tests__/SettingsPanel.test.tsx
```
Expected: FAIL — no element labeled "tool calls default".

- [ ] **Step 3: Add the control and group to SettingsPanel**

In `web/src/components/settings/SettingsPanel.tsx`:

Add state initializer alongside the others:
```tsx
  const [toolDetail, setToolDetail] = useState<AppSettings['defaultToolDetail']>(
    settings?.defaultToolDetail ?? 'auto',
  )
```

Add `defaultToolDetail: toolDetail,` to the `updateSettings.mutate({ ... })` object in `handleSave`.

Add a "Transcript display" section. Place it after the "Default tool approvals" block and **move the existing "Collapse thinking by default" checkbox into it.** The section markup:
```tsx
          {/* Transcript display */}
          <div className="space-y-3 border-t border-gray-200 dark:border-gray-700 pt-4">
            <h3 className="text-sm font-semibold text-gray-900 dark:text-gray-100">
              Transcript display
            </h3>
            <div className="space-y-1">
              <label
                htmlFor="settings-tool-detail"
                className="block text-sm font-medium text-gray-700 dark:text-gray-300"
              >
                Tool calls default
              </label>
              <select
                id="settings-tool-detail"
                value={toolDetail}
                onChange={(e) => setToolDetail(e.target.value as AppSettings['defaultToolDetail'])}
                className={inputClass}
              >
                <option value="auto">Auto-fold long groups</option>
                <option value="all-open">Expand all</option>
                <option value="all-closed">Collapse all</option>
              </select>
            </div>
            {/* (relocated) Collapse thinking by default checkbox goes here */}
          </div>
```
Move the existing `{/* Collapse thinking by default */}` `<div className="flex items-center gap-3">...</div>` block to where the comment indicates, so the checkbox renders inside the new section.

- [ ] **Step 4: Run tsc, lint, and the SettingsPanel test**

Run (from `web/`):
```bash
npx tsc --noEmit && npx eslint src/components/settings/SettingsPanel.tsx && npx vitest run src/components/settings/__tests__/SettingsPanel.test.tsx
```
Expected: tsc clean, eslint clean, all SettingsPanel tests PASS (including the pre-existing thinkingCollapsed test, which still finds the checkbox by label).

- [ ] **Step 5: Commit**

```bash
git add web/src/components/settings/SettingsPanel.tsx web/src/components/settings/__tests__/SettingsPanel.test.tsx
git commit -m "feat(settings): add Transcript display group with tool-fold default

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Full-suite verification

**Files:** none (verification only)

- [ ] **Step 1: Grep for stale references to the removed toggle**

Run (from repo root):
```bash
grep -rn "toggleToolDetail\|Collapse tools\|Auto-fold tool calls\|toolToggle" web/src --include="*.ts" --include="*.tsx"
```
Expected: no matches. If any remain, remove them and re-run the relevant test file.

- [ ] **Step 2: Full web suite + types + lint**

Run (from `web/`):
```bash
npx tsc --noEmit && npx eslint . && npx vitest run
```
Expected: all green.

- [ ] **Step 3: Full server settings suite**

Run:
```bash
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_settings.py -v
```
Expected: all green.

- [ ] **Step 4: Open a PR** (follow the project completion workflow: branch off master, push, `gh pr create`, merge once mergeable).

---

## Self-Review

**Spec coverage:**
- Persisted default in `AppSettings` → Task 1. ✔
- `AppSettings` TS type → Task 2. ✔
- Runtime derives mode from settings; remove `useState`/`toggleToolDetail` → Task 3. ✔
- Remove top-bar button (SessionView + DesktopShell) and `c` shortcut + ShortcutsHelp → Task 3. ✔
- Wire `thinkingCollapsed` into `ThinkingBlock` with manual-override stickiness → Task 4. ✔
- SettingsPanel "Transcript display" group (select + relocated checkbox) → Task 5. ✔
- Nullable column / `init_db` backfill / read-time `or 'auto'` → Task 1, Global Constraints. ✔
- `ReviewView` keeps literal `auto` and now passes `thinkingCollapsed={true}` → Task 4. ✔
- Tests: server round-trip/validation/default (Task 1), SettingsPanel save (Task 5), ThinkingBlock honors setting (Task 4), SessionView toggle removed (Task 3). ✔

**Type consistency:** `ToolDetailMode` used uniformly from `@/session/groupBlocks`; `defaultToolDetail` (camelCase API / `default_tool_detail` column); `defaultCollapsed` (ThinkingBlock prop) vs `thinkingCollapsed` (runtime/Turn/TranscriptView field) — intentionally distinct names, mapping done in Turn's render. `_VALID_TOOL_DETAIL` matches the `ToolDetailMode` union exactly.

**Placeholder scan:** none — all steps include concrete code and commands.
