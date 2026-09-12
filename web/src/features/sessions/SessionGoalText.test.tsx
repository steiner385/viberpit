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
})
