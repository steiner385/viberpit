import { describe, it, expect } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { MilestoneCard } from './MilestoneCard'
import { FRIENDLY, HOUSEHOLD } from './__fixtures__/ladder'
import type { Milestone } from '@/api/milestones'

const mount = (m: Milestone) => render(<ol><MilestoneCard milestone={m} /></ol>)

describe('MilestoneCard', () => {
  it('marks the current milestone as the current step with its status line', () => {
    mount(HOUSEHOLD)
    const card = screen.getByRole('listitem', { name: /Phase 0/ })
    expect(card).toHaveAttribute('aria-current', 'step')
    expect(card).toHaveTextContent('Current · the long phase by design')
  })

  it('does not mark an upcoming milestone current', () => {
    mount(FRIENDLY)
    const card = screen.getByRole('listitem', { name: /Phase 1/ })
    expect(card).not.toHaveAttribute('aria-current')
    expect(card).toHaveTextContent('Upcoming')
  })

  it('strikes through an off platform and keeps its note', () => {
    mount(FRIENDLY)
    const chip = screen.getByText('Kiosk')
    expect(chip.tagName).toBe('S')
    expect(chip.parentElement).toHaveTextContent('Kiosk (not yet)')
  })

  it('lists in-flight leaves first with their PR, done leaves as a count', () => {
    mount(HOUSEHOLD)
    const open = screen.getByRole('list', { name: 'Open leaves' })
    const items = within(open).getAllByRole('listitem')
    expect(items).toHaveLength(1)
    expect(within(items[0]).getByRole('link', { name: 'PR #20310' })).toHaveAttribute('href', expect.stringContaining('/pull/20310'))
    expect(screen.getByText('17 done')).toBeInTheDocument()
  })

  it('caps open leaves at six and links the rest to the first epic', () => {
    mount(FRIENDLY)
    const items = within(screen.getByRole('list', { name: 'Open leaves' })).getAllByRole('listitem')
    expect(items).toHaveLength(6)
    expect(items[0]).toHaveTextContent('Leaf 20450') // in flight first
    expect(screen.getByRole('link', { name: '+3 more' })).toHaveAttribute('href', FRIENDLY.epics[0].url)
  })

  it('mutes a done milestone and shows its override', () => {
    mount({
      ...HOUSEHOLD,
      state: 'done',
      exitedAt: '2026-10-20T00:00:00Z',
      exitOverride: {
        by: 'owner@example.com',
        reason: 'owner accepted the risk',
        at: '2026-10-20T00:00:00Z',
        unmet: [{ key: 'nightly', status: 'unmet', value: { n: 1 } }],
      },
    })
    const card = screen.getByRole('listitem', { name: /Phase 0/ })
    expect(card).toHaveAttribute('data-state', 'done')
    expect(card).toHaveTextContent(/^.*Done · entered .* exited .*/)
    expect(within(card).getByText('Advanced by override: owner accepted the risk')).toBeInTheDocument()
  })

  it('shows no override banner when a done milestone advanced cleanly', () => {
    mount({ ...HOUSEHOLD, state: 'done', exitedAt: '2026-10-20T00:00:00Z', exitOverride: null })
    expect(screen.queryByText(/Advanced by override/)).not.toBeInTheDocument()
  })
})
