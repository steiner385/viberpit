// web/src/components/MergeTrainWidget.test.tsx
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MergeTrainWidget } from './MergeTrainWidget'
import type { MergeTrainState } from '../api/types'

const active: MergeTrainState = {
  repo: 'cairnea/KinDash', depth: 5, trains_per_hour: 0.8, batch_success_pct: 38,
  ejects_24h: 46, oldest_wait_seconds: 5760, failing: [10823],
  building: { pr_number: 10824, percent: 47, eta_seconds: 720 }, blocked: [10805, 10799],
  awaiting_qa: 1, awaiting_prod: 42, merges_next: { pr_number: 10824, percent: 47, eta_seconds: 720 },
}

describe('MergeTrainWidget', () => {
  it('renders health line, segments and merges-next for an active queue', () => {
    render(<MergeTrainWidget state={active} />)
    expect(screen.getByText(/depth 5/)).toBeInTheDocument()
    expect(screen.getByText(/46 ejects/)).toBeInTheDocument()
    expect(screen.getByText(/#10823/)).toBeInTheDocument()        // failing
    expect(screen.getByText(/#10805/)).toBeInTheDocument()        // blocked
    expect(screen.getByText(/Merges next/)).toBeInTheDocument()
  })

  it('renders nothing for an idle repo', () => {
    const idle: MergeTrainState = { ...active, depth: 0, ejects_24h: 0, trains_per_hour: 0,
      failing: [], blocked: [], building: null }
    const { container } = render(<MergeTrainWidget state={idle} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing when state is undefined', () => {
    const { container } = render(<MergeTrainWidget state={undefined} />)
    expect(container).toBeEmptyDOMElement()
  })
})
