/**
 * SessionView ↔ SessionMeta integration: the status strip reflects account
 * (from useSessions, overridden by rotation), model + context (from reducer),
 * and account quota (from useAccounts).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { SessionView } from '../SessionView'
import type { SessionEvent } from '@/session/events'

let capturedHandlers: Record<string, ((...args: unknown[]) => void)[]> = {}

const mockOn = vi.fn().mockImplementation((event: string, cb: (...args: unknown[]) => void) => {
  if (!capturedHandlers[event]) capturedHandlers[event] = []
  capturedHandlers[event].push(cb)
})

const stableMockSocket = {
  attach: vi.fn(), detach: vi.fn(), interrupt: vi.fn(), prompt: vi.fn(),
  sendInterrupt: vi.fn(), permissionResponse: vi.fn(), answerQuestion: vi.fn(),
  setPermissionMode: vi.fn(), cancelQueued: vi.fn(),
  on: mockOn, off: vi.fn(), connectionState: 'connected' as const, socket: null,
}

vi.mock('@/ws/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/ws/client')>()
  return { ...actual, useSessionSocket: () => stableMockSocket }
})

vi.mock('@/api/hooks', () => ({
  useAdoptable: vi.fn(() => ({ data: { candidates: [] } })),
  useAdoptSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useSessions: () => ({ data: [{ id: 's1', name: 'Fix OAuth login bug', accountName: 'work-max', status: 'idle', createdAt: '', updatedAt: '' }] }),
  useAccounts: () => ({ data: [{ name: 'work-max', status: 'ok', usage: { session: { pct: 42, resetsAt: null }, weekly: { pct: 18, resetsAt: null } } }] }),
  useSettings: () => ({ data: undefined }),
  useModels: () => ({ data: [] }),
  useCloseSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  usePermanentDeleteSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useRenameSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useSessionsViewing: vi.fn(),
}))

function emitSessionEvent(event: SessionEvent) {
  for (const handler of capturedHandlers['sessionEvent'] ?? []) act(() => { handler(event) })
}
function emitRotated(msg: Record<string, unknown>) {
  for (const handler of capturedHandlers['rotated'] ?? []) act(() => { handler(msg) })
}

function wrapper({ children }: { children: React.ReactNode }) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
}

describe('SessionView status strip', () => {
  beforeEach(() => {
    capturedHandlers = {}
    mockOn.mockClear()
    mockOn.mockImplementation((event: string, cb: (...args: unknown[]) => void) => {
      if (!capturedHandlers[event]) capturedHandlers[event] = []
      capturedHandlers[event].push(cb)
    })
  })

  it('shows the session name (AI title) as the header title', () => {
    render(<SessionView sessionId="s1" onBack={vi.fn()} onDone={vi.fn()} />, { wrapper })
    // The same generated title shown on the dashboard session card appears in
    // the terminal header — not the raw sessionId prefix.
    expect(screen.getByText('Fix OAuth login bug')).toBeInTheDocument()
    expect(screen.queryByText('s1')).not.toBeInTheDocument()
  })

  it('shows the account from useSessions and quota from useAccounts', () => {
    render(<SessionView sessionId="s1" onBack={vi.fn()} onDone={vi.fn()} />, { wrapper })
    expect(screen.getByLabelText(/switch account/i)).toHaveValue('work-max')
    const quota = screen.getByLabelText(/account quota/i)
    expect(quota).toHaveTextContent('42%')
    expect(quota).toHaveTextContent('18%')
  })

  it('shows the model after a sessionMeta event', async () => {
    render(<SessionView sessionId="s1" onBack={vi.fn()} onDone={vi.fn()} />, { wrapper })
    emitSessionEvent({ type: 'sessionMeta', model: 'claude-sonnet-4-5', sessionId: 's1' })
    await waitFor(() => {
      expect(screen.getByLabelText(/select model/i)).toHaveValue('claude-sonnet-4-5')
    })
  })

  it('shows remaining context after a turnEnd with usage', async () => {
    render(<SessionView sessionId="s1" onBack={vi.fn()} onDone={vi.fn()} />, { wrapper })
    emitSessionEvent({ type: 'sessionMeta', model: 'claude-sonnet-4-5', sessionId: 's1' })
    // 50k of 200k used → 75% remaining
    emitSessionEvent({
      type: 'turnEnd', turnId: 't1',
      usage: { input_tokens: 10_000, output_tokens: 5, cache_read_input_tokens: 40_000 },
      costUsd: 0.2,
    })
    await waitFor(() => {
      expect(screen.getByLabelText(/remaining context/i)).toHaveTextContent('75%')
    })
  })

  it('overrides the account with the latest rotation target', async () => {
    render(<SessionView sessionId="s1" onBack={vi.fn()} onDone={vi.fn()} />, { wrapper })
    emitRotated({ type: 'rotated', sessionId: 's1', fromAccount: 'work-max', toAccount: 'backup-max' })
    await waitFor(() => {
      expect(screen.getByLabelText(/switch account/i)).toHaveValue('backup-max')
    })
  })
})
