import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { PipelineBoard } from '../PipelineView'
import type { PipelinePr } from '@/api/types'

function pr(over: Partial<PipelinePr>): PipelinePr {
  return {
    number: 1, url: 'u', state: 'open', checks: 'none', title: 't',
    deployed: false, repo: 'o/n', stage: 'ready', session: null, ...over,
  }
}

describe('PipelineBoard', () => {
  it('renders sections in urgency order and hides empty stages', () => {
    render(<PipelineBoard prs={[
      pr({ number: 2, stage: 'ready' }),
      pr({ number: 1, stage: 'parked', checks: 'failing' }),
      pr({ number: 3, stage: 'merged', state: 'merged' }),
    ]} />)
    const headings = screen.getAllByRole('button').map((b) => b.textContent)
    const order = headings.join('|')
    expect(order.indexOf('Parked')).toBeLessThan(order.indexOf('Ready'))
    expect(order.indexOf('Ready')).toBeLessThan(order.indexOf('Merged'))
    expect(screen.queryByText('In CI')).toBeNull()  // no ci-stage PR → hidden
  })

  it('shows an empty state when there are no PRs', () => {
    render(<PipelineBoard prs={[]} />)
    expect(screen.getByText(/no open prs/i)).toBeInTheDocument()
  })

  it('session-linked failing PR opens its session and offers Fix; unlinked does not', () => {
    const onOpenSession = vi.fn()
    const onFixPr = vi.fn()
    render(<PipelineBoard
      prs={[
        pr({ number: 5, stage: 'parked', checks: 'failing',
             session: { id: 's1', name: 'Fix me' } }),
        pr({ number: 6, stage: 'parked', checks: 'failing', session: null }),
      ]}
      onOpenSession={onOpenSession}
      onFixPr={onFixPr}
    />)
    fireEvent.click(screen.getByTestId('pipeline-row-5'))
    expect(onOpenSession).toHaveBeenCalledWith('s1')

    fireEvent.click(screen.getByTestId('pipeline-fix-5'))
    expect(onFixPr).toHaveBeenCalledWith('s1', 5)

    // Unlinked PR #6 has no Fix button.
    expect(screen.queryByTestId('pipeline-fix-6')).toBeNull()
  })

  it('marks the far-right label as the corresponding session (icon + tooltip)', () => {
    render(<PipelineBoard prs={[
      pr({ number: 7, stage: 'ready', session: { id: 's7', name: 'My session' } }),
    ]} />)
    const label = screen.getByTestId('pipeline-session-7')
    expect(label).toHaveTextContent('My session')
    // Tooltip names it a session, so it can't be misread as the PR title.
    expect(label).toHaveAttribute('title', 'Session: My session')
    // A decorative session glyph precedes the name.
    expect(label.querySelector('svg')).not.toBeNull()
  })
})
