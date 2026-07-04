import { render, screen } from '@testing-library/react'
import { CheckRowList } from '../CheckRowList'
import type { CheckRow } from '@/api/types'

function row(p: Partial<CheckRow> & { name: string }): CheckRow {
  return {
    name: p.name, workflow: p.workflow ?? 'w', status: p.status ?? 'COMPLETED',
    conclusion: p.conclusion ?? 'success', duration_seconds: p.duration_seconds ?? null,
    required: true, overdue: p.overdue ?? false,
    rem_low_seconds: p.rem_low_seconds ?? null, rem_nominal_seconds: p.rem_nominal_seconds ?? null,
    rem_high_seconds: p.rem_high_seconds ?? null,
  }
}
const solidWidth = (testid: string) => {
  const el = screen.getByTestId(testid).querySelector('[data-band="solid"]') as HTMLElement
  return parseFloat(el.style.width)
}

it('sets data-state and label per conclusion', () => {
  render(<CheckRowList updatedAt={0} checks={[
    row({ name: 'lint', conclusion: 'success', duration_seconds: 300 }),
    row({ name: 'test', conclusion: 'failure', duration_seconds: 120 }),
    row({ name: 'skip', conclusion: 'skipped', duration_seconds: null }),
  ]} />)
  expect(screen.getByTestId('check-row-lint')).toHaveAttribute('data-state', 'done')
  expect(screen.getByTestId('check-row-lint')).toHaveTextContent('5m ✓')
  expect(screen.getByTestId('check-row-test')).toHaveAttribute('data-state', 'failed')
  expect(screen.getByTestId('check-row-skip')).toHaveTextContent('–')
})

it('shares one scale so a 30s bar is ~0.1x a 300s bar', () => {
  render(<CheckRowList updatedAt={0} checks={[
    row({ name: 'short', conclusion: 'success', duration_seconds: 30 }),
    row({ name: 'long', conclusion: 'success', duration_seconds: 300 }),
  ]} />)
  expect(solidWidth('check-row-long')).toBe(100)
  expect(solidWidth('check-row-short')).toBeCloseTo(10, 0)
})

it('renders queued rows as a 15% stub', () => {
  render(<CheckRowList updatedAt={0} checks={[
    row({ name: 'q', status: 'QUEUED', conclusion: 'pending', duration_seconds: null }),
    row({ name: 'done', conclusion: 'success', duration_seconds: 300 }),
  ]} />)
  expect(screen.getByTestId('check-row-q')).toHaveAttribute('data-state', 'queued')
  expect(solidWidth('check-row-q')).toBe(15)
})

it('a running row shows band + tick and an elapsed/expected label', () => {
  render(<CheckRowList updatedAt={0} checks={[
    row({ name: 'run', status: 'IN_PROGRESS', conclusion: 'pending', duration_seconds: 60,
      rem_low_seconds: 30, rem_nominal_seconds: 60, rem_high_seconds: 120 }),
  ]} />)
  const r = screen.getByTestId('check-row-run')
  expect(r.querySelector('[data-band="range"]')).toBeInTheDocument()
  expect(r.querySelector('[data-band="tick"]')).toBeInTheDocument()
  expect(r).toHaveTextContent('/ ~2m')
})
