import { render, screen } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { SessionBody } from '../SessionBody'
import type { SessionRuntime } from '@/session/useSessionRuntime'
import type { Block } from '@/session/reducer'

// Mirror the same stubs as SessionBody.test.tsx
vi.mock('@/api/hooks', () => ({
  useModels: () => ({ data: [] }),
  useAccounts: () => ({ data: [] }),
  useSessions: () => ({ data: [] }),
  useSessionsViewing: vi.fn(),
  useAddCopilotAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useCopilotAddStatus: vi.fn(() => ({ data: undefined, isLoading: false })),
  useCancelCopilotAdd: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useAssessDispatch: vi.fn(() => ({ assess: vi.fn() })),
}))

const textBlock = (id: string): Block => ({
  kind: 'text', id, text: 'hello', streaming: false, thinkingMs: null,
})

function makeRuntime(
  overrides: Partial<SessionRuntime['state']> = {},
): SessionRuntime {
  return {
    sessionId: 's1',
    state: {
      turns: [{ id: 't1', role: 'assistant', blocks: [textBlock('b1')] }],
      openBlockId: null,
      status: { state: 'idle', count: 0 },
      title: null, model: null, usage: null,
      replaying: false, error: null, queued: [], rotations: [],
      condense: null,
      ...overrides,
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

describe('SessionBody — condensing indicator', () => {
  it('renders CondensingProgress (progressbar) instead of WorkingIndicator when condensing', () => {
    const rt = makeRuntime({
      status: { state: 'condensing', count: 0 },
      condense: { active: true, expectedMs: 9000 },
    })
    render(<SessionBody rt={rt} />)
    // progressbar role is unique to CondensingProgress — StatusBar doesn't render one
    expect(screen.getByRole('progressbar')).toBeInTheDocument()
    // The font-medium label is unique to CondensingProgress (StatusBar uses working-shimmer class)
    expect(screen.getByText('Condensing context…', { selector: '.font-medium' })).toBeInTheDocument()
    // The bottom busy-tier renders CondensingProgress, not a WorkingIndicator;
    // the StatusBar's own WorkingIndicator (inside SessionMeta) still renders a spinner,
    // so there IS a working-spinner in the tree — but only from the status bar, not the bottom tier.
    // Confirm: the progressbar (bottom CondensingProgress) is present alongside the status-bar spinner.
    expect(screen.getAllByTestId('working-spinner')).toHaveLength(1) // only the StatusBar's spinner
  })

  it('renders the working spinner in the bottom tier (not progressbar) for normal in_progress status', () => {
    const rt = makeRuntime({
      status: { state: 'in_progress', count: 1 },
      condense: null,
    })
    render(<SessionBody rt={rt} />)
    // Two working-spinners: one in StatusBar, one in the bottom WorkingIndicator
    expect(screen.getAllByTestId('working-spinner')).toHaveLength(2)
    expect(screen.queryByRole('progressbar')).toBeNull()
  })
})
