import { createRef } from 'react'
import { describe, it, expect, vi } from 'vitest'
import { act, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { PromptRow, type PromptRowMenus } from './PromptRow'
import type { SessionRuntime } from '@/session/useSessionRuntime'
import type { AgentCapabilities } from '@/api/types'

function fakeRuntime(over: Partial<SessionRuntime> = {}): SessionRuntime {
  return { sessionId: 's1', mode: 'auto', pendingMode: null, onSetMode: vi.fn(), ...over } as unknown as SessionRuntime
}

const CODEX_CAPS: AgentCapabilities = {
  models: true, reasoningEffort: true, steer: true, interrupt: true, approvals: true, questions: true,
  fork: false, adopt: false, selfModelSwitch: false, subagents: false, attachments: true,
  accountRotation: false, accountSwitchNewThread: true,
}

describe('PromptRow', () => {
  it('renders the mode picker showing the runtime mode', () => {
    render(<PromptRow rt={fakeRuntime({ mode: 'plan' })} />)
    expect(screen.getByRole('button', { name: /mode/i })).toHaveTextContent('Plan')
  })

  it('routes a selection to rt.onSetMode', async () => {
    const rt = fakeRuntime()
    render(<PromptRow rt={rt} />)
    await userEvent.click(screen.getByRole('button', { name: /mode/i }))
    await userEvent.click(screen.getByRole('menuitemradio', { name: /^Full auto/ }))
    expect(rt.onSetMode).toHaveBeenCalledWith('full-auto')
  })

  it('surfaces the runtime pending mode', () => {
    render(<PromptRow rt={fakeRuntime({ pendingMode: 'plan' })} />)
    expect(screen.getByRole('status')).toHaveTextContent('Mode change pending')
  })

  it('renders nothing in the assistant drawer (hideSessionControls)', () => {
    const { container } = render(<PromptRow rt={fakeRuntime()} hideSessionControls />)
    expect(container).toBeEmptyDOMElement()
  })

  it('lists all seven modes for a Codex session: no engine capability restricts modes', async () => {
    // AgentCapabilities has no permission-mode field and the Codex driver maps
    // every ViberPit mode (server/codex/permissions.py), so nothing is filtered.
    render(<PromptRow rt={fakeRuntime()} session={{ engine: 'codex' }} caps={CODEX_CAPS} />)
    await userEvent.click(screen.getByRole('button', { name: /mode/i }))
    expect(within(screen.getByRole('menu')).getAllByRole('menuitemradio')).toHaveLength(7)
  })

  it('exposes openMode() on menuRef', () => {
    const menuRef = createRef<PromptRowMenus>()
    render(<PromptRow rt={fakeRuntime()} menuRef={menuRef} />)
    expect(screen.queryByRole('menu')).toBeNull()
    act(() => menuRef.current?.openMode())
    expect(screen.getByRole('menu', { name: 'Permission mode' })).toBeInTheDocument()
    // Model / effort pickers land in a later task; until then they are safe no-ops.
    expect(() => { menuRef.current?.openModel(); menuRef.current?.openEffort() }).not.toThrow()
  })
})
