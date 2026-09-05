import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { NoMatchState } from '../NoMatchState'

describe('NoMatchState', () => {
  it('announces the reason as a status and offers the caller-named reset', async () => {
    const onClear = vi.fn()
    render(
      <NoMatchState
        message="No archived sessions match your search."
        actionLabel="Clear"
        onClear={onClear}
      />,
    )
    expect(screen.getByRole('status')).toHaveTextContent('No archived sessions match your search.')
    await userEvent.click(screen.getByRole('button', { name: 'Clear' }))
    expect(onClear).toHaveBeenCalledOnce()
  })
})
