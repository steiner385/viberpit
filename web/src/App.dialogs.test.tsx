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
  { name: 'alice', status: 'ok', usage: { session: { pct: 20, resetsAt: null }, weekly: { pct: 30, resetsAt: null } }, liveSessions: { active: 0, idle: 0 }, rank: null, rankReason: null },
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

  it('redirects legacy ?dialog=settings to the Settings tab', async () => {
    renderAt('/sessions?dialog=settings')
    await waitFor(() => {
      expect(screen.getByTestId('search')).not.toHaveTextContent('dialog=settings')
    })
    // The Settings tab renders its inner section strip — no modal dialog.
    expect(await screen.findByRole('tablist', { name: /settings sections/i })).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
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
