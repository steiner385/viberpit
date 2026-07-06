import { render, screen } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { SessionBody } from '../SessionBody'
import type { SessionRuntime } from '@/session/useSessionRuntime'
import type { Block } from '@/session/reducer'

// SessionBody now calls useModels/useAccounts/useSessions (account+model switchers).
// Stub them so this layout test needs no QueryClientProvider or network.
vi.mock('@/api/hooks', () => ({
  useModels: () => ({ data: [] }),
  useAccounts: () => ({ data: [] }),
  useSessions: () => ({ data: [] }),
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

describe('SessionBody', () => {
  it('renders the pinned answer panel for a LIVE open question (desktop + mobile)', () => {
    render(<SessionBody rt={makeRuntime([question('a', true)])} />)
    expect(screen.getByTestId('pending-question-panel')).toBeInTheDocument()
  })

  it('does not render the panel when the only question is historical (replayed)', () => {
    render(<SessionBody rt={makeRuntime([question('a', false)])} />)
    expect(screen.queryByTestId('pending-question-panel')).toBeNull()
  })
})
