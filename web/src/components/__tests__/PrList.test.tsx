import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import { PrList } from '../PrList'
import type { PullRequestInfo } from '@/api/types'

const pr = (number: number, over: Partial<PullRequestInfo> = {}): PullRequestInfo => ({
  number, url: `https://github.com/o/r/pull/${number}`,
  state: 'open', checks: 'passing', title: `PR ${number}`, deployed: false, ...over,
})

describe('PrList', () => {
  it('renders nothing for an empty list', () => {
    const { container } = render(<PrList prs={[]} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders a single pill for one PR (no chip)', () => {
    render(<PrList prs={[pr(42)]} />)
    expect(screen.getByRole('link', { name: /#42/ })).toBeInTheDocument()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('renders a count chip with deployed tally for 2+ PRs', () => {
    render(<PrList prs={[pr(1, { state: 'merged', deployed: true }), pr(2)]} />)
    const chip = screen.getByRole('button', { name: /2 PRs/i })
    expect(chip.textContent).toMatch(/2 PRs/)
    expect(chip.textContent).toMatch(/1/)  // one deployed
    expect(chip).toHaveAttribute('aria-label', expect.stringMatching(/2 PRs.*1 deployed/i))
  })

  it('reveals all PRs in the popover when the chip is clicked', () => {
    render(<PrList prs={[pr(1, { state: 'merged' }), pr(2)]} />)
    fireEvent.click(screen.getByRole('button', { name: /2 PRs/i }))
    expect(screen.getByRole('link', { name: /#1/ })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /#2/ })).toBeInTheDocument()
  })
})
