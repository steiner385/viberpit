/**
 * Task 12b: Conversation|Browser tab strip integration tests.
 * Tests gate the Browser tab on /api/x11/metrics response.
 */

import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { SessionBody } from './SessionBody'
import type { SessionRuntime } from '@/session/useSessionRuntime'

function makeQueryClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } })
}

function renderWithQuery(ui: React.ReactElement, path = '/sessions/s1') {
  return render(
    <QueryClientProvider client={makeQueryClient()}>
      <MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  )
}

// Stub BrowserTab so noVNC and vncClient never load
vi.mock('@/features/sessions/browser/BrowserTab', () => ({
  BrowserTab: ({ sessionId }: { sessionId: string }) => (
    <div data-testid="browser-tab-stub">BrowserTab:{sessionId}</div>
  ),
}))

// Stub the hooks so SessionBody's query calls return safe empty data
// without firing real fetch requests (which would conflict with the spied fetch).
vi.mock('@/api/hooks', () => ({
  // In-session MCP readout (SessionBody → SessionMeta mcpSlot).
  useSessionMcp: vi.fn(() => ({ data: undefined })),
  // Per-session standing rules readout (SessionBody → SessionMeta rulesSlot).
  usePermissionRules: vi.fn(() => ({ data: [] })),
  useRevokePermissionRule: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
  useReconnectSessionMcp: vi.fn(() => ({ mutate: vi.fn(), isPending: false, isError: false, error: null })),
  useModels: () => ({ data: [] }),
  useAccounts: () => ({ data: [] }),
  useSessions: () => ({ data: [] }),
  // Engine capabilities (SessionBody gates Claude-only surfaces on them).
  useEngineOffers: vi.fn(() => []),
  // Capability gate for engine-conditional controls; undefined = "unknown",
  // which every gate reads as "capability present".
  useEngineCapabilities: vi.fn(() => undefined),
  useSessionsViewing: vi.fn(),
  useModelApproval: vi.fn(() => ({ mutate: vi.fn() })),
  useResolveSuspectedComplete: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  // Unpin account button (SessionBody → SessionMeta).
  useSetAccountPin: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useAddCopilotAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useCopilotAddStatus: vi.fn(() => ({ data: undefined, isLoading: false })),
  useCancelCopilotAdd: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useAssessDispatch: vi.fn(() => ({ assess: vi.fn() })),
}))

function makeRuntime(): SessionRuntime {
  return {
    sessionId: 's1',
    state: {
      turns: [],
      openBlockId: null,
      status: { state: 'idle', count: 0 },
      title: null, model: null, usage: null,
      replaying: false, error: null, queued: [], rotations: [], latestSuggestions: [],
      subAgents: [],
    },
    mode: 'interactive',
    toolDetailMode: 'auto',
    toggleToolDetail: vi.fn(),
    connectionState: 'connected',
    title: 'Test', account: 'acct', quota: null, contextPct: null, subAgents: [],
    hasOpenAction: false, hasOpenPermission: false,
    armedQuestionId: null, chatArmed: false,
    onDecision: vi.fn(), onAnswer: vi.fn(), onChat: vi.fn(), onPrompt: vi.fn(),
    onInterrupt: vi.fn(), onSetMode: vi.fn(), onCancelQueued: vi.fn(),
  } as unknown as SessionRuntime
}

describe('SessionBody — Browser tab gating', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('shows Browser tab when /api/x11/metrics returns enabled metrics', async () => {
    vi.spyOn(global, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ active_displays: 1, total_sessions: 1 }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    )
    renderWithQuery(<SessionBody rt={makeRuntime()} />)
    await waitFor(() => {
      expect(screen.getByRole('tab', { name: /browser/i })).toBeInTheDocument()
    })
  })

  it('renders BrowserTab stub when Browser tab is clicked', async () => {
    vi.spyOn(global, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ active_displays: 1, total_sessions: 1 }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    )
    renderWithQuery(<SessionBody rt={makeRuntime()} />)
    await waitFor(() => {
      expect(screen.getByRole('tab', { name: /browser/i })).toBeInTheDocument()
    })
    await userEvent.click(screen.getByRole('tab', { name: /browser/i }))
    // The viewer is loaded lazily (#1411), so it arrives a microtask after the click.
    expect(await screen.findByTestId('browser-tab-stub')).toBeInTheDocument()
  })

  it('hides Browser tab when /api/x11/metrics returns {enabled: false}', async () => {
    vi.spyOn(global, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ enabled: false }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    )
    renderWithQuery(<SessionBody rt={makeRuntime()} />)
    // Give it time to resolve
    await waitFor(() => {
      expect(screen.queryByRole('tab', { name: /browser/i })).toBeNull()
    })
  })

  it('hides Browser tab when fetch rejects with a network error', async () => {
    vi.spyOn(global, 'fetch').mockRejectedValue(new Error('Network error'))
    renderWithQuery(<SessionBody rt={makeRuntime()} />)
    await waitFor(() => {
      expect(screen.queryByRole('tab', { name: /browser/i })).toBeNull()
    })
  })

  it('conversation container has no role=tabpanel when browser feature is disabled (fetch rejects)', async () => {
    vi.spyOn(global, 'fetch').mockRejectedValue(new Error('Network error'))
    renderWithQuery(<SessionBody rt={makeRuntime()} />)
    // Wait for the fetch to settle (browserEnabled stays false)
    await waitFor(() => {
      expect(screen.queryByRole('tab', { name: /browser/i })).toBeNull()
    })
    // The conversation container must NOT carry tab ARIA (dangling reference)
    expect(screen.queryByRole('tabpanel')).toBeNull()
  })

  it('conversation container has no role=tabpanel when metrics report disabled', async () => {
    vi.spyOn(global, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ enabled: false }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    )
    renderWithQuery(<SessionBody rt={makeRuntime()} />)
    await waitFor(() => {
      expect(screen.queryByRole('tab', { name: /browser/i })).toBeNull()
    })
    expect(screen.queryByRole('tabpanel')).toBeNull()
  })

  it('the strip is the shared Tabs primitive: roving tabindex, ?view=browser deep-links', async () => {
    vi.spyOn(global, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ active_displays: 1, total_sessions: 1 }), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      }),
    )
    renderWithQuery(<SessionBody rt={makeRuntime()} />, '/sessions/s1?view=browser')
    await waitFor(() => expect(screen.getByRole('tab', { name: /browser/i })).toHaveAttribute('aria-selected', 'true'))
    expect(screen.getByRole('tab', { name: /conversation/i })).toHaveAttribute('tabindex', '-1')
    expect(screen.getByTestId('browser-tab-stub')).toBeInTheDocument()
  })

  it('?view=browser is ignored while the Browser view is unavailable', async () => {
    vi.spyOn(global, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ enabled: false }), { status: 200, headers: { 'Content-Type': 'application/json' } }),
    )
    renderWithQuery(<SessionBody rt={makeRuntime()} />, '/sessions/s1?view=browser')
    await waitFor(() => expect(screen.queryByRole('tab', { name: /browser/i })).toBeNull())
    expect(screen.queryByTestId('browser-tab-stub')).toBeNull()
  })
})
