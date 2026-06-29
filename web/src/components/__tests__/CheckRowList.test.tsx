// web/src/components/__tests__/CheckRowList.test.tsx
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { CheckRowList } from '@/components/CheckRowList'
import type { CheckRow } from '@/api/types'

const row = (o: Partial<CheckRow>): CheckRow => ({
  name: 'lint', workflow: 'CI', status: 'COMPLETED', conclusion: 'success',
  duration_seconds: 300, required: true, overdue: false, ...o,
})

describe('CheckRowList', () => {
  it('renders a row per check with state + duration', () => {
    render(<CheckRowList checks={[row({}), row({ name: 'test', conclusion: 'failure' })]} />)
    expect(screen.getByTestId('check-row-lint')).toHaveAttribute('data-state', 'done')
    expect(screen.getByTestId('check-row-test')).toHaveAttribute('data-state', 'failed')
    expect(screen.getByTestId('check-row-lint')).toHaveTextContent('5m')
  })
  it('marks overdue running checks', () => {
    render(<CheckRowList checks={[row({ name: 'slow', conclusion: 'pending', overdue: true,
      status: 'IN_PROGRESS' })]} />)
    expect(screen.getByTestId('check-row-slow')).toHaveAttribute('data-state', 'overdue')
  })
})
