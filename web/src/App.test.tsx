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
    liveSessions: { active: 0, idle: 0 },
    rank: null,
    rankReason: null,
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

  it('renders the DesktopShell side-by-side layout at lg and up', async () => {
    vi.stubGlobal('matchMedia', (q: string) => ({
      matches: true, media: q,
      addEventListener: () => {}, removeEventListener: () => {},
      addListener: () => {}, removeListener: () => {}, onchange: null, dispatchEvent: () => false,
    }))
    // At lg+, App ignores routing and renders the DesktopShell. The New Session
    // slot is a selectable sidebar entry (data-selected) — the mobile/routed
    // layout's "New session" button has no data-selected attribute, so this
    // assertion is true only when the DesktopShell is rendered.
    renderAt('/sessions')
    await waitFor(() => {
      expect(screen.getByLabelText('New session')).toHaveAttribute('data-selected', 'true')
    })
    vi.unstubAllGlobals()
  })
})
