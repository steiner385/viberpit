// web/src/components/__tests__/CheckRowList.test.tsx
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import { CheckRowList } from '@/components/CheckRowList'
import type { CheckRow } from '@/api/types'

const NOW = 1_000_000

const row = (o: Partial<CheckRow>): CheckRow => ({
  name: 'lint', workflow: 'CI', status: 'COMPLETED', conclusion: 'success',
  duration_seconds: 300, required: true, overdue: false, ...o,
})

const runningRow = (over: Partial<CheckRow> = {}): CheckRow => ({
  name: 'build', workflow: 'ci', status: 'IN_PROGRESS', conclusion: 'pending',
  duration_seconds: 60, required: true, overdue: false,
  rem_low_seconds: 60, rem_nominal_seconds: 120, rem_high_seconds: 240, ...over,
})

function solidWidth(container: HTMLElement): number {
  const solid = container.querySelector('[data-band="solid"]') as HTMLElement
  return parseFloat(solid.style.width)
}

afterEach(() => vi.restoreAllMocks())

describe('CheckRowList', () => {
  it('renders a row per check with state + duration', () => {
    vi.spyOn(Date, 'now').mockReturnValue(NOW)
    render(<CheckRowList checks={[row({}), row({ name: 'test', conclusion: 'failure' })]} updatedAt={NOW} />)
    expect(screen.getByTestId('check-row-lint')).toHaveAttribute('data-state', 'done')
    expect(screen.getByTestId('check-row-test')).toHaveAttribute('data-state', 'failed')
    expect(screen.getByTestId('check-row-lint')).toHaveTextContent('5m')
  })
  it('marks overdue running checks', () => {
    vi.spyOn(Date, 'now').mockReturnValue(NOW)
    render(<CheckRowList checks={[row({ name: 'slow', conclusion: 'pending', overdue: true,
      status: 'IN_PROGRESS' })]} updatedAt={NOW} />)
    expect(screen.getByTestId('check-row-slow')).toHaveAttribute('data-state', 'overdue')
  })
  it('renders both checks when two share a name but have different workflows', () => {
    vi.spyOn(Date, 'now').mockReturnValue(NOW)
    const checks = [
      row({ name: 'lint', workflow: 'CI' }),
      row({ name: 'lint', workflow: 'Nightly' }),
    ]
    render(<CheckRowList checks={checks} updatedAt={NOW} />)
    const rows = screen.getAllByTestId(/^check-row-/)
    expect(rows).toHaveLength(2)
  })
  it('sorts rows by (workflow, name)', () => {
    vi.spyOn(Date, 'now').mockReturnValue(NOW)
    const checks = [
      row({ name: 'test', workflow: 'Zeta' }),
      row({ name: 'lint', workflow: 'Alpha' }),
      row({ name: 'build', workflow: 'Alpha' }),
    ]
    render(<CheckRowList checks={checks} updatedAt={NOW} />)
    const rows = screen.getAllByTestId(/^check-row-/)
    const texts = rows.map((r) => r.textContent ?? '')
    expect(texts[0]).toContain('build')
    expect(texts[1]).toContain('lint')
    expect(texts[2]).toContain('test')
  })
  it('renders a confidence band for a running row with a range', () => {
    vi.spyOn(Date, 'now').mockReturnValue(NOW)
    render(<CheckRowList checks={[row({
      name: 'slow', conclusion: 'pending', status: 'IN_PROGRESS', duration_seconds: 240,
      rem_low_seconds: 60, rem_nominal_seconds: 180, rem_high_seconds: 420,
    })]} updatedAt={NOW} />)
    const r = screen.getByTestId('check-row-slow')
    expect(r.querySelector('[data-band="solid"]')).toBeInTheDocument()
    expect(r.querySelector('[data-band="range"]')).toBeInTheDocument()
    expect(r.querySelector('[data-band="tick"]')).toBeInTheDocument()
  })
  it('falls back to the plain bar when a running row has no range', () => {
    vi.spyOn(Date, 'now').mockReturnValue(NOW)
    render(<CheckRowList checks={[row({
      name: 'norange', conclusion: 'pending', status: 'IN_PROGRESS', duration_seconds: 30,
      rem_low_seconds: null, rem_nominal_seconds: null, rem_high_seconds: null,
    })]} updatedAt={NOW} />)
    expect(screen.getByTestId('check-row-norange').querySelector('[data-band="range"]')).toBeNull()
  })
})

describe('CheckRowList live tick', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('a running row advances its solid width as now passes the poll time', () => {
    const updatedAt = 1_000_000
    // First render: now == updatedAt — flush the useNow setTimeout(0) catch-up
    vi.spyOn(Date, 'now').mockReturnValue(updatedAt)
    const a = render(<CheckRowList checks={[runningRow()]} updatedAt={updatedAt} />)
    act(() => { vi.advanceTimersByTime(0) })
    const w0 = solidWidth(a.container)
    a.unmount()
    // Second render: now 90s later — flush catch-up so useNow picks up the new value
    vi.spyOn(Date, 'now').mockReturnValue(updatedAt + 90_000)
    const b = render(<CheckRowList checks={[runningRow()]} updatedAt={updatedAt} />)
    act(() => { vi.advanceTimersByTime(0) })
    expect(solidWidth(b.container)).toBeGreaterThan(w0)
  })

  it('flips a row to overdue once the live delta passes rem_high_seconds', () => {
    const updatedAt = 1_000_000
    vi.spyOn(Date, 'now').mockReturnValue(updatedAt + 300_000) // 300s > rem_high 240s
    const { container } = render(<CheckRowList checks={[runningRow()]} updatedAt={updatedAt} />)
    act(() => { vi.advanceTimersByTime(0) })
    expect(container.querySelector('[data-state="overdue"]')).not.toBeNull()
    expect(screen.getByTestId('check-row-build')).toHaveTextContent('⚠')
  })

  it('does not tick a done row (now has no effect)', () => {
    const done = runningRow({ status: 'COMPLETED', conclusion: 'success' })
    vi.spyOn(Date, 'now').mockReturnValue(2_000_000)
    const { container } = render(<CheckRowList checks={[done]} updatedAt={1_000_000} />)
    act(() => { vi.advanceTimersByTime(0) })
    const solid = container.querySelector('[data-band="solid"]') as HTMLElement | null
    // done rows take the plain 100% bar, not a band → no live advance
    expect(solid).toBeNull()
  })
})
