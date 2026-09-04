// web/src/components/session/SessionMeta.test.tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { SessionMeta } from './SessionMeta'

// `mode` and `onSetMode` are required (the plan-mode toggle reads them); the
// fixture omitted both, so the toggle rendered against `undefined`. 'default' is
// the same case it was already exercising — `mode === 'plan'` stays false.
const base = {
  account: 'acct-a', model: 'opus', mode: 'default', contextPct: null, quota: null,
  subAgents: [],
  models: [{ id: 'opus', label: 'Opus' }, { id: 'sonnet', label: 'Sonnet' }],
  accounts: [{ name: 'acct-a' }, { name: 'acct-b' }],
  accountPinned: false,
  onSetMode: vi.fn(),
}

describe('SessionMeta switchers', () => {
  it('dispatches a model selection', () => {
    const onSelectModel = vi.fn()
    render(<SessionMeta {...base} onSelectModel={onSelectModel} onSelectAccount={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('Select model'), { target: { value: 'sonnet' } })
    expect(onSelectModel).toHaveBeenCalledWith('sonnet')
  })

  it('does not dispatch a model selection when the value equals the current model', () => {
    const onSelectModel = vi.fn()
    render(<SessionMeta {...base} onSelectModel={onSelectModel} onSelectAccount={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('Select model'), { target: { value: 'opus' } })
    expect(onSelectModel).not.toHaveBeenCalled()
  })

  it('dispatches an account selection', () => {
    const onSelectAccount = vi.fn()
    render(<SessionMeta {...base} onSelectModel={vi.fn()} onSelectAccount={onSelectAccount} />)
    fireEvent.change(screen.getByLabelText('Switch account'), { target: { value: 'acct-b' } })
    expect(onSelectAccount).toHaveBeenCalledWith('acct-b')
  })

  it('shows a pin indicator when pinned', () => {
    render(<SessionMeta {...base} accountPinned onSelectModel={vi.fn()} onSelectAccount={vi.fn()} />)
    expect(screen.getByLabelText('Account pinned')).toBeInTheDocument()
  })
})
