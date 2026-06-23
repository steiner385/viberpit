// web/src/components/__tests__/PrPill.test.tsx
import { render, screen } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { PrPill } from '../PrPill'
import type { PullRequestInfo } from '@/api/types'

const base: PullRequestInfo = {
  number: 42, url: 'https://github.com/o/r/pull/42',
  state: 'open', checks: 'passing', title: 'Add thing', deployed: false,
}

describe('PrPill', () => {
  it('shows the PR number and links to the PR in a new tab', () => {
    render(<PrPill pr={base} />)
    const link = screen.getByRole('link', { name: /#42/ })
    expect(link).toHaveAttribute('href', base.url)
    expect(link).toHaveAttribute('target', '_blank')
  })

  it('does not bubble clicks to an ancestor handler', () => {
    const onParent = vi.fn()
    render(<div onClick={onParent}><PrPill pr={base} /></div>)
    screen.getByRole('link', { name: /#42/ }).click()
    expect(onParent).not.toHaveBeenCalled()
  })

  it('renders a state-specific label for each state', () => {
    const states = ['open', 'draft', 'merged', 'closed'] as const
    for (const state of states) {
      const { unmount } = render(<PrPill pr={{ ...base, state }} />)
      expect(screen.getByRole('link')).toHaveAttribute('data-state', state)
      unmount()
    }
  })

  it('reflects the checks rollup in a checks indicator', () => {
    const { rerender } = render(<PrPill pr={{ ...base, checks: 'failing' }} />)
    expect(screen.getByRole('link')).toHaveAttribute('data-checks', 'failing')
    rerender(<PrPill pr={{ ...base, checks: 'pending' }} />)
    expect(screen.getByRole('link')).toHaveAttribute('data-checks', 'pending')
  })

  it('shows a deployed affordance for a deployed merged PR', () => {
    render(<PrPill pr={{ ...base, state: 'merged', deployed: true }} />)
    const link = screen.getByRole('link')
    expect(link).toHaveAttribute('data-deployed', 'true')
    expect(link.getAttribute('aria-label')).toMatch(/deployed/i)
  })

  it('shows no deployed affordance when not deployed', () => {
    render(<PrPill pr={{ ...base, state: 'merged', deployed: false }} />)
    expect(screen.getByRole('link')).not.toHaveAttribute('data-deployed', 'true')
  })
})
