import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { StageTrack } from '../StageTrack'
import type { StageModel } from '@/api/types'

const model: StageModel = {
  repo: 'o/r',
  nodes: [
    { id: 'ci', label: 'CI', kind: 'ci', order: 0, source: 'derived' },
    { id: 'ready', label: 'Ready', kind: 'gate', order: 1, source: 'derived' },
    { id: 'merged', label: 'Merged', kind: 'merged', order: 2, source: 'derived' },
  ],
}

describe('StageTrack', () => {
  it('renders one node per model node with its state', () => {
    render(
      <StageTrack
        model={model}
        states={[
          { id: 'ci', state: 'done' },
          { id: 'ready', state: 'active' },
          { id: 'merged', state: 'pending' },
        ]}
      />,
    )
    expect(screen.getByTestId('stage-node-ci')).toHaveAttribute('data-state', 'done')
    expect(screen.getByTestId('stage-node-ready')).toHaveAttribute('data-state', 'active')
    expect(screen.getByTestId('stage-node-merged')).toHaveAttribute('data-state', 'pending')
    expect(screen.getByText('CI')).toBeInTheDocument()
  })

  it('falls back to a skeleton when model is undefined', () => {
    render(<StageTrack model={undefined} states={undefined} />)
    expect(screen.getByTestId('stage-node-ci')).toBeInTheDocument()
    expect(screen.getByTestId('stage-node-merged')).toBeInTheDocument()
  })

  it('shows ETA on an active CI node', () => {
    render(
      <StageTrack
        model={model}
        etaSeconds={75}
        states={[{ id: 'ci', state: 'active' }]}
      />,
    )
    expect(screen.getByText(/~1m|~75s/)).toBeInTheDocument()
  })
})
