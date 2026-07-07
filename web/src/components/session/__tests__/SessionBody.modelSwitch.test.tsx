import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { SessionBody } from '../SessionBody'
import type { SessionRuntime } from '@/session/useSessionRuntime'
import type { Block } from '@/session/reducer'

// Mirror the same stubs as SessionBody.test.tsx, but with a real models list so
// the dialog can resolve the model label.
vi.mock('@/api/hooks', () => ({
  useModels: () => ({ data: [{ id: 'opus', label: 'Opus 4' }, { id: 'sonnet', label: 'Sonnet 4' }] }),
  useAccounts: () => ({ data: [] }),
  useSessions: () => ({ data: [] }),
  useSessionsViewing: vi.fn(),
  useAddCopilotAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useCopilotAddStatus: vi.fn(() => ({ data: undefined, isLoading: false })),
  useCancelCopilotAdd: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
}))

const textBlock = (id: string): Block => ({
  kind: 'text', id, text: 'hello', streaming: false, thinkingMs: null,
})

function makeRuntime(statusState: string): SessionRuntime {
  return {
    sessionId: 's1',
    state: {
      turns: [{ id: 't1', role: 'assistant', blocks: [textBlock('b1')] }],
      openBlockId: null,
      status: { state: statusState, count: 0 },
      title: null, model: 'opus', usage: null,
      replaying: false, error: null, queued: [], rotations: [],
      condense: null,
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
    onCommand: vi.fn(),
  } as unknown as SessionRuntime
}

function selectModel(value: string) {
  fireEvent.change(screen.getByLabelText('Select model'), { target: { value } })
}

describe('SessionBody — model switch confirmation on busy sessions', () => {
  it('busy session: selecting a different model opens the dialog and does NOT send the command yet', () => {
    const rt = makeRuntime('working')
    render(<SessionBody rt={rt} />)
    selectModel('sonnet')
    expect(screen.getByRole('dialog', { name: 'Switch model?' })).toBeInTheDocument()
    // Copy uses the model LABEL, not the raw id
    expect(screen.getByRole('dialog')).toHaveTextContent('Sonnet 4')
    expect(rt.onCommand).not.toHaveBeenCalled()
  })

  it('confirm sends the model command and closes the dialog', () => {
    const rt = makeRuntime('working')
    render(<SessionBody rt={rt} />)
    selectModel('sonnet')
    fireEvent.click(screen.getByRole('button', { name: 'Stop & switch' }))
    expect(rt.onCommand).toHaveBeenCalledWith('model', 'sonnet')
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('cancel closes the dialog without sending anything', () => {
    const rt = makeRuntime('working')
    render(<SessionBody rt={rt} />)
    selectModel('sonnet')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(rt.onCommand).not.toHaveBeenCalled()
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('non-busy session: selecting a model sends the command immediately, no dialog', () => {
    const rt = makeRuntime('done') // 'done' is your-turn tier, not busy
    render(<SessionBody rt={rt} />)
    selectModel('sonnet')
    expect(rt.onCommand).toHaveBeenCalledWith('model', 'sonnet')
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('selecting the current model is a no-op (no dialog, no command), even while busy', () => {
    const rt = makeRuntime('working')
    render(<SessionBody rt={rt} />)
    selectModel('opus') // current model
    expect(rt.onCommand).not.toHaveBeenCalled()
    expect(screen.queryByRole('dialog')).toBeNull()
  })
})
