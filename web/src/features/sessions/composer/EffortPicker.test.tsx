import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { EffortPicker } from './EffortPicker'
import { PromptRow } from './PromptRow'
import type { SessionRuntime } from '@/session/useSessionRuntime'
import type { AgentCapabilities, ModelOption } from '@/api/types'

const CODEX_CAPS: AgentCapabilities = {
  models: true, reasoningEffort: true, steer: true, interrupt: true, approvals: true, questions: true,
  fork: false, adopt: false, selfModelSwitch: false, subagents: false, attachments: true,
  accountRotation: false, accountSwitchNewThread: true,
}

const CODEX_MODELS: ModelOption[] = [
  { id: 'gpt-5', label: 'GPT-5', reasoningEfforts: ['low', 'medium', 'high'], defaultReasoningEffort: 'medium' },
  { id: 'gpt-5-mini', label: 'GPT-5 mini' },
]

function fakeRuntime(over: Partial<SessionRuntime> = {}): SessionRuntime {
  return {
    sessionId: 's1', mode: 'auto', pendingMode: null, onSetMode: vi.fn(),
    model: 'gpt-5', liveModel: null, modelPending: false, onSetModel: vi.fn(),
    state: { status: { state: 'idle', count: 0 } },
    ...over,
  } as unknown as SessionRuntime
}

const effortTrigger = () => screen.queryByRole('button', { name: /^Effort:/ })

describe('EffortPicker', () => {
  it('shows the current effort and lists the offered efforts', async () => {
    const onSelect = vi.fn()
    render(<EffortPicker effort="medium" efforts={['low', 'medium', 'high']} onSelect={onSelect} />)
    expect(effortTrigger()).toHaveTextContent('medium')
    await userEvent.click(effortTrigger()!)
    expect(screen.getByRole('menuitemradio', { name: /^medium/ })).toHaveAttribute('aria-checked', 'true')
    await userEvent.click(screen.getByRole('menuitemradio', { name: /^high/ }))
    expect(onSelect).toHaveBeenCalledWith('high')
  })

  it('selecting the current effort is a no-op', async () => {
    const onSelect = vi.fn()
    render(<EffortPicker effort="medium" efforts={['low', 'medium', 'high']} onSelect={onSelect} />)
    await userEvent.click(effortTrigger()!)
    await userEvent.click(screen.getByRole('menuitemradio', { name: /^medium/ }))
    expect(onSelect).not.toHaveBeenCalled()
  })
})

describe('PromptRow effort gating', () => {
  it('is absent when the engine lacks the reasoningEffort capability', () => {
    render(
      <PromptRow rt={fakeRuntime()} session={{ engine: 'codex' }} models={CODEX_MODELS}
        caps={{ ...CODEX_CAPS, reasoningEffort: false }} />,
    )
    expect(effortTrigger()).toBeNull()
  })

  it('is absent when the chosen model offers no reasoningEfforts', () => {
    render(
      <PromptRow rt={fakeRuntime({ model: 'gpt-5-mini' })} session={{ engine: 'codex' }}
        models={CODEX_MODELS} caps={CODEX_CAPS} />,
    )
    expect(effortTrigger()).toBeNull()
  })

  it("shows the session's effort, else the model default", () => {
    const { rerender } = render(
      <PromptRow rt={fakeRuntime()} session={{ engine: 'codex', reasoningEffort: 'low' }}
        models={CODEX_MODELS} caps={CODEX_CAPS} />,
    )
    expect(effortTrigger()).toHaveTextContent('low')
    rerender(
      <PromptRow rt={fakeRuntime()} session={{ engine: 'codex', reasoningEffort: null }}
        models={CODEX_MODELS} caps={CODEX_CAPS} />,
    )
    expect(effortTrigger()).toHaveTextContent('medium')
  })

  it("selecting high calls onSetModel(currentModel, 'now', 'high')", async () => {
    const rt = fakeRuntime()
    render(<PromptRow rt={rt} session={{ engine: 'codex' }} models={CODEX_MODELS} caps={CODEX_CAPS} />)
    await userEvent.click(effortTrigger()!)
    await userEvent.click(screen.getByRole('menuitemradio', { name: /^high/ }))
    expect(rt.onSetModel).toHaveBeenCalledWith('gpt-5', 'now', 'high')
  })
})
