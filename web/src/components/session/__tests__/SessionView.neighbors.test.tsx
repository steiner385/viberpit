// web/src/components/session/__tests__/SessionView.neighbors.test.tsx
/**
 * SessionView prev/next chevron navigation. Mocks useSessionSocket the same way
 * SessionView.test.tsx does, and seeds the react-query cache so useSessionNeighbors
 * resolves to a known ordered list.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { SessionView } from '../SessionView'
import { QK } from '@/api/hooks'
import type { Session } from '@/api/types'

vi.mock('@/ws/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/ws/client')>()
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

function mk(id: string): Session {
  return {
    id, name: id, accountName: 'a', status: 'idle',
    createdAt: '2026-06-01T00:00:00Z', updatedAt: '2026-06-01T00:00:00Z',
  }
}

function wrapperWith(sessions: Session[]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  qc.setQueryData(QK.sessions, sessions)
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
}

describe('SessionView prev/next chevrons', () => {
  beforeEach(() => window.localStorage.clear())

  it('renders prev/next chevrons with 44px touch targets when neighbors exist', () => {
    render(<SessionView sessionId="b" onBack={vi.fn()} onNavigateSession={vi.fn()} />, {
      wrapper: wrapperWith([mk('a'), mk('b'), mk('c')]),
    })
    const next = screen.getByRole('button', { name: /next session/i })
    expect(screen.getByRole('button', { name: /previous session/i })).toBeInTheDocument()
    expect(next.className).toContain('min-h-[44px]')
  })

  it('navigates to the next session id when › is clicked', async () => {
    const user = userEvent.setup()
    const onNavigateSession = vi.fn()
    render(<SessionView sessionId="b" onBack={vi.fn()} onNavigateSession={onNavigateSession} />, {
      wrapper: wrapperWith([mk('a'), mk('b'), mk('c')]),
    })
    await user.click(screen.getByRole('button', { name: /next session/i }))
    expect(onNavigateSession).toHaveBeenCalledWith('c')
  })

  it('wraps to the first session when › is clicked on the last session', async () => {
    const user = userEvent.setup()
    const onNavigateSession = vi.fn()
    render(<SessionView sessionId="c" onBack={vi.fn()} onNavigateSession={onNavigateSession} />, {
      wrapper: wrapperWith([mk('a'), mk('b'), mk('c')]),
    })
    await user.click(screen.getByRole('button', { name: /next session/i }))
    expect(onNavigateSession).toHaveBeenCalledWith('a')
  })

  it('hides the chevrons when there is only one session', () => {
    render(<SessionView sessionId="a" onBack={vi.fn()} onNavigateSession={vi.fn()} />, {
      wrapper: wrapperWith([mk('a')]),
    })
    expect(screen.queryByRole('button', { name: /next session/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /previous session/i })).not.toBeInTheDocument()
  })
})
