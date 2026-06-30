// web/src/components/__tests__/CheckDrillDown.test.tsx
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { CheckDrillDown } from '@/components/CheckDrillDown'
import type { CheckGroup } from '@/api/types'

const UPDATED_AT = 1_000_000

const g = (kind: 'head' | 'merge', cost: number | null, eta?: number | null): CheckGroup => ({
  kind, checks: [{ name: `${kind}-lint`, workflow: 'CI', status: 'COMPLETED',
    conclusion: 'success', duration_seconds: 1920, required: true, overdue: false }],
  total_seconds: 1920, cost_usd: cost, eta_seconds: eta ?? null,
})

describe('CheckDrillDown', () => {
  it('renders merge group before head group with labels + cost', () => {
    render(<CheckDrillDown groups={[g('head', 0.25), g('merge', 0.25)]} updatedAt={UPDATED_AT} />)
    const headers = screen.getAllByRole('heading')
    expect(headers[0]).toHaveTextContent('MERGE GROUP BUILD')
    expect(headers[1]).toHaveTextContent('PR CHECKS (HEAD COMMIT)')
    expect(screen.getByTestId('ci-cost-head')).toHaveTextContent('(~$0.25)')
  })
  it('omits cost line when cost is null', () => {
    render(<CheckDrillDown groups={[g('head', null)]} updatedAt={UPDATED_AT} />)
    expect(screen.queryByTestId('ci-cost-head')).toBeNull()
  })
  it('renders merge-queue ETA when eta_seconds is set', () => {
    render(<CheckDrillDown groups={[g('merge', null, 300)]} updatedAt={UPDATED_AT} />)
    const el = screen.getByTestId('merge-eta-merge')
    expect(el).toHaveTextContent('5m')
  })
  it('omits ETA line when eta_seconds is null', () => {
    render(<CheckDrillDown groups={[g('merge', null, null)]} updatedAt={UPDATED_AT} />)
    expect(screen.queryByTestId('merge-eta-merge')).toBeNull()
  })
})
