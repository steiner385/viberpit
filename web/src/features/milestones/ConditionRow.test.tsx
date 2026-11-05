// One gate condition: status as glyph + text (never colour alone), the value
// against its target, its age in the visible text, and the error in a tooltip
// and `aria-describedby`.
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ConditionRow } from './ConditionRow'
import { HOUSEHOLD, ago, cond } from './__fixtures__/ladder'

const [rubric, laneFixes, nightly, consent] = HOUSEHOLD.gate

const mount = (c = rubric) => render(<ul><ConditionRow condition={c} /></ul>)

describe('ConditionRow', () => {
  it('renders a stale row as ? Unknown with its age, the error described', () => {
    mount(nightly)
    const status = screen.getByText('Unknown')
    expect(status.closest('[data-status]')).toHaveTextContent('? Unknown')
    expect(screen.getByRole('listitem')).toHaveTextContent('2d ago')
    const described = status.closest('[aria-describedby]')!
    const note = document.getElementById(described.getAttribute('aria-describedby')!)
    expect(note).toHaveTextContent('GitHub returned 502 for nightly.yml')
    expect(described).toHaveAttribute('title', expect.stringContaining('GitHub returned 502'))
  })

  it('renders ✔ Met for a met condition', () => {
    mount(consent)
    expect(screen.getByText('Met').closest('[data-status]')).toHaveTextContent('✔ Met')
    expect(screen.getByText('3 of 3 closed')).toBeInTheDocument()
  })

  it('renders ✖ Not met and the value against its target', () => {
    mount(rubric)
    expect(screen.getByText('Not met').closest('[data-status]')).toHaveTextContent('✖ Not met')
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
    expect(screen.getByRole('listitem')).toHaveTextContent('Ticked by tony · call notes')
  })

  it('says never measured when there is no reading', () => {
    mount(cond({ key: 'x' }))
    expect(screen.getByRole('listitem')).toHaveTextContent('Never measured')
  })
})
