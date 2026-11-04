import { createRef } from 'react'
import { beforeEach, describe, it, expect, vi } from 'vitest'
import { act, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { PromptRow, type PromptRowMenus } from './PromptRow'
import type { SessionRuntime } from '@/session/useSessionRuntime'
import type { AgentCapabilities, ModelOption } from '@/api/types'
import { FAST_MODE_COPY, FAST_NOTE_SEEN_KEY } from './fastMode'

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
  accountRotation: false, accountSwitchNewThread: true, rewind: false, flagSettings: false,
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

  describe('fast mode (D-14)', () => {
    const CLAUDE_CAPS: AgentCapabilities = {
      ...CODEX_CAPS, reasoningEffort: false, fork: true, rewind: true, flagSettings: true,
    }
    const claudeRt = () => fakeRuntime({ model: 'opus' })
    beforeEach(() => localStorage.clear())

    it('hides ⚡ unless the session reports fast mode available', () => {
      const { rerender } = render(
        <PromptRow rt={claudeRt()} session={{ engine: 'claude_code', fastModeAvailable: false }} caps={CLAUDE_CAPS} onToggleFast={vi.fn()} />)
      expect(screen.queryByRole('button', { name: 'Fast mode' })).toBeNull()
      rerender(<PromptRow rt={claudeRt()} session={{ engine: 'claude_code' }} caps={CLAUDE_CAPS} onToggleFast={vi.fn()} />)
      expect(screen.queryByRole('button', { name: 'Fast mode' })).toBeNull()
      rerender(<PromptRow rt={claudeRt()} session={{ engine: 'claude_code', fastModeAvailable: true }} caps={CLAUDE_CAPS} onToggleFast={vi.fn()} />)
      expect(screen.getByRole('button', { name: 'Fast mode' })).toBeInTheDocument()
    })

    it('carries the cost sentence as its tooltip, and its state as aria-pressed', () => {
      render(<PromptRow rt={claudeRt()} session={{ engine: 'claude_code', fastModeAvailable: true, fastMode: true }}
        caps={CLAUDE_CAPS} onToggleFast={vi.fn()} />)
      const bolt = screen.getByRole('button', { name: 'Fast mode' })
      expect(bolt).toHaveAttribute('title', FAST_MODE_COPY)
      expect(bolt).toHaveAttribute('aria-pressed', 'true')
      expect(FAST_MODE_COPY).toBe(
        'Fast mode — faster output on the same model. Bills usage credits at a higher rate, outside your plan.')
    })

    it('toggling it hands the flipped value to onToggleFast', async () => {
      const onToggleFast = vi.fn()
      const { rerender } = render(<PromptRow rt={claudeRt()} session={{ engine: 'claude_code', fastModeAvailable: true }}
        caps={CLAUDE_CAPS} onToggleFast={onToggleFast} />)
      await userEvent.click(screen.getByRole('button', { name: 'Fast mode' }))
      expect(onToggleFast).toHaveBeenLastCalledWith(true)
      rerender(<PromptRow rt={claudeRt()} session={{ engine: 'claude_code', fastModeAvailable: true, fastMode: true }}
        caps={CLAUDE_CAPS} onToggleFast={onToggleFast} />)
      await userEvent.click(screen.getByRole('button', { name: 'Fast mode' }))
      expect(onToggleFast).toHaveBeenLastCalledWith(false)
    })

    it('shows the cost note inline the first time fast mode is on in this browser, and never again', async () => {
      const props = { rt: claudeRt(), caps: CLAUDE_CAPS, onToggleFast: vi.fn() }
      const { rerender, unmount } = render(
        <PromptRow {...props} session={{ engine: 'claude_code', fastModeAvailable: true, fastMode: false }} />)
      expect(screen.queryByRole('note')).toBeNull()
      rerender(<PromptRow {...props} session={{ engine: 'claude_code', fastModeAvailable: true, fastMode: true }} />)
      expect(screen.getByRole('note')).toHaveTextContent(FAST_MODE_COPY)
      expect(localStorage.getItem(FAST_NOTE_SEEN_KEY)).toBe('1')
      await userEvent.click(screen.getByRole('button', { name: 'Dismiss fast mode note' }))
      expect(screen.queryByRole('note')).toBeNull()
      unmount()
      // A later session, the same browser: the note has been seen.
      render(<PromptRow {...props} session={{ engine: 'claude_code', fastModeAvailable: true, fastMode: true }} />)
      expect(screen.queryByRole('note')).toBeNull()
    })
  })

  describe('live Claude effort (D-14)', () => {
    const CLAUDE_CAPS: AgentCapabilities = { ...CODEX_CAPS, reasoningEffort: false, flagSettings: true }
    const CLAUDE_MODELS: ModelOption[] = [{ id: 'opus', label: 'Opus' }, { id: 'sonnet', label: 'Sonnet' }]

    it('offers the CLI effort levels for a Claude session with live flag settings', async () => {
      render(<PromptRow rt={fakeRuntime({ model: 'opus' })} session={{ engine: 'claude_code', reasoningEffort: 'high' }}
        caps={CLAUDE_CAPS} models={CLAUDE_MODELS} />)
      await userEvent.click(screen.getByRole('button', { name: 'Effort: high' }))
      const items = within(screen.getByRole('menu', { name: 'Reasoning effort' })).getAllByRole('menuitemradio')
      expect(items.map((i) => i.textContent?.replace(/\d$/, ''))).toEqual(['low', 'medium', 'high', 'xhigh', 'max'])
    })

    it('posts the pick through rt.onSetModel on the current model, now when idle', async () => {
      const rt = fakeRuntime({ model: 'opus' })
      render(<PromptRow rt={rt} session={{ engine: 'claude_code' }} caps={CLAUDE_CAPS} models={CLAUDE_MODELS} />)
      await userEvent.click(screen.getByRole('button', { name: 'Effort: Default' }))
      await userEvent.click(screen.getByRole('menuitemradio', { name: /^max/ }))
      expect(rt.onSetModel).toHaveBeenCalledWith('opus', 'now', 'max')
    })

    it('stays hidden for Claude when live flag settings are off (or not reported)', () => {
      const { rerender } = render(<PromptRow rt={fakeRuntime({ model: 'opus' })} session={{ engine: 'claude_code' }}
        caps={{ ...CLAUDE_CAPS, flagSettings: false }} models={CLAUDE_MODELS} />)
      expect(screen.queryByRole('button', { name: /^Effort:/ })).toBeNull()
      rerender(<PromptRow rt={fakeRuntime({ model: 'opus' })} session={{ engine: 'claude_code' }} models={CLAUDE_MODELS} />)
      expect(screen.queryByRole('button', { name: /^Effort:/ })).toBeNull()
    })

    it("keeps a Codex model's own efforts — the catalog row wins over the fallback list", async () => {
      render(<PromptRow rt={fakeRuntime()} session={{ engine: 'codex' }} caps={{ ...CODEX_CAPS, flagSettings: false }} models={CODEX_MODELS} />)
      await userEvent.click(screen.getByRole('button', { name: /^Effort:/ }))
      expect(within(screen.getByRole('menu')).getAllByRole('menuitemradio')).toHaveLength(3)
    })
  })
})
