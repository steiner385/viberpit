# Deep-linkable URLs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every meaningful view in the ccswitch web UI its own clean-path URL (dashboard tabs, live session, archived review, plus the New-session and Settings dialogs) so links can be bookmarked and shared.

**Architecture:** Introduce React Router v7 as the source of truth for view/tab/dialog state. `App.tsx` owns all routing; `Dashboard.tsx` stays router-free and becomes *controlled* via new optional props (`activeTab`/`onTabChange`, `dialog`/`onDialogChange`) that fall back to internal state when omitted — so existing isolated component tests keep passing. The FastAPI backend gains an SPA fallback so clean-path deep links survive a refresh.

**Tech Stack:** React 19 + TypeScript + Vite + Vitest/Testing Library (frontend); FastAPI + Starlette + pytest/httpx (backend); `react-router-dom` v7 (new dependency).

## Global Constraints

- Constitution v1.1.0 applies. Relevant here:
  - **V (TDD + quality gates):** every task is test-first; `npm run test`, `npm run lint`, `npm run typecheck` (in `web/`) and `pytest` must be green before commit.
  - **VI (single source of truth / no dead code):** remove the now-unused view state machine and focus-nonce code; do not leave parallel state mechanisms.
  - **VII (modern vetted deps):** `react-router-dom` v7 is the only new dependency; it supports React 19.
- Frontend commands run from `web/`. Backend commands run from repo root.
- Clean-path URLs only (no hash routing).
- URL map (exact):
  - `/` → redirect to `/sessions`
  - `/sessions` (Sessions tab), `/accounts` (Accounts tab), `/archived` (Archived tab)
  - `/session/:sessionId` (live session), `/review/:sessionId` (archived review)
  - dialogs as a search param on the current route: `?dialog=new`, `?dialog=settings`
  - any unknown path → redirect to `/sessions`

## Scope note (read before starting)

The approved design says "dialogs are URL-addressable." This plan implements that for the two **parameterless, genuinely shareable** dialogs — **New session** (`?dialog=new`) and **Settings** (`?dialog=settings`). The remaining dialogs are **transient, target-bound confirmations** spawned from a row action (Rename session, Delete account, Permanently delete, Shortcuts help). They require an in-memory target object and are not meaningful as shared links, so they deliberately remain local component state. This is a YAGNI-driven boundary; if shareable rename/delete links are wanted later, they would need an id in the URL and are out of scope here.

---

## Task 1: Backend SPA fallback

Independent of the frontend tasks (can be done first or in parallel). Adds a `SPAStaticFiles` that serves `index.html` for any path that isn't a real file, so clean-path deep links survive a refresh, while `/api/*` and `/ws/*` keep their precedence.

**Files:**
- Create: `server/spa.py`
- Create: `server/tests/test_spa_fallback.py`
- Modify: `server/app.py` (imports near top; replace the SPA mount block at lines 127–130)

**Interfaces:**
- Produces:
  - `server/spa.py`: `class SPAStaticFiles(StaticFiles)` and `def mount_spa(app: FastAPI, dist_dir: Path) -> None` — mounts the SPA at `/` only when `dist_dir` is an existing directory; no-op otherwise.
- Consumes: nothing from other tasks.

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_spa_fallback.py`:

```python
"""SPA fallback: clean-path deep links serve index.html; assets and /api still work."""
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server.spa import mount_spa


def _make_dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>ccswitch</title>")
    assets = dist / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("console.log('hi')")
    return dist


@pytest.mark.asyncio
async def test_spa_serves_index_for_client_route(tmp_path):
    app = FastAPI()
    mount_spa(app, _make_dist(tmp_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/session/abc123")
    assert resp.status_code == 200
    assert "ccswitch" in resp.text


@pytest.mark.asyncio
async def test_spa_serves_real_asset(tmp_path):
    app = FastAPI()
    mount_spa(app, _make_dist(tmp_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/assets/app.js")
    assert resp.status_code == 200
    assert "console.log" in resp.text


@pytest.mark.asyncio
async def test_api_routes_take_precedence_over_spa(tmp_path):
    app = FastAPI()

    @app.get("/api/ping")
    async def ping():
        return {"pong": True}

    mount_spa(app, _make_dist(tmp_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/ping")
    assert resp.status_code == 200
    assert resp.json() == {"pong": True}


@pytest.mark.asyncio
async def test_mount_spa_noop_when_dist_missing(tmp_path):
    app = FastAPI()
    mount_spa(app, tmp_path / "nonexistent")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/session/abc")
    assert resp.status_code == 404
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest server/tests/test_spa_fallback.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'server.spa'`.

- [ ] **Step 3: Implement `server/spa.py`**

Create `server/spa.py`:

```python
"""Single-page-app static serving with client-side-routing fallback.

`StaticFiles(html=True)` only serves index.html for the directory root and real
files, so a refresh on a clean deep link like `/session/abc` 404s. `SPAStaticFiles`
catches that 404 and serves `index.html` instead, letting the React router resolve
the path on the client.
"""
from pathlib import Path

from fastapi import FastAPI
from starlette.exceptions import HTTPException
from starlette.staticfiles import StaticFiles
from starlette.types import Scope


class SPAStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope):
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code == 404:
                return await super().get_response("index.html", scope)
            raise


def mount_spa(app: FastAPI, dist_dir: Path) -> None:
    """Mount the built SPA at `/` if `dist_dir` exists; otherwise do nothing.

    Must be called AFTER all `/api/*` and `/ws/*` routes are registered so those
    routes match before this catch-all mount.
    """
    if not dist_dir.is_dir():
        return
    app.mount("/", SPAStaticFiles(directory=str(dist_dir), html=True), name="spa")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest server/tests/test_spa_fallback.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Wire `mount_spa` into `server/app.py`**

In `server/app.py`, replace the import line:

```python
from fastapi.staticfiles import StaticFiles
```

with:

```python
from server.spa import mount_spa
```

Then replace the SPA mount block (currently lines 127–130):

```python
    # SPA static files — optional; guard so it's missing in dev/test (T039)
    spa_dist = Path(__file__).parent.parent / "web" / "dist"
    if spa_dist.is_dir():
        app.mount("/", StaticFiles(directory=str(spa_dist), html=True), name="spa")
```

with:

```python
    # SPA static files with client-side-routing fallback — optional; guarded so a
    # missing build (dev/test) doesn't break startup. Registered AFTER the API/WS
    # routers so /api/* and /ws/* keep precedence.
    mount_spa(app, Path(__file__).parent.parent / "web" / "dist")
```

(`Path` is already imported at the top of `server/app.py`.)

- [ ] **Step 6: Run the full backend suite to confirm no regression**

Run: `python -m pytest server/tests/test_spa_fallback.py server/tests/test_health.py -v`
Expected: PASS for all.

- [ ] **Step 7: Commit**

```bash
git add server/spa.py server/tests/test_spa_fallback.py server/app.py
git commit -m "feat(server): SPA fallback so clean-path deep links survive refresh

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Frontend routing — URL drives view and tab

Replace the `App.tsx` view state machine with React Router routes. The browser location becomes the source of truth for which view is shown and which dashboard tab is active. `Dashboard` becomes controlled (new optional `activeTab`/`onTabChange` props) while keeping its uncontrolled fallback so existing isolated tests pass unchanged.

**Files:**
- Modify: `web/package.json` (add dependency)
- Modify: `web/src/main.tsx` (wrap in `<BrowserRouter>`)
- Modify: `web/src/App.tsx` (full rewrite — routes replace the state machine)
- Modify: `web/src/App.test.tsx` (full rewrite — router-driven tests)
- Modify: `web/src/components/Dashboard.tsx` (controlled `activeTab`/`onTabChange`; remove unused `focusDispatchSignal` usage path)

**Interfaces:**
- Consumes: `mount_spa` is unrelated; no dependency on Task 1 at the code level.
- Produces:
  - `Dashboard` props gain `activeTab?: TabId` and `onTabChange?: (tab: TabId) => void`. When `onTabChange` is provided, tab clicks call it (and `activeTab` is treated as controlled); otherwise internal `useState` is used (unchanged behavior).
  - `App` renders routes: `/` → redirect `/sessions`; `/sessions|/accounts|/archived` → dashboard with that tab; `/session/:sessionId` → `SessionView`; `/review/:sessionId` → `ReviewView`; `*` → redirect `/sessions`.
  - `SessionView`/`ReviewView` keep their existing props (`sessionId: string`, `onBack: () => void`); `App` supplies `onBack={() => navigate('/sessions')}`.

- [ ] **Step 1: Add the router dependency**

Run: `cd web && npm install react-router-dom@^7`
Expected: `react-router-dom` added to `web/package.json` dependencies and installed.

- [ ] **Step 2: Write the failing routing tests**

Replace the entire contents of `web/src/App.test.tsx` with:

```tsx
/**
 * Tests for App routing. The backend API and WebSocket are mocked so no real
 * network access occurs. The app is rendered inside a MemoryRouter so each test
 * can deep-link to a specific URL.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, useLocation } from 'react-router-dom'
import App from './App'
import type { Session, Account } from './api/types'

vi.mock('./ws/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./ws/client')>()
  return {
    ...actual,
    useSessionSocket: () => ({
      attach: vi.fn(),
      detach: vi.fn(),
      interrupt: vi.fn(),
      prompt: vi.fn(),
      sendInterrupt: vi.fn(),
      permissionResponse: vi.fn(),
      answerQuestion: vi.fn(),
      setPermissionMode: vi.fn(),
      on: vi.fn(),
      off: vi.fn(),
      connectionState: 'connected' as const,
      socket: null,
    }),
  }
})

const MOCK_SESSIONS: Session[] = [
  {
    id: 'sess-1',
    name: 'Work',
    accountName: 'alice',
    status: 'idle',
    createdAt: '2026-06-19T10:00:00Z',
    updatedAt: '2026-06-19T10:00:00Z',
  },
]

const MOCK_ACCOUNTS: Account[] = [
  {
    name: 'alice',
    status: 'ok',
    usage: { session: { pct: 20, resetsAt: null }, weekly: { pct: 30, resetsAt: null } },
  },
]

vi.mock('./api/client', () => ({
  ApiRequestError: class extends Error {
    status: number; body: Record<string, unknown>
    constructor(status: number, body: Record<string, unknown>) {
      super(`API error ${status}`)
      this.status = status
      this.body = body
    }
  },
  api: {
    get: vi.fn().mockImplementation((path: string) => {
      if (path === '/sessions') return Promise.resolve(MOCK_SESSIONS)
      if (path === '/accounts') return Promise.resolve(MOCK_ACCOUNTS)
      return Promise.resolve([])
    }),
    post: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
  },
}))

// Test helper: surfaces the current pathname so assertions can read the URL.
function LocationProbe() {
  const location = useLocation()
  return <div data-testid="pathname">{location.pathname}</div>
}

function renderAt(initialPath: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialPath]}>
        <App />
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('App routing', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders the dashboard heading at /sessions', () => {
    renderAt('/sessions')
    expect(screen.getByRole('heading', { name: /ccswitch/i })).toBeInTheDocument()
  })

  it('redirects / to /sessions', () => {
    renderAt('/')
    expect(screen.getByTestId('pathname')).toHaveTextContent('/sessions')
    expect(screen.getByRole('heading', { name: /ccswitch/i })).toBeInTheDocument()
  })

  it('redirects an unknown path to /sessions', () => {
    renderAt('/nonsense/path')
    expect(screen.getByTestId('pathname')).toHaveTextContent('/sessions')
  })

  it('deep-links to a session view at /session/:id', () => {
    renderAt('/session/sess-1')
    expect(screen.getByRole('button', { name: /back to session list/i })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: /ccswitch/i })).not.toBeInTheDocument()
  })

  it('deep-links to the archived review view at /review/:id', () => {
    renderAt('/review/sess-1')
    expect(screen.getByRole('button', { name: /back to dashboard/i })).toBeInTheDocument()
  })

  it('opens a session from the list and updates the URL', async () => {
    const user = userEvent.setup()
    renderAt('/sessions')
    await waitFor(() => {
      expect(screen.getByLabelText(/open session work/i)).toBeInTheDocument()
    })
    await user.click(screen.getByLabelText(/open session work/i))
    expect(screen.getByTestId('pathname')).toHaveTextContent('/session/sess-1')
    expect(screen.getByRole('button', { name: /back to session list/i })).toBeInTheDocument()
  })

  it('navigates back to /sessions from a session', async () => {
    const user = userEvent.setup()
    renderAt('/session/sess-1')
    await user.click(screen.getByRole('button', { name: /back to session list/i }))
    expect(screen.getByTestId('pathname')).toHaveTextContent('/sessions')
    expect(screen.getByRole('heading', { name: /ccswitch/i })).toBeInTheDocument()
  })

  it('focuses the dispatch prompt after returning to the dashboard', async () => {
    const user = userEvent.setup()
    renderAt('/session/sess-1')
    await user.click(screen.getByRole('button', { name: /back to session list/i }))
    await waitFor(() => {
      expect(document.activeElement).toBe(
        screen.getByRole('textbox', { name: /new session prompt/i }),
      )
    })
  })

  it('deep-links to the accounts tab at /accounts', async () => {
    renderAt('/accounts')
    await waitFor(() => {
      expect(screen.getByRole('tab', { name: /accounts/i })).toHaveAttribute('aria-selected', 'true')
    })
  })

  it('clicking the Accounts tab updates the URL to /accounts', async () => {
    const user = userEvent.setup()
    renderAt('/sessions')
    await user.click(screen.getByRole('tab', { name: /accounts/i }))
    expect(screen.getByTestId('pathname')).toHaveTextContent('/accounts')
  })
})
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd web && npx vitest run src/App.test.tsx`
Expected: FAIL — current `App.tsx` has no router, so redirects/`pathname` assertions and URL-update assertions fail.

- [ ] **Step 4: Rewrite `web/src/App.tsx`**

Replace the entire contents of `web/src/App.tsx` with:

```tsx
import { Navigate, Route, Routes, useNavigate, useParams } from 'react-router-dom'
import { Dashboard, type TabId } from './components/Dashboard'
import { SessionView } from './components/session/SessionView'
import { ReviewView } from './components/ReviewView'

// Routing is the source of truth for which view is shown:
//   /sessions | /accounts | /archived  → dashboard with that tab active
//   /session/:sessionId                → full-screen terminal for a session
//   /review/:sessionId                 → read-only transcript for an archived session
// Dialogs are layered on the dashboard via a `?dialog=` search param (see Dashboard).

function DashboardRoute({ tab }: { tab: TabId }) {
  const navigate = useNavigate()
  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="p-4 border-b border-border">
        <h1 className="text-2xl font-bold tracking-tight">ccswitch</h1>
      </header>
      <main className="p-4">
        <Dashboard
          activeTab={tab}
          onTabChange={(next) => navigate(next === 'sessions' ? '/sessions' : `/${next}`)}
          onOpenSession={(id) => navigate(`/session/${id}`)}
          onReviewSession={(id) => navigate(`/review/${id}`)}
        />
      </main>
    </div>
  )
}

function SessionRoute() {
  const { sessionId } = useParams()
  const navigate = useNavigate()
  if (!sessionId) return <Navigate to="/sessions" replace />
  return (
    <div className="min-h-screen bg-background text-foreground">
      <SessionView sessionId={sessionId} onBack={() => navigate('/sessions')} />
    </div>
  )
}

function ReviewRoute() {
  const { sessionId } = useParams()
  const navigate = useNavigate()
  if (!sessionId) return <Navigate to="/sessions" replace />
  return (
    <div className="min-h-screen bg-background text-foreground">
      <ReviewView sessionId={sessionId} onBack={() => navigate('/sessions')} />
    </div>
  )
}

function App() {
  return (
    <Routes>
      <Route path="/" element={<Navigate to="/sessions" replace />} />
      <Route path="/sessions" element={<DashboardRoute tab="sessions" />} />
      <Route path="/accounts" element={<DashboardRoute tab="accounts" />} />
      <Route path="/archived" element={<DashboardRoute tab="archived" />} />
      <Route path="/session/:sessionId" element={<SessionRoute />} />
      <Route path="/review/:sessionId" element={<ReviewRoute />} />
      <Route path="*" element={<Navigate to="/sessions" replace />} />
    </Routes>
  )
}

export default App
```

- [ ] **Step 5: Make `Dashboard` controlled for tabs**

In `web/src/components/Dashboard.tsx`:

(a) Export the `TabId` type so `App.tsx` can import it. Change:

```tsx
type TabId = 'sessions' | 'accounts' | 'archived'
```

to:

```tsx
export type TabId = 'sessions' | 'accounts' | 'archived'
```

(b) Add the controlled props to the `DashboardProps` interface (keep the existing members). Replace the interface body's prop list by adding these two members alongside the existing ones:

```tsx
interface DashboardProps {
  /** Called when the user opens a session (navigates to Terminal view). */
  onOpenSession?: (sessionId: string) => void
  /** Bump this to re-focus the dispatch bar (e.g. after returning from a session). */
  focusDispatchSignal?: unknown
  /** Called when the user wants to review an archived session. */
  onReviewSession?: (sessionId: string) => void
  /** If set, the archived tab is selected on mount (uncontrolled fallback only). */
  initialTab?: TabId
  /** Controlled active tab. When provided with onTabChange, the route owns the tab. */
  activeTab?: TabId
  /** Called when the user selects a tab. Presence switches the component to controlled mode. */
  onTabChange?: (tab: TabId) => void
}
```

(c) Update the function signature to destructure the new props:

```tsx
export function Dashboard({ onOpenSession, focusDispatchSignal, onReviewSession, initialTab, activeTab: activeTabProp, onTabChange }: DashboardProps) {
```

(d) Replace the active-tab state line:

```tsx
  const [activeTab, setActiveTab] = useState<TabId>(initialTab ?? 'sessions')
```

with a controlled-or-internal implementation:

```tsx
  const [internalTab, setInternalTab] = useState<TabId>(activeTabProp ?? initialTab ?? 'sessions')
  const activeTab = activeTabProp ?? internalTab
  const setActiveTab = (tab: TabId) => {
    if (onTabChange) onTabChange(tab)
    else setInternalTab(tab)
  }
```

(No other changes are needed in `Dashboard.tsx` for this task: the tab buttons already call `setActiveTab('sessions' | 'accounts' | 'archived')`, and the arrow-key handler only moves focus.)

- [ ] **Step 6: Update `web/src/main.tsx` to provide the router**

Replace the entire contents of `web/src/main.tsx` with:

```tsx
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter } from 'react-router-dom'
import './index.css'
import App from './App.tsx'

const queryClient = new QueryClient()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
)
```

- [ ] **Step 7: Run the routing tests to verify they pass**

Run: `cd web && npx vitest run src/App.test.tsx`
Expected: PASS (all routing tests).

- [ ] **Step 8: Run the full frontend suite + gates to confirm no regression**

Run: `cd web && npm run test && npm run typecheck && npm run lint`
Expected: all green. (The existing `DashboardTabs.test.tsx` and `DashboardSettings.test.tsx` render `<Dashboard />` with no `onTabChange`, so they use the uncontrolled fallback and pass unchanged.)

If `npm run typecheck` flags `focusDispatchSignal` as unused in `Dashboard.tsx`: it is still an accepted prop (kept for backward compatibility with callers/tests) and is destructured but unused. If the lint rule `no-unused-vars` fails on it, remove `focusDispatchSignal` from both the destructure in step 5(c) and the `DashboardProps` interface in 5(b), since `App.tsx` no longer passes it and DispatchBar autofocuses on mount. Re-run the gates.

- [ ] **Step 9: Commit**

```bash
git add web/package.json web/package-lock.json web/src/main.tsx web/src/App.tsx web/src/App.test.tsx web/src/components/Dashboard.tsx
git commit -m "feat(web): URL-driven views and tabs via React Router

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Frontend dialogs as URL layers (`?dialog=new`, `?dialog=settings`)

Make the New-session and Settings dialogs addressable via a `?dialog=` search param so a link can open them directly. `Dashboard` gains optional `dialog`/`onDialogChange` props with an uncontrolled fallback; `App`'s `DashboardRoute` wires them to `useSearchParams`.

**Files:**
- Modify: `web/src/App.tsx` (`DashboardRoute` reads/writes the `dialog` search param)
- Modify: `web/src/components/Dashboard.tsx` (controlled `dialog`/`onDialogChange` for New + Settings)
- Create: `web/src/App.dialogs.test.tsx` (router-driven dialog tests)

**Interfaces:**
- Consumes: `Dashboard`, `DashboardRoute`, and `TabId` from Task 2.
- Produces:
  - `Dashboard` props gain `dialog?: 'new' | 'settings' | null` and `onDialogChange?: (dialog: 'new' | 'settings' | null) => void`. When `onDialogChange` is provided, the New and Settings dialogs are controlled by `dialog`; otherwise internal `useState` is used (unchanged behavior).
  - `?dialog=new` opens the New-session dialog; `?dialog=settings` opens Settings; removing the param closes them.

- [ ] **Step 1: Write the failing dialog tests**

Create `web/src/App.dialogs.test.tsx`:

```tsx
/**
 * Tests for URL-addressable dialogs (?dialog=new, ?dialog=settings).
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, useLocation } from 'react-router-dom'
import App from './App'
import type { Session, Account } from './api/types'

vi.mock('./ws/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./ws/client')>()
  return {
    ...actual,
    useSessionSocket: () => ({
      attach: vi.fn(), detach: vi.fn(), interrupt: vi.fn(), prompt: vi.fn(),
      sendInterrupt: vi.fn(), permissionResponse: vi.fn(), answerQuestion: vi.fn(),
      setPermissionMode: vi.fn(), on: vi.fn(), off: vi.fn(),
      connectionState: 'connected' as const, socket: null,
    }),
  }
})

const MOCK_ACCOUNTS: Account[] = [
  { name: 'alice', status: 'ok', usage: { session: { pct: 20, resetsAt: null }, weekly: { pct: 30, resetsAt: null } } },
]
const MOCK_SESSIONS: Session[] = []

vi.mock('./api/client', () => ({
  ApiRequestError: class extends Error {
    status: number; body: Record<string, unknown>
    constructor(status: number, body: Record<string, unknown>) {
      super(`API error ${status}`); this.status = status; this.body = body
    }
  },
  api: {
    get: vi.fn().mockImplementation((path: string) => {
      if (path === '/sessions') return Promise.resolve(MOCK_SESSIONS)
      if (path === '/accounts') return Promise.resolve(MOCK_ACCOUNTS)
      return Promise.resolve([])
    }),
    post: vi.fn(), patch: vi.fn(), del: vi.fn(),
  },
}))

function LocationProbe() {
  const location = useLocation()
  return <div data-testid="search">{location.search}</div>
}

function renderAt(initialPath: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialPath]}>
        <App />
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('URL-addressable dialogs', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('opens the New session dialog from ?dialog=new', async () => {
    renderAt('/sessions?dialog=new')
    expect(await screen.findByRole('dialog', { name: /new session/i })).toBeInTheDocument()
  })

  it('opens the Settings dialog from ?dialog=settings', async () => {
    renderAt('/sessions?dialog=settings')
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
  })

  it('sets ?dialog=new when the New button is clicked', async () => {
    const user = userEvent.setup()
    renderAt('/sessions')
    await user.click(screen.getAllByRole('button', { name: /new session/i })[0])
    await waitFor(() => {
      expect(screen.getByTestId('search')).toHaveTextContent('dialog=new')
    })
  })

  it('clears the dialog param when the New dialog is closed', async () => {
    const user = userEvent.setup()
    renderAt('/sessions?dialog=new')
    expect(await screen.findByRole('dialog', { name: /new session/i })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /cancel/i }))
    await waitFor(() => {
      expect(screen.getByTestId('search')).not.toHaveTextContent('dialog=new')
    })
  })
})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd web && npx vitest run src/App.dialogs.test.tsx`
Expected: FAIL — `DashboardRoute` does not yet read/write `?dialog=`, and `Dashboard` is not yet controlled for dialogs.

- [ ] **Step 3: Make `Dashboard` controlled for the New and Settings dialogs**

In `web/src/components/Dashboard.tsx`:

(a) Add the two props to `DashboardProps` (alongside the members added in Task 2):

```tsx
  /** Controlled dialog state. Presence of onDialogChange switches to controlled mode. */
  dialog?: 'new' | 'settings' | null
  /** Called to open/close a URL-addressable dialog. */
  onDialogChange?: (dialog: 'new' | 'settings' | null) => void
```

(b) Update the destructure to include them:

```tsx
export function Dashboard({ onOpenSession, focusDispatchSignal, onReviewSession, initialTab, activeTab: activeTabProp, onTabChange, dialog, onDialogChange }: DashboardProps) {
```

(c) Just after the existing `showNewDialog`/`showSettings` state declarations (the lines
`const [showNewDialog, setShowNewDialog] = useState(false)` and
`const [showSettings, setShowSettings] = useState(false)`), add derived open-state and a dismiss helper:

```tsx
  const dialogControlled = !!onDialogChange
  const newDialogOpen = dialogControlled ? dialog === 'new' : showNewDialog
  const settingsOpen = dialogControlled ? dialog === 'settings' : showSettings
  function dismissNewDialog() {
    if (dialogControlled) onDialogChange!(null)
    else setShowNewDialog(false)
  }
```

(d) Replace the body of `openNewDialog`:

```tsx
  function openNewDialog() {
    setAllLimitedReset(undefined)
    setShowNewDialog(true)
  }
```

with:

```tsx
  function openNewDialog() {
    setAllLimitedReset(undefined)
    if (dialogControlled) onDialogChange!('new')
    else setShowNewDialog(true)
  }
```

(e) Replace the body of `closeNewDialog`:

```tsx
  function closeNewDialog() {
    setShowNewDialog(false)
    setBranchError(null)
    newButtonRef.current?.focus()
  }
```

with:

```tsx
  function closeNewDialog() {
    setBranchError(null)
    dismissNewDialog()
    newButtonRef.current?.focus()
  }
```

(f) In `handleCreateSubmit`, the success path and the all-limited error path both call `setShowNewDialog(false)`. Replace both occurrences of `setShowNewDialog(false)` inside `handleCreateSubmit` with `dismissNewDialog()`.

(g) Update the Settings button `onClick` (currently `onClick={() => setShowSettings(true)}`):

```tsx
                onClick={() => { if (dialogControlled) onDialogChange!('settings'); else setShowSettings(true) }}
```

(h) Replace the New-dialog render guard. Change:

```tsx
      {showNewDialog && (
        <NewSessionDialog
          accounts={accounts}
          onClose={closeNewDialog}
```

to:

```tsx
      {newDialogOpen && (
        <NewSessionDialog
          accounts={accounts}
          onClose={closeNewDialog}
```

(i) Replace the Settings render guard. Change:

```tsx
      {showSettings && (
        <SettingsPanel
          onClose={() => {
            setShowSettings(false)
            settingsButtonRef.current?.focus()
          }}
        />
      )}
```

to:

```tsx
      {settingsOpen && (
        <SettingsPanel
          onClose={() => {
            if (dialogControlled) onDialogChange!(null)
            else setShowSettings(false)
            settingsButtonRef.current?.focus()
          }}
        />
      )}
```

- [ ] **Step 4: Wire the `dialog` search param in `App.tsx`**

In `web/src/App.tsx`, add `useSearchParams` to the import from `react-router-dom`:

```tsx
import { Navigate, Route, Routes, useNavigate, useParams, useSearchParams } from 'react-router-dom'
```

Replace the `DashboardRoute` component with:

```tsx
function DashboardRoute({ tab }: { tab: TabId }) {
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const rawDialog = searchParams.get('dialog')
  const dialog = rawDialog === 'new' || rawDialog === 'settings' ? rawDialog : null
  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="p-4 border-b border-border">
        <h1 className="text-2xl font-bold tracking-tight">ccswitch</h1>
      </header>
      <main className="p-4">
        <Dashboard
          activeTab={tab}
          onTabChange={(next) => navigate(next === 'sessions' ? '/sessions' : `/${next}`)}
          onOpenSession={(id) => navigate(`/session/${id}`)}
          onReviewSession={(id) => navigate(`/review/${id}`)}
          dialog={dialog}
          onDialogChange={(next) => {
            setSearchParams((prev) => {
              const params = new URLSearchParams(prev)
              if (next) params.set('dialog', next)
              else params.delete('dialog')
              return params
            })
          }}
        />
      </main>
    </div>
  )
}
```

- [ ] **Step 5: Run the dialog tests to verify they pass**

Run: `cd web && npx vitest run src/App.dialogs.test.tsx`
Expected: PASS (all dialog tests).

- [ ] **Step 6: Run the full frontend suite + gates**

Run: `cd web && npm run test && npm run typecheck && npm run lint`
Expected: all green. (Existing `DashboardSettings.test.tsx` renders `<Dashboard />` without `onDialogChange`, so Settings uses the uncontrolled fallback and passes unchanged.)

- [ ] **Step 7: Commit**

```bash
git add web/src/App.tsx web/src/components/Dashboard.tsx web/src/App.dialogs.test.tsx
git commit -m "feat(web): URL-addressable New and Settings dialogs via ?dialog=

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Manual verification and finish

**Files:** none (verification + integration).

- [ ] **Step 1: Build the frontend so a real `web/dist` exists**

Run: `cd web && npm run build`
Expected: build succeeds; `web/dist/index.html` and `web/dist/assets/` exist.

- [ ] **Step 2: Start the backend and verify deep links survive a refresh**

Start the server (per `specs/001-ccswitch-web/quickstart.md`), then:

Run: `curl -s -o /dev/null -w "%{http_code}\n" -H "Tailscale-User-Login: tony@steinonline.net" http://127.0.0.1:8787/session/does-not-exist-yet`
Expected: `200` (served `index.html`, not 404).

Run: `curl -s -H "Tailscale-User-Login: tony@steinonline.net" http://127.0.0.1:8787/api/health`
Expected: JSON `{"ok": true, ...}` (API still takes precedence).

- [ ] **Step 3: Manual browser smoke (optional but recommended)**

Open `/session/<a real session id>` directly, refresh, and confirm the session view loads. Open `/accounts`, confirm the Accounts tab is active. Click into a session, then Back, and confirm the URL returns to `/sessions` and the prompt is focused. Append `?dialog=settings` to `/sessions` and confirm Settings opens.

- [ ] **Step 4: Final full-suite gate**

Run: `python -m pytest server/tests -q && cd web && npm run test && npm run typecheck && npm run lint`
Expected: all green.

- [ ] **Step 5: Finish the branch**

Use the project completion workflow (CLAUDE.md): push `001-deep-linkable-urls`, open a PR via `gh pr create` with the Claude Code trailer, and merge once mergeable.

---

## Self-review

- **Spec coverage:**
  - Router + clean paths → Task 2 (React Router v7, BrowserRouter) + Task 1 (SPA fallback). ✓
  - `/` → `/sessions`, `/accounts`, `/archived` → Task 2 routes. ✓
  - `/session/:id`, `/review/:id` → Task 2 routes. ✓
  - Dialogs via `?dialog=` → Task 3 (New + Settings; transient confirmations explicitly scoped out in the Scope note). ✓
  - Location as source of truth; remove view state machine → Task 2 (App rewrite). ✓
  - Keyboard-first focus preserved → Task 2 focus test (relies on DispatchBar mount-autofocus; the unused `focusDispatchNonce`/`originElementId` machinery is removed). ✓
  - Unknown id/path handling → Task 2 catch-all + `SessionRoute`/`ReviewRoute` guards. ✓
  - Backend catch-all not shadowing `/api`,`/ws`; guarded for missing build → Task 1 tests. ✓
  - Tests for all of the above → each task is test-first. ✓
- **Placeholder scan:** no TBD/TODO/"handle edge cases"; every code step contains complete code. ✓
- **Type consistency:** `TabId` exported in Task 2 and imported in `App.tsx`; `Dashboard` prop names (`activeTab`, `onTabChange`, `dialog`, `onDialogChange`) are consistent across Tasks 2–3; `SessionView`/`ReviewView` props (`sessionId`, `onBack`) match their existing definitions; `mount_spa(app, dist_dir)` signature consistent between `server/spa.py`, its tests, and `server/app.py`. ✓
