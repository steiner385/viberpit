import { describe, it, expect } from 'vitest'
import { useState } from 'react'
import { render, screen, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useConfirm } from './useConfirm'

function Probe({ typed }: { typed?: string }) {
  const { confirm, dialog } = useConfirm()
  const [answer, setAnswer] = useState('unasked')
  return (
    <div>
      <button onClick={() => void confirm({ title: 'Delete it?', description: 'Gone for good.', confirmLabel: 'Delete', typed }).then((ok) => setAnswer(ok ? 'yes' : 'no'))}>
        ask
      </button>
      <span data-testid="answer">{answer}</span>
      {dialog}
    </div>
  )
}

describe('useConfirm', () => {
  it('resolves true on the action button and renders nothing once answered', async () => {
    render(<Probe />)
    expect(screen.queryByRole('dialog')).toBeNull()
    await userEvent.click(screen.getByText('ask'))
    expect(screen.getByRole('dialog', { name: 'Delete it?' })).toHaveTextContent('Gone for good.')
    await userEvent.click(screen.getByRole('button', { name: 'Delete' }))
    expect(screen.getByTestId('answer')).toHaveTextContent('yes')
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('resolves false on Cancel and on Escape', async () => {
    render(<Probe />)
    await userEvent.click(screen.getByText('ask'))
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.getByTestId('answer')).toHaveTextContent('no')
    await userEvent.click(screen.getByText('ask'))
    await act(async () => { await userEvent.keyboard('{Escape}') })
    expect(screen.getByTestId('answer')).toHaveTextContent('no')
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('passes the Typed tier through: the action waits for the exact text', async () => {
    render(<Probe typed="rocky" />)
    await userEvent.click(screen.getByText('ask'))
    const btn = screen.getByRole('button', { name: 'Delete' })
    expect(btn).toBeDisabled()
    await userEvent.type(screen.getByLabelText(/Type rocky to confirm/), 'rocky')
    expect(btn).toBeEnabled()
    await userEvent.click(btn)
    expect(screen.getByTestId('answer')).toHaveTextContent('yes')
  })
})
