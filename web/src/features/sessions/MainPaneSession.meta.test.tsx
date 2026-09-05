/**
 * MainPaneSession ↔ SessionMeta integration: the status strip reflects account
 * (from useSessions, overridden by rotation), model + context (from reducer),
 * and account quota (from useAccounts).
 *
 * Was `SessionView.meta.test.tsx`. `SessionView` is deleted (Task 18 / Item 2);
 * the pane the shell actually renders is `MainPaneSession`, and it reads the
 * runtime from the provider `PageHost` mounts, so the harness supplies one.
 * Every assertion is unchanged.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MainPaneSession } from './MainPaneSession'
import { SessionRuntimeProvider } from '@/components/session/SessionRuntimeProvider'
import type { SessionEvent } from '@/session/events'
import { anthropicUsage } from '@/test/factories'

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
  // "Reload session" (MainPaneSession → SessionHeader → SessionActions).
  useReloadSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  // "Next best account" (MainPaneSession → SessionHeader → SessionActions).
  useRotateSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  // Unpin account button (MainPaneSession → SessionBody → SessionMeta).
  useSetAccountPin: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useGenerateSuggestions: vi.fn(() => ({ mutate: vi.fn(), mutateAsync: vi.fn(() => Promise.resolve([])), isPending: false })),
  useAdoptable: vi.fn(() => ({ data: { candidates: [] } })),
  useAdoptSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useSessions: () => ({ data: [{ id: 's1', name: 'Fix OAuth login bug', accountName: 'work-max', status: 'idle', createdAt: '', updatedAt: '' }] }),
  useAccounts: () => ({ data: [{ name: 'work-max', status: 'ok', usage: anthropicUsage(42, 18) }] }),
  useSettings: () => ({ data: undefined }),
  useModels: () => ({ data: [] }),
  useCloseSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  usePermanentDeleteSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useRenameSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  // Engine capabilities (SessionBody gates Claude-only surfaces on them).
  useEngineOffers: vi.fn(() => []),
  // Capability gate for engine-conditional controls; undefined = "unknown",
  // which every gate reads as "capability present".
  useEngineCapabilities: vi.fn(() => undefined),
  useSessionsViewing: vi.fn(),
  useModelApproval: vi.fn(() => ({ mutate: vi.fn() })),
  useAddCopilotAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useCopilotAddStatus: vi.fn(() => ({ data: undefined, isLoading: false })),
  useCancelCopilotAdd: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useAssessDispatch: vi.fn(() => ({ assess: vi.fn() })),
}))

function emitSessionEvent(event: SessionEvent) {
  for (const handler of capturedHandlers['sessionEvent'] ?? []) act(() => { handler(event) })
}
function emitRotated(msg: Record<string, unknown>) {
  for (const handler of capturedHandlers['rotated'] ?? []) act(() => { handler(msg) })
}

function wrapper({ children }: { children: React.ReactNode }) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={queryClient}>
      <SessionRuntimeProvider sessionId="s1">{children}</SessionRuntimeProvider>
    </QueryClientProvider>
  )
}

function renderPane() {
  return render(
    <MainPaneSession sessionId="s1" onClosed={vi.fn()} onDetach={vi.fn()} onOpenSession={vi.fn()} />,
    { wrapper },
  )
}

describe('MainPaneSession status strip', () => {
  beforeEach(() => {
    capturedHandlers = {}
    mockOn.mockClear()
    mockOn.mockImplementation((event: string, cb: (...args: unknown[]) => void) => {
      if (!capturedHandlers[event]) capturedHandlers[event] = []
      capturedHandlers[event].push(cb)
    })
  })

  it('shows the session name (AI title) as the header title', () => {
    renderPane()
    // The same generated title shown on the dashboard session card appears in
    // the terminal header — not the raw sessionId prefix.
    expect(screen.getByText('Fix OAuth login bug')).toBeInTheDocument()
    expect(screen.queryByText('s1')).not.toBeInTheDocument()
  })

  it('shows the account from useSessions and quota from useAccounts', () => {
    renderPane()
    expect(screen.getByLabelText(/switch account/i)).toHaveValue('work-max')
    const quota = screen.getByLabelText(/account quota/i)
    expect(quota).toHaveTextContent('42%')
    expect(quota).toHaveTextContent('18%')
  })

  it('shows the live model after a sessionMeta event, while the picker stays on the record choice', async () => {
    renderPane()
    emitSessionEvent({ type: 'sessionMeta', model: 'claude-sonnet-4-5', sessionId: 's1' })
    await waitFor(() => {
      expect(screen.getByLabelText(/model in use/i)).toHaveTextContent('Sonnet 4.5')
    })
    // The SDK's launch model is what the account defaulted to, not a user choice:
    // the picker reflects the session record (no model → "Default"). Binding the
    // picker to sessionMeta is what made every session look pinned to the default.
    expect(screen.getByLabelText(/select model/i)).toHaveValue('default')
  })

  it('shows remaining context after a turnEnd with usage', async () => {
    renderPane()
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
    renderPane()
    emitRotated({ type: 'rotated', sessionId: 's1', fromAccount: 'work-max', toAccount: 'backup-max' })
    await waitFor(() => {
      expect(screen.getByLabelText(/switch account/i)).toHaveValue('backup-max')
    })
  })
})
