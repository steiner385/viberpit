// One gate condition: line 1 is the status glyph and the full-width label; line 2
// leads with the status text, then the value against its target and its age
// (status is glyph + text, never colour alone). A failed reading's error rides a
// tooltip and `aria-describedby`.
import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { ConditionRow } from './ConditionRow'
import { HOUSEHOLD, ago, cond } from './__fixtures__/ladder'

vi.mock('@/api/milestones', async (orig) => ({
  ...(await orig<typeof import('@/api/milestones')>()),
  useTickCondition: () => ({ mutate: vi.fn(), reset: vi.fn(), isPending: false, error: null }),
}))

const [rubric, laneFixes, nightly, consent] = HOUSEHOLD.gate

const mount = (c = rubric) => render(<ul><ConditionRow condition={c} /></ul>)

/** The row's two lines: [glyph, label] and the meta line. */
function lines() {
  const li = screen.getByRole('listitem')
  const [head, meta] = Array.from(li.children) as HTMLElement[]
  return { li, head, meta }
}

describe('ConditionRow', () => {
  it('renders a stale row as ? + label, then Unknown with its age, the error described', () => {
    mount(nightly)
    const { li, head, meta } = lines()
    expect(li).toHaveAttribute('data-status', 'unknown')
    expect(head).toHaveTextContent(`?${nightly.label}`)
    expect(head.firstElementChild).toHaveTextContent('?')
    expect(head.firstElementChild).toHaveAttribute('aria-hidden', 'true')
    expect(meta.firstElementChild).toHaveTextContent(/^Unknown$/)
    expect(meta).toHaveTextContent('2d ago')
    const status = screen.getByText('Unknown')
    const described = status.closest('[aria-describedby]')!
    const note = document.getElementById(described.getAttribute('aria-describedby')!)
    expect(note).toHaveTextContent('GitHub returned 502 for nightly.yml')
    expect(described).toHaveAttribute('title', expect.stringContaining('GitHub returned 502'))
  })

  it('renders ✔ Met for a met condition', () => {
    mount(consent)
    const { head, meta } = lines()
    expect(head.firstElementChild).toHaveTextContent('✔')
    expect(meta.firstElementChild).toHaveTextContent(/^Met$/)
    expect(screen.getByText('3 of 3 closed')).toBeInTheDocument()
  })

  it('renders ✖ Not met and the value against its target', () => {
    mount(rubric)
    const { head, meta } = lines()
    expect(head.firstElementChild).toHaveTextContent('✖')
    expect(meta).toHaveTextContent(/^Not met293 → 0/)
    expect(screen.getByText('293 → 0')).toBeInTheDocument()
    expect(screen.getByRole('listitem')).toHaveTextContent('3h ago')
    expect(screen.getByRole('link', { name: /source/i })).toHaveAttribute('href', rubric.sourceUrl)
  })

  it('reads a non-eq target as "n of ≥target" and closed refs as "8 of 9 closed"', () => {
    const { unmount } = mount(cond({ key: 's', status: 'unmet', value: { n: 2 }, op: 'ge', target: 3, measuredAt: ago(60) }))
    expect(screen.getByText('2 of ≥3')).toBeInTheDocument()
    unmount()
    mount(laneFixes)
    expect(screen.getByText('8 of 9 closed')).toBeInTheDocument()
  })

  it('shows who ticked a manual condition and the evidence', () => {
    mount(cond({ key: 'm', status: 'met', value: { met: true }, manualBy: 'tony', manualEvidence: 'call notes', measuredAt: ago(60) }))
    expect(screen.getByRole('listitem')).toHaveTextContent('by tony · call notes')
  })

  it('a manual condition offers Tick, then Update once ticked; either opens the tick dialog', () => {
    const manual = cond({ key: 'setup', label: 'Every family completes setup' })
    const { unmount } = render(<ul><ConditionRow condition={manual} manage={{ slug: 'kindash', scope: 'friendly' }} /></ul>)
    expect(screen.queryByRole('dialog')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Tick: Every family completes setup' }))
    expect(screen.getByRole('dialog', { name: 'Tick condition' })).toBeInTheDocument()
    unmount()
    render(<ul><ConditionRow condition={{ ...manual, manualBy: 'tony', value: { met: true } }} manage={{ slug: 'kindash', scope: 'friendly' }} /></ul>)
    expect(screen.getByRole('button', { name: 'Update: Every family completes setup' })).toBeInTheDocument()
  })

  it('offers no Tick for a measured condition, nor without manage', () => {
    render(<ul><ConditionRow condition={rubric} manage={{ slug: 'kindash', scope: 'household' }} /></ul>)
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('says never measured when there is no reading', () => {
    mount(cond({ key: 'x' }))
    expect(screen.getByRole('listitem')).toHaveTextContent('Never measured')
  })
})
