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

  // Issue #1400 and the 2026-09-29 critique (P2): a long goal sits beside the
  // (also truncating) title in SessionHeader, and flex shrinks in proportion to
  // content width — so the LONGER text kept more of the row (title 124px, goal
  // ~280px on desktop; "I…" on a phone). The title has first claim: this span
  // shrinks first, is capped, and is hidden outright below `narrow:`.
  it('shrinks before the title and is capped, so a long goal cannot starve the title beside it', () => {
    render(<SessionGoalText goal="a very long reckoned goal that would otherwise compete for space" source="explicit" title="Fix login bug" />)
    const cls = screen.getByTestId('session-goal').className
    expect(cls).toMatch(/shrink-\[4\]/)
    expect(cls).toMatch(/max-w-\[40%\]/)
  })

  it('is hidden below `narrow:` — the phone header has no room for a second line of text', () => {
    render(<SessionGoalText goal="ship the PR" source="explicit" title="Fix login bug" />)
    expect(screen.getByTestId('session-goal').className).toMatch(/narrow:hidden/)
  })
})
