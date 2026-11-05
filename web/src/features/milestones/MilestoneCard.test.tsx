import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { MilestoneCard } from './MilestoneCard'
import { FRIENDLY, HOUSEHOLD } from './__fixtures__/ladder'
import type { AdvanceMutation, Milestone } from '@/api/milestones'

const { idle } = vi.hoisted(() => ({ idle: () => ({ mutate: () => {}, reset: () => {}, isPending: false, error: null }) }))
vi.mock('@/api/milestones', async (orig) => ({
  ...(await orig<typeof import('@/api/milestones')>()),
  useSetStatusNote: idle,
  useTickCondition: idle,
}))

const mount = (m: Milestone, next: Milestone | null = FRIENDLY, currentRemoved = false) =>
  render(<ol><MilestoneCard milestone={m} slug="kindash" next={next} currentRemoved={currentRemoved}
    advance={idle() as unknown as AdvanceMutation} /></ol>)

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

  it('the current card offers Advance with override… while its gate is unmet', () => {
    mount(HOUSEHOLD)
    fireEvent.click(screen.getByRole('button', { name: 'Advance with override…' }))
    expect(screen.getByRole('dialog', { name: 'Advance with override' })).toBeInTheDocument()
  })

  it('the current card offers a plain Advance once its gate is ready', () => {
    mount({ ...HOUSEHOLD, gateSummary: { met: 4, total: 4, unknown: 0, ready: true } })
    expect(screen.getByRole('button', { name: 'Advance' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /override/ })).toBeNull()
  })

  it('only the current card edits its status line and advances', () => {
    mount(HOUSEHOLD)
    expect(screen.getByRole('button', { name: 'Edit status line' })).toBeInTheDocument()
  })

  it('an upcoming card can tick its manual conditions but not advance', () => {
    mount(FRIENDLY)
    expect(screen.queryByRole('button', { name: /Advance/ })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Edit status line' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Tick: Every family completes setup with no support call' })).toBeInTheDocument()
  })

  it('a done card offers no actions', () => {
    mount({ ...FRIENDLY, state: 'done' })
    expect(screen.queryByRole('button')).toBeNull()
  })

  describe('a current milestone removed from the definition file', () => {
    const REMOVED_HINT = 'This milestone was removed from the definition file; restore it there to tick its conditions.'
    const currentWithManual: Milestone = { ...FRIENDLY, state: 'current' }
    const tickName = 'Tick: Every family completes setup with no support call'

    it('hides Tick and says how to restore it', () => {
      mount(currentWithManual, null, true)
      expect(screen.queryByRole('button', { name: tickName })).toBeNull()
      expect(screen.getByText(REMOVED_HINT)).toBeInTheDocument()
      // The rung can still be advanced off.
      expect(screen.getByRole('button', { name: /Advance/ })).toBeInTheDocument()
    })

    it('keeps Tick on the current card while it is still defined', () => {
      mount(currentWithManual, null, false)
      expect(screen.getByRole('button', { name: tickName })).toBeInTheDocument()
      expect(screen.queryByText(REMOVED_HINT)).toBeNull()
    })

    it('leaves an upcoming card alone when the current one is the removed one', () => {
      mount(FRIENDLY, null, true)
      expect(screen.getByRole('button', { name: tickName })).toBeInTheDocument()
      expect(screen.queryByText(REMOVED_HINT)).toBeNull()
    })
  })
})
