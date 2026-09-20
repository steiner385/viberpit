import { render, screen } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import { SessionGoalText } from './SessionGoalText'

describe('SessionGoalText', () => {
  it('renders the reckoned goal in smaller subdued text with the source in the tooltip', () => {
    render(<SessionGoalText goal="ship the PR" source="explicit" title="Fix login bug" />)
    const el = screen.getByTestId('session-goal')
    expect(el).toHaveTextContent('ship the PR')
    expect(el).toHaveAttribute('title', expect.stringMatching(/goal/i))
    expect(el.className).toMatch(/text-xs/)
  })

  it('renders nothing when the goal is just the title fallback (no added information)', () => {
    render(<SessionGoalText goal="Fix login bug" source="initial_prompt" title="Fix login bug" />)
    expect(screen.queryByTestId('session-goal')).not.toBeInTheDocument()
  })

  it('renders nothing when there is no goal', () => {
    render(<SessionGoalText goal={undefined} source={undefined} title="Fix login bug" />)
    expect(screen.queryByTestId('session-goal')).not.toBeInTheDocument()
  })

  // Issue #1400: a long goal sits beside the (also truncating) title in
  // SessionHeader; without a cap the two shrink roughly proportionally on a
  // narrow phone, still starving the title. The cap gives the title first
  // claim regardless of how long the goal text is.
  it('caps its width below `narrow:` so a long goal cannot starve the title beside it', () => {
    render(<SessionGoalText goal="a very long reckoned goal that would otherwise compete for space" source="explicit" title="Fix login bug" />)
    expect(screen.getByTestId('session-goal').className).toMatch(/narrow:max-w-\[5rem\]/)
  })
})
