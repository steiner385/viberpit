import { createRef } from 'react'
import { describe, it, expect, vi } from 'vitest'
import { act, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { PromptRow, type PromptRowMenus } from './PromptRow'
import type { SessionRuntime } from '@/session/useSessionRuntime'
import type { AgentCapabilities, ModelOption } from '@/api/types'

function fakeRuntime(over: Partial<SessionRuntime> = {}): SessionRuntime {
  return {
    sessionId: 's1', mode: 'auto', pendingMode: null, onSetMode: vi.fn(),
    model: 'gpt-5', liveModel: null, modelPending: false, onSetModel: vi.fn(),
    state: { status: { state: 'idle', count: 0 } },
    ...over,
  } as unknown as SessionRuntime
}

const CODEX_CAPS: AgentCapabilities = {
  models: true, reasoningEffort: true, steer: true, interrupt: true, approvals: true, questions: true,
  fork: false, adopt: false, selfModelSwitch: false, subagents: false, attachments: true,
  accountRotation: false, accountSwitchNewThread: true,
}

const CODEX_MODELS: ModelOption[] = [
  { id: 'gpt-5', label: 'GPT-5', reasoningEfforts: ['low', 'medium', 'high'], defaultReasoningEffort: 'medium' },
  { id: 'gpt-5-mini', label: 'GPT-5 mini' },
]

describe('PromptRow', () => {
  it('renders the mode picker showing the runtime mode', () => {
    render(<PromptRow rt={fakeRuntime({ mode: 'plan' })} />)
    expect(screen.getByRole('button', { name: /^Mode:/ })).toHaveTextContent('Plan')
  })

  it('routes a selection to rt.onSetMode', async () => {
    const rt = fakeRuntime()
    render(<PromptRow rt={rt} />)
    await userEvent.click(screen.getByRole('button', { name: /^Mode:/ }))
    await userEvent.click(screen.getByRole('menuitemradio', { name: /^Full auto/ }))
    expect(rt.onSetMode).toHaveBeenCalledWith('full-auto')
  })

  it('surfaces the runtime pending mode', () => {
    render(<PromptRow rt={fakeRuntime({ pendingMode: 'plan' })} />)
    expect(screen.getByRole('status')).toHaveTextContent('Mode change pending')
  })

  it('lists all seven modes for a Codex session: no engine capability restricts modes', async () => {
    // AgentCapabilities has no permission-mode field and the Codex driver maps
    // every ViberPit mode (server/codex/permissions.py), so nothing is filtered.
    render(<PromptRow rt={fakeRuntime()} session={{ engine: 'codex' }} caps={CODEX_CAPS} />)
    await userEvent.click(screen.getByRole('button', { name: /^Mode:/ }))
    expect(within(screen.getByRole('menu')).getAllByRole('menuitemradio')).toHaveLength(7)
  })

  it('exposes openMode() / openModel() / openEffort() on menuRef', () => {
    const menuRef = createRef<PromptRowMenus>()
    render(<PromptRow rt={fakeRuntime()} session={{ engine: 'codex' }} caps={CODEX_CAPS} models={CODEX_MODELS} menuRef={menuRef} />)
    expect(screen.queryByRole('menu')).toBeNull()
    act(() => menuRef.current?.openMode())
    expect(screen.getByRole('menu', { name: 'Permission mode' })).toBeInTheDocument()
    act(() => menuRef.current?.openModel())
    expect(screen.getByRole('menu', { name: 'Model' })).toBeInTheDocument()
    expect(screen.queryByRole('menu', { name: 'Permission mode' })).toBeNull()
    act(() => menuRef.current?.openEffort())
    expect(screen.getByRole('menu', { name: 'Reasoning effort' })).toBeInTheDocument()
  })

  it('openEffort() is a no-op when the effort picker is hidden', () => {
    const menuRef = createRef<PromptRowMenus>()
    render(<PromptRow rt={fakeRuntime({ model: 'opus' })} menuRef={menuRef} />)
    act(() => menuRef.current?.openEffort())
    expect(screen.queryByRole('menu')).toBeNull()
  })

  it('renders the model picker on the chosen alias', () => {
    render(<PromptRow rt={fakeRuntime()} session={{ engine: 'codex' }} caps={CODEX_CAPS} models={CODEX_MODELS} />)
    expect(screen.getByRole('button', { name: /^Model:/ })).toHaveTextContent('GPT-5')
  })

  it('a model pick on a busy session asks first; the choice reaches rt.onSetModel', async () => {
    const rt = fakeRuntime({ state: { status: { state: 'working', count: 0 } } } as unknown as Partial<SessionRuntime>)
    render(<PromptRow rt={rt} session={{ engine: 'codex' }} caps={CODEX_CAPS} models={CODEX_MODELS} />)
    await userEvent.click(screen.getByRole('button', { name: /^Model:/ }))
    await userEvent.click(screen.getByRole('menuitemradio', { name: /^GPT-5 mini/ }))
    // Codex can steer: the non-interrupting choice says so.
    await userEvent.click(screen.getByRole('button', { name: 'Switch without stopping' }))
    expect(rt.onSetModel).toHaveBeenCalledWith('gpt-5-mini', 'after_turn')
  })

  describe('usage ring', () => {
    it('labels the share of the context window used', () => {
      render(<PromptRow rt={fakeRuntime({ contextPct: 20 })} />)
      expect(screen.getByRole('img', { name: 'Context 80% used' })).toBeInTheDocument()
    })

    it('warns that auto-compaction is near at the danger tier', () => {
      render(<PromptRow rt={fakeRuntime({ contextPct: 5 })} />)
      expect(screen.getByRole('img', { name: /^Context 95% used/ }))
        .toHaveAccessibleName('Context 95% used · auto-compacts soon')
    })

    it('does not warn below the danger tier', () => {
      render(<PromptRow rt={fakeRuntime({ contextPct: 20 })} />)
      expect(screen.getByRole('img', { name: /^Context/ }).getAttribute('aria-label')).not.toMatch(/auto-compacts/)
    })

    it('is absent while no context reading exists', () => {
      render(<PromptRow rt={fakeRuntime({ contextPct: null })} />)
      expect(screen.queryByRole('img', { name: /^Context/ })).toBeNull()
    })
  })
})
