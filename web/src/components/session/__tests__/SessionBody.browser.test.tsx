/**
 * Task 12b: Conversation|Browser tab strip integration tests.
 * Tests gate the Browser tab on /api/x11/metrics response.
 */

import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { SessionBody } from '../SessionBody'
import type { SessionRuntime } from '@/session/useSessionRuntime'

// Stub BrowserTab so noVNC and vncClient never load
vi.mock('../../BrowserTab', () => ({
  BrowserTab: ({ sessionId }: { sessionId: string }) => (
    <div data-testid="browser-tab-stub">BrowserTab:{sessionId}</div>
  ),
}))

function makeRuntime(): SessionRuntime {
  return {
    sessionId: 's1',
    state: {
      turns: [],
      openBlockId: null,
      status: { state: 'idle', count: 0 },
      title: null, model: null, usage: null,
      replaying: false, error: null, queued: [], rotations: [],
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
    render(<SessionBody rt={makeRuntime()} />)
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
    render(<SessionBody rt={makeRuntime()} />)
    await waitFor(() => {
      expect(screen.getByRole('tab', { name: /browser/i })).toBeInTheDocument()
    })
    await userEvent.click(screen.getByRole('tab', { name: /browser/i }))
    expect(screen.getByTestId('browser-tab-stub')).toBeInTheDocument()
  })

  it('hides Browser tab when /api/x11/metrics returns {enabled: false}', async () => {
    vi.spyOn(global, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ enabled: false }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    )
    render(<SessionBody rt={makeRuntime()} />)
    // Give it time to resolve
    await waitFor(() => {
      expect(screen.queryByRole('tab', { name: /browser/i })).toBeNull()
    })
  })

  it('hides Browser tab when fetch rejects with a network error', async () => {
    vi.spyOn(global, 'fetch').mockRejectedValue(new Error('Network error'))
    render(<SessionBody rt={makeRuntime()} />)
    await waitFor(() => {
      expect(screen.queryByRole('tab', { name: /browser/i })).toBeNull()
    })
  })

  it('conversation container has no role=tabpanel when browser feature is disabled (fetch rejects)', async () => {
    vi.spyOn(global, 'fetch').mockRejectedValue(new Error('Network error'))
    render(<SessionBody rt={makeRuntime()} />)
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
    render(<SessionBody rt={makeRuntime()} />)
    await waitFor(() => {
      expect(screen.queryByRole('tab', { name: /browser/i })).toBeNull()
    })
    expect(screen.queryByRole('tabpanel')).toBeNull()
  })
})
