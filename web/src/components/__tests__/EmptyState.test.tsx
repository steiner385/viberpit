import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { EmptyState } from '../EmptyState'

describe('EmptyState', () => {
  it('renders a "no sessions" message', () => {
    render(<EmptyState />)
    expect(screen.getByText(/no sessions/i)).toBeInTheDocument()
  })

  // The regression guard for the duplication this component's CTA became once
  // `SessionListColumn` pinned a New Session button to the foot of the same
  // column: the two sat a few pixels apart firing the same handler. The zero
  // state is copy now. Restoring a button here would silently re-create the
  // pair, so this asserts the absence rather than leaving it to a reviewer.
  it('renders no button — the foot of the list column owns the create action', () => {
    render(<EmptyState />)
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })
})
