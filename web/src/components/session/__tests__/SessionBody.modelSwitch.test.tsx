import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { SessionBody } from '../SessionBody'
import type { SessionRuntime } from '@/session/useSessionRuntime'
import type { Block } from '@/session/reducer'
import * as hooks from '@/api/hooks'
import { queryResult } from '@/test/reactQuery'
import { session as makeSession } from '@/test/factories'
import type { AgentCapabilities, EngineOffer } from '@/api/types'

// Mirror the same stubs as SessionBody.test.tsx, but with a real models list so
// the dialog can resolve the model label.
// `vi.hoisted` because the mock factory below is hoisted above ordinary consts.
const { useEngineOffers } = vi.hoisted(() => ({
  useEngineOffers: vi.fn((): { engine: string; capabilities?: unknown }[] => []),
}))

vi.mock('@/api/hooks', () => ({
  useModels: () => ({ data: [{ id: 'opus', label: 'Opus 4' }, { id: 'sonnet', label: 'Sonnet 4' }] }),
  useAccounts: () => ({ data: [] }),
  useSessions: vi.fn(() => ({ data: [] })),
  // Engine capabilities. `useEngineCapabilities` DELEGATES to the mocked
  // `useEngineOffers` rather than being a second stub, so a test that seeds
  // offers still drives the gate it is asserting on.
  useEngineOffers,
  useEngineCapabilities: (engine?: string) =>
    useEngineOffers().find(
      (o: { engine: string }) => o.engine === (engine ?? 'claude_code'),
    )?.capabilities,
  useSessionsViewing: vi.fn(),
  useModelApproval: vi.fn(() => ({ mutate: vi.fn() })),
  // Unpin account button (SessionBody → SessionMeta).
  useSetAccountPin: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useAddCopilotAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useCopilotAddStatus: vi.fn(() => ({ data: undefined, isLoading: false })),
  useCancelCopilotAdd: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useAssessDispatch: vi.fn(() => ({ assess: vi.fn() })),
}))

const textBlock = (id: string): Block => ({
  kind: 'text', id, text: 'hello',
})

function makeRuntime(statusState: string, overrides: Partial<SessionRuntime> = {}): SessionRuntime {
  return {
    sessionId: 's1',
    state: {
      turns: [{ id: 't1', role: 'assistant', blocks: [textBlock('b1')] }],
      openBlockId: null,
      status: { state: statusState, count: 0 },
      title: null, model: 'claude-opus-4-8', usage: null,
      replaying: false, error: null, queued: [], rotations: [], latestSuggestions: [],
      condense: null,
    },
    mode: 'interactive',
    toolDetailMode: 'auto',
    thinkingCollapsed: true,
    connectionState: 'connected',
    title: 'Test', account: 'acct', quota: null, contextPct: null, subAgents: [],
    // The picker binds to the session record's chosen alias, not the SDK id.
    model: 'opus', liveModel: 'claude-opus-4-8', modelPending: false,
    hasOpenAction: false, hasOpenPermission: false,
    armedQuestionId: null, chatArmed: false,
    onDecision: vi.fn(), onAnswer: vi.fn(), onChat: vi.fn(), onPrompt: vi.fn(),
    onInterrupt: vi.fn(), onSetMode: vi.fn(), onCancelQueued: vi.fn(),
    onCommand: vi.fn(), onSetModel: vi.fn(),
    ...overrides,
  } as unknown as SessionRuntime
}

function selectModel(value: string) {
  fireEvent.change(screen.getByLabelText('Select model'), { target: { value } })
}

describe('SessionBody — model switch confirmation on busy sessions', () => {
  it('busy session: selecting a different model opens the dialog and does NOT switch yet', () => {
    const rt = makeRuntime('working')
    render(<SessionBody rt={rt} />)
    selectModel('sonnet')
    expect(screen.getByRole('dialog', { name: 'Switch model?' })).toBeInTheDocument()
    // Copy uses the model LABEL, not the raw id
    expect(screen.getByRole('dialog')).toHaveTextContent('Sonnet 4')
    expect(rt.onSetModel).not.toHaveBeenCalled()
    expect(rt.onCommand).not.toHaveBeenCalled()
  })

  it('"Stop & switch now" switches immediately and closes the dialog', () => {
    const rt = makeRuntime('working')
    render(<SessionBody rt={rt} />)
    selectModel('sonnet')
    fireEvent.click(screen.getByRole('button', { name: 'Stop & switch now' }))
    expect(rt.onSetModel).toHaveBeenCalledWith('sonnet', 'now')
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('"Switch after this turn" defers the switch and closes the dialog', () => {
    const rt = makeRuntime('working')
    render(<SessionBody rt={rt} />)
    selectModel('sonnet')
    fireEvent.click(screen.getByRole('button', { name: 'Switch after this turn' }))
    expect(rt.onSetModel).toHaveBeenCalledWith('sonnet', 'after_turn')
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('cancel closes the dialog without switching anything', () => {
    const rt = makeRuntime('working')
    render(<SessionBody rt={rt} />)
    selectModel('sonnet')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(rt.onSetModel).not.toHaveBeenCalled()
    expect(screen.queryByRole('dialog')).toBeNull()
    // The picker is controlled by the record choice, so it snaps back to it.
    expect(screen.getByLabelText('Select model')).toHaveValue('opus')
  })

  it('non-busy session: selecting a model switches immediately, no dialog', () => {
    const rt = makeRuntime('done') // 'done' is your-turn tier, not busy
    render(<SessionBody rt={rt} />)
    selectModel('sonnet')
    expect(rt.onSetModel).toHaveBeenCalledWith('sonnet', 'now')
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('selecting the current model is a no-op (no dialog, no command), even while busy', () => {
    const rt = makeRuntime('working')
    render(<SessionBody rt={rt} />)
    selectModel('opus') // current model
    expect(rt.onSetModel).not.toHaveBeenCalled()
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('the picker shows the chosen alias even while the live turn still runs the old model', () => {
    // After "switch after this turn": record says sonnet, the in-flight turn is
    // still producing on opus. The picker must NOT snap back to the live id —
    // that snap-back is what made every session look pinned to the default.
    const rt = makeRuntime('working', { model: 'sonnet', liveModel: 'claude-opus-4-8', modelPending: true })
    render(<SessionBody rt={rt} />)
    expect(screen.getByLabelText('Select model')).toHaveValue('sonnet')
    expect(screen.getByLabelText('Model in use')).toHaveTextContent('Opus 4.8')
    expect(screen.getByLabelText('Model switch pending')).toHaveTextContent(/after this turn/i)
  })

  // The non-interrupting choice is labelled from the engine's `steer`
  // capability, never from an engine-name comparison: an engine that can steer
  // retargets the turn already running instead of letting it finish.
  it('offers "switch without stopping" for an engine that can steer a running turn', () => {
    vi.mocked(hooks.useSessions).mockReturnValue(
      queryResult([makeSession({ id: 's1', engine: 'codex' })]),
    )
    const capabilities: AgentCapabilities = {
      models: true, reasoningEffort: true, steer: true, interrupt: true, approvals: true,
      questions: false, fork: false, adopt: false, selfModelSwitch: false, subagents: false,
      attachments: true, accountRotation: false,
    }
    const offer: EngineOffer = {
      engine: 'codex', available: true, hosts: ['dobby'], unavailableReasons: {}, capabilities,
    }
    vi.mocked(hooks.useEngineOffers).mockReturnValue([offer])
    const rt = makeRuntime('working')
    render(<SessionBody rt={rt} />)
    selectModel('sonnet')
    fireEvent.click(screen.getByRole('button', { name: 'Switch without stopping' }))
    expect(rt.onSetModel).toHaveBeenCalledWith('sonnet', 'after_turn')
  })
})
