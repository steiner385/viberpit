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

  it('renders a band on the connector after an active CI node when a range is present', () => {
    const bandModel: StageModel = { repo: 'o/r', nodes: [
      { id: 'ci', label: 'CI', kind: 'ci', order: 0, source: 'derived' },
      { id: 'merged', label: 'Merged', kind: 'merged', order: 1, source: 'derived' },
    ] }
    render(<StageTrack model={bandModel}
      states={[{ id: 'ci', state: 'active' }, { id: 'merged', state: 'pending' }]}
      etaSeconds={180} etaLowSeconds={60} etaHighSeconds={420} etaPercent={40} />)
    expect(screen.getByTestId('ci-band')).toBeInTheDocument()
  })

  it('renders one node per deploy env with env-name labels and states', () => {
    const multiDeployModel = { repo: 'o/r', nodes: [
      { id: 'ci', label: 'CI', kind: 'ci', order: 0, source: 'derived' },
      { id: 'ready', label: 'Ready', kind: 'gate', order: 1, source: 'derived' },
      { id: 'deploy:qa', label: 'qa', kind: 'deploy', order: 2, source: 'derived' },
      { id: 'deploy:staging', label: 'staging', kind: 'deploy', order: 3, source: 'derived' },
      { id: 'deploy:production', label: 'production', kind: 'deploy', order: 4, source: 'derived' },
      { id: 'merged', label: 'Merged', kind: 'merged', order: 5, source: 'derived' },
    ] } as const
    render(<StageTrack model={multiDeployModel} states={[
      { id: 'ci', state: 'done' }, { id: 'ready', state: 'done' },
      { id: 'deploy:qa', state: 'done' }, { id: 'deploy:staging', state: 'active' },
      { id: 'deploy:production', state: 'pending' }, { id: 'merged', state: 'pending' },
    ]} />)
    expect(screen.getByTestId('stage-node-deploy:qa')).toHaveAttribute('data-state', 'done')
    expect(screen.getByTestId('stage-node-deploy:staging')).toHaveAttribute('data-state', 'active')
    expect(screen.getByText('staging')).toBeInTheDocument()
    expect(screen.getByText('production')).toBeInTheDocument()
  })
})
