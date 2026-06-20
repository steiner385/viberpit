/**
 * Tests for App.tsx view-state machine.
 *
 * xterm is mocked so it's never instantiated in jsdom.
 * The backend API is mocked so no real fetches occur.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import App from './App'
import type { Session, Account } from './api/types'

// ── Mock @xterm/xterm and @xterm/addon-fit ────────────────────────────────────
// These import a DOM-incompatible canvas renderer that crashes in jsdom.
// We mock them as ES module factories so dynamic import() returns the mock.

vi.mock('@xterm/xterm', () => {
  function MockTerminal() {
    return {
      loadAddon: vi.fn(),
      open: vi.fn(),
      focus: vi.fn(),
      write: vi.fn(),
      dispose: vi.fn(),
      onData: vi.fn(),
      cols: 80,
      rows: 24,
    }
  }
  return { Terminal: MockTerminal }
})

vi.mock('@xterm/addon-fit', () => {
  function MockFitAddon() {
    return { fit: vi.fn() }
  }
  return { FitAddon: MockFitAddon }
})

// ── Mock the WebSocket singleton ──────────────────────────────────────────────

vi.mock('./ws/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./ws/client')>()
  return {
    ...actual,
    useSessionSocket: () => ({
      attach: vi.fn(),
      detach: vi.fn(),
      input: vi.fn(),
      resize: vi.fn(),
      interrupt: vi.fn(),
      on: vi.fn(),
      off: vi.fn(),
      connectionState: 'connected' as const,
      socket: null,
    }),
  }
})

// ── Mock the REST API calls ───────────────────────────────────────────────────

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
    post:  vi.fn(),
    patch: vi.fn(),
    del:   vi.fn(),
  },
}))

// ── Test wrapper ──────────────────────────────────────────────────────────────

function wrapper({ children }: { children: React.ReactNode }) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
}

// ── Tests ─────────────────────────────────────────────────────────────────────

describe('App view-state machine', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders the ccswitch heading in dashboard view', () => {
    render(<App />, { wrapper })
    expect(screen.getByRole('heading', { name: /ccswitch/i })).toBeInTheDocument()
  })

  it('switches to Terminal view when a session is opened', async () => {
    const user = userEvent.setup()
    render(<App />, { wrapper })

    // Wait for the session list to load
    await waitFor(() => {
      expect(screen.getByLabelText(/open session work/i)).toBeInTheDocument()
    })

    // Click the session open button
    await user.click(screen.getByLabelText(/open session work/i))

    // Should now show the Terminal (Back button visible, heading gone)
    expect(screen.getByRole('button', { name: /back to session list/i })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: /ccswitch/i })).not.toBeInTheDocument()
  })

  it('returns to Dashboard view when Back is clicked', async () => {
    const user = userEvent.setup()
    render(<App />, { wrapper })

    await waitFor(() => {
      expect(screen.getByLabelText(/open session work/i)).toBeInTheDocument()
    })

    // Navigate to session
    await user.click(screen.getByLabelText(/open session work/i))

    // Navigate back
    await user.click(screen.getByRole('button', { name: /back to session list/i }))

    // Should be back on dashboard
    expect(screen.getByRole('heading', { name: /ccswitch/i })).toBeInTheDocument()
  })

  it('restores focus to the originating button after Back', async () => {
    const user = userEvent.setup()
    render(<App />, { wrapper })

    await waitFor(() => {
      expect(screen.getByLabelText(/open session work/i)).toBeInTheDocument()
    })

    // Click to open session
    const openButton = screen.getByLabelText(/open session work/i)
    await user.click(openButton)

    // Click Back
    await user.click(screen.getByRole('button', { name: /back to session list/i }))

    // Focus should return to the originating button (id="row-sess-1")
    await waitFor(() => {
      const el = document.getElementById('row-sess-1')
      expect(el).not.toBeNull()
      expect(document.activeElement).toBe(el)
    })
  })
})
