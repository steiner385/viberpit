import { render, screen } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { SessionBody } from '../SessionBody'
import type { SessionRuntime } from '@/session/useSessionRuntime'
import type { Block } from '@/session/reducer'
import * as hooks from '@/api/hooks'

// SessionBody now calls useModels/useAccounts/useSessions (account+model switchers).
// Stub them so this layout test needs no QueryClientProvider or network.
vi.mock('@/api/hooks', () => ({
  useModels: vi.fn(() => ({ data: [] })),
  useAccounts: vi.fn(() => ({ data: [] })),
  useSessions: vi.fn(() => ({ data: [] })),
  useSessionsViewing: vi.fn(),
  useAddCopilotAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useCopilotAddStatus: vi.fn(() => ({ data: undefined, isLoading: false })),
  useCancelCopilotAdd: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
}))

// Regression: the desktop master-detail shell renders SessionBody but previously
// did NOT render PendingQuestionPanel, so a live AskUserQuestion (filtered out of
// the inline transcript by Turn) appeared nowhere on desktop and was unanswerable.
// PendingQuestionPanel now lives inside SessionBody, so both layouts surface it.

const question = (id: string, live: boolean): Block => ({
  kind: 'question', id, questionId: id, header: id, question: `${id}?`,
  options: [{ label: 'Yes' }], multiSelect: false, live,
})

function makeRuntime(blocks: Block[]): SessionRuntime {
  return {
    sessionId: 's1',
    state: {
      turns: [{ id: 't1', role: 'assistant', blocks }],
      openBlockId: null,
      status: { state: 'idle', count: 0 },
      title: null, model: null, usage: null,
      replaying: false, error: null, queued: [], rotations: [],
    },
    mode: 'interactive',
    toolDetailMode: 'auto',
    thinkingCollapsed: true,
    connectionState: 'connected',
    title: 'Test', account: 'acct', quota: null, contextPct: null, subAgents: [],
    hasOpenAction: false, hasOpenPermission: false,
    armedQuestionId: null, chatArmed: false,
    onDecision: vi.fn(), onAnswer: vi.fn(), onChat: vi.fn(), onPrompt: vi.fn(),
    onInterrupt: vi.fn(), onSetMode: vi.fn(), onCancelQueued: vi.fn(),
  } as unknown as SessionRuntime
}

function makeEmptyRuntime(): SessionRuntime {
  return {
    sessionId: 's1',
    state: {
      turns: [],
      openBlockId: null,
      status: { state: 'idle', count: 0 },
      title: null, model: null, usage: null,
      replaying: false, error: null, queued: [], rotations: [],
    },
    mode: 'interactive',
    toolDetailMode: 'auto',
    thinkingCollapsed: true,
    connectionState: 'connected',
    title: 'Test', account: 'acct', quota: null, contextPct: null, subAgents: [],
    hasOpenAction: false, hasOpenPermission: false,
    armedQuestionId: null, chatArmed: false,
    onDecision: vi.fn(), onAnswer: vi.fn(), onChat: vi.fn(), onPrompt: vi.fn(),
    onInterrupt: vi.fn(), onSetMode: vi.fn(), onCancelQueued: vi.fn(),
  } as unknown as SessionRuntime
}

describe('SessionBody', () => {
  beforeEach(() => {
    vi.mocked(hooks.useModels).mockReturnValue({ data: [] } as ReturnType<typeof hooks.useModels>)
    vi.mocked(hooks.useAccounts).mockReturnValue({ data: [] } as ReturnType<typeof hooks.useAccounts>)
    vi.mocked(hooks.useSessions).mockReturnValue({ data: [] } as ReturnType<typeof hooks.useSessions>)
  })

  it('renders the pinned answer panel for a LIVE open question (desktop + mobile)', () => {
    render(<SessionBody rt={makeRuntime([question('a', true)])} />)
    expect(screen.getByTestId('pending-question-panel')).toBeInTheDocument()
  })

  it('does not render the panel when the only question is historical (replayed)', () => {
    render(<SessionBody rt={makeRuntime([question('a', false)])} />)
    expect(screen.queryByTestId('pending-question-panel')).toBeNull()
  })

  it('renders "Queued — position 3" placeholder for a queued session with queuePosition 3', () => {
    vi.mocked(hooks.useSessions).mockReturnValue({
      data: [{ id: 's1', status: 'queued', queuePosition: 3 }],
    } as ReturnType<typeof hooks.useSessions>)
    render(<SessionBody rt={makeEmptyRuntime()} />)
    expect(screen.getByText(/Queued — position 3/i)).toBeInTheDocument()
  })
})
