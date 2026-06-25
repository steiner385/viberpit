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

  it('suppresses the stale checks label for a terminal PR with frozen failing checks', () => {
    // A closed PR keeps `checks: 'failing'` frozen by GitHub, but there's no
    // action to take — don't surface the red X / "checks failing" on it.
    const { rerender } = render(<PrPill pr={{ ...base, state: 'closed', checks: 'failing' }} />)
    let link = screen.getByRole('link')
    expect(link).toHaveAttribute('data-checks', 'failing')  // data stays factual
    expect(link.getAttribute('aria-label')).not.toMatch(/checks/i)

    rerender(<PrPill pr={{ ...base, state: 'merged', checks: 'failing' }} />)
    link = screen.getByRole('link')
    expect(link.getAttribute('aria-label')).not.toMatch(/checks/i)
  })

  it('still surfaces failing checks on an actionable (open) PR', () => {
    render(<PrPill pr={{ ...base, state: 'open', checks: 'failing' }} />)
    expect(screen.getByRole('link').getAttribute('aria-label')).toMatch(/checks failing/i)
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

  describe('stage badge', () => {
    it('shows stage badge text and data-stage for ci, ready, parked', () => {
      for (const stage of ['ci', 'ready', 'parked'] as const) {
        const { unmount } = render(<PrPill pr={{ ...base, stage }} />)
        const link = screen.getByRole('link')
        expect(link).toHaveAttribute('data-stage', stage)
        expect(link.textContent).toContain(stage)
        expect(link.getAttribute('aria-label')).toMatch(new RegExp(stage))
        unmount()
      }
    })

    it('does not render a stage badge for merged stage (state color conveys it)', () => {
      render(<PrPill pr={{ ...base, state: 'merged', stage: 'merged' }} />)
      const link = screen.getByRole('link')
      expect(link).not.toHaveAttribute('data-stage')
      // text content should not contain the word "merged" as a badge (only in aria/title via state)
      // but the link text itself (#42 + icon) should not include "merged" as badge text
      const textNodes = Array.from(link.querySelectorAll('span')).map((s) => s.textContent)
      expect(textNodes).not.toContain('merged')
    })

    it('does not render a stage badge for empty stage', () => {
      render(<PrPill pr={{ ...base, stage: '' }} />)
      expect(screen.getByRole('link')).not.toHaveAttribute('data-stage')
    })

    it('does not render a stage badge when stage is absent', () => {
      render(<PrPill pr={{ ...base }} />)
      expect(screen.getByRole('link')).not.toHaveAttribute('data-stage')
    })
  })
})
