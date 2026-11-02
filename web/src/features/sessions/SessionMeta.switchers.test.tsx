// web/src/features/sessions/SessionMeta.switchers.test.tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { SessionMeta } from './SessionMeta'

const base = {
  sessionId: 's1',
  account: 'acct-a', quota: null,
  subAgents: [],
  accounts: [{ name: 'acct-a' }, { name: 'acct-b' }],
  accountPinned: false,
}

describe('SessionMeta switchers', () => {
  it('dispatches an account selection', () => {
    const onSelectAccount = vi.fn()
    render(<SessionMeta {...base} onSelectAccount={onSelectAccount} />)
    fireEvent.change(screen.getByLabelText('Switch account'), { target: { value: 'acct-b' } })
    expect(onSelectAccount).toHaveBeenCalledWith('acct-b')
  })

  it('renders the pin as an Unpin button and dispatches onUnpin', () => {
    const onUnpin = vi.fn()
    render(<SessionMeta {...base} accountPinned onSelectAccount={vi.fn()} onUnpin={onUnpin} />)
    fireEvent.click(screen.getByRole('button', { name: 'Unpin account' }))
    expect(onUnpin).toHaveBeenCalledTimes(1)
  })

  it('renders no pin control when not pinned', () => {
    render(<SessionMeta {...base} onSelectAccount={vi.fn()} />)
    expect(screen.queryByRole('button', { name: 'Unpin account' })).not.toBeInTheDocument()
  })
})
