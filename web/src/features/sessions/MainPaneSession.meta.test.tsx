/**
 * MainPaneSession ↔ session rail integration (D-6; was the `SessionMeta`
 * strip): on a pane of 64rem or more the rail reflects the account (from
 * useSessions, overridden by rotation) and the account quota (from
 * useAccounts); the model and context live in the docked prompt row.
 *
 * Was `SessionView.meta.test.tsx`. `SessionView` is deleted (Task 18 / Item 2);
 * the pane the shell actually renders is `MainPaneSession`, and it reads the
 * runtime from the provider `PageHost` mounts, so the harness supplies one.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { MainPaneSession } from './MainPaneSession'
import { SessionRuntimeProvider, useSessionRuntimeContext } from '@/features/sessions/SessionRuntimeProvider'
import { SessionComposer } from '@/features/sessions/composer/SessionComposer'
import type { SessionEvent } from '@/session/events'
import { anthropicUsage } from '@/test/factories'
import { setElementWidth, resetElementWidth } from '@/test/elementWidth'

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
  // Rail cards (SessionBody → SessionRail): MCP list, rules, account pin.
  useSessionMcp: vi.fn(() => ({ data: undefined })),
  usePermissionRules: vi.fn(() => ({ data: [] })),
  useMemoryFile: vi.fn(() => ({ data: undefined })),
  useRevokePermissionRule: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
  useReconnectSessionMcp: vi.fn(() => ({ mutate: vi.fn(), isPending: false, isError: false, error: null })),
  // "Reload session" (MainPaneSession → SessionHeader → SessionActions).
  useReloadSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  // "Next best account" (MainPaneSession → SessionHeader → SessionActions).
  useRotateSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
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
  useResolveSuspectedComplete: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useMarkSessionViewed: vi.fn(() => ({ mutate: vi.fn() })),
  // The worktree diff summary (SessionBody's Changes tab, SessionHeader's +N −M chip).
  useSessionDiff: vi.fn(() => ({ data: undefined })),
  useForkSession: vi.fn(() => ({ fork: vi.fn(), isPending: false })),
  useRewindSession: vi.fn(() => ({ preview: vi.fn(), rewind: vi.fn() })),
  useUndoRewind: vi.fn(() => ({ undo: vi.fn(), isPending: false })),
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
      <MemoryRouter initialEntries={['/sessions/s1']}>
        <SessionRuntimeProvider sessionId="s1">{children}</SessionRuntimeProvider>
      </MemoryRouter>
    </QueryClientProvider>
  )
}

function renderPane() {
  return render(
    <MainPaneSession sessionId="s1" onClosed={vi.fn()} onDetach={vi.fn()} />,
    { wrapper },
  )
}

/** The model picker lives in the composer's prompt row, which the shell mounts
 *  beside the pane (PromptDock) from the same runtime provider. */
function DockedComposer() {
  return <SessionComposer rt={useSessionRuntimeContext()} />
}

describe('MainPaneSession session rail', () => {
  // A wide pane: the account and quota readouts are in the rail column.
  afterEach(() => resetElementWidth())
  beforeEach(() => {
    setElementWidth(1200)
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
    expect(screen.getByRole('heading', { level: 1, name: 'Fix OAuth login bug' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { level: 1, name: 's1' })).not.toBeInTheDocument()
  })

  it('shows the account from useSessions and quota from useAccounts', async () => {
    renderPane()
    expect(await screen.findByLabelText(/switch account/i)).toHaveValue('work-max')
    const quota = screen.getByLabelText(/account quota/i)
    expect(quota).toHaveTextContent('42%')
    expect(quota).toHaveTextContent('18%')
  })

  it('shows the live model after a sessionMeta event, while the picker stays on the record choice', async () => {
    render(
      <>
        <MainPaneSession sessionId="s1" onClosed={vi.fn()} onDetach={vi.fn()} />
        <DockedComposer />
      </>,
      { wrapper },
    )
    emitSessionEvent({ type: 'sessionMeta', model: 'claude-sonnet-4-5', sessionId: 's1' })
    await waitFor(() => {
      expect(screen.getByLabelText(/model in use/i)).toHaveTextContent('Sonnet 4.5')
    })
    // The SDK's launch model is what the account defaulted to, not a user choice:
    // the picker reflects the session record (no model → "Default"). Binding the
    // picker to sessionMeta is what made every session look pinned to the default.
    expect(screen.getByRole('button', { name: /^Model:/ })).toHaveTextContent('Default')
  })

  it('shows context usage in the prompt row after a turnEnd with usage', async () => {
    render(
      <>
        <MainPaneSession sessionId="s1" onClosed={vi.fn()} onDetach={vi.fn()} />
        <DockedComposer />
      </>,
      { wrapper },
    )
    emitSessionEvent({ type: 'sessionMeta', model: 'claude-sonnet-4-5', sessionId: 's1' })
    // 50k of 200k used → 25% used (75% remaining)
    emitSessionEvent({
      type: 'turnEnd', turnId: 't1',
      usage: { input_tokens: 10_000, output_tokens: 5, cache_read_input_tokens: 40_000 },
      costUsd: 0.2,
    })
    await waitFor(() => {
      expect(screen.getByRole('img', { name: 'Context 25% used' })).toBeInTheDocument()
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
