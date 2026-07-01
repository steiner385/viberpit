import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
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

const CI_MODEL = { nodes: [{ id: 'ci', label: 'CI', kind: 'ci' }, { id: 'ready', label: 'Ready' }] }
const CI_ACTIVE = [{ id: 'ci', state: 'active' as const }, { id: 'ready', state: 'pending' as const }]

function solidWidth(container: HTMLElement): number {
  const band = container.querySelector('[data-testid="ci-band"]')
  const solid = band?.querySelector('span') as HTMLElement
  return parseFloat(solid.style.width) // "NN%" → NN
}

afterEach(() => vi.restoreAllMocks())

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
        updatedAt={Date.now()}
      />,
    )
    expect(screen.getByTestId('stage-node-ci')).toHaveAttribute('data-state', 'done')
    expect(screen.getByTestId('stage-node-ready')).toHaveAttribute('data-state', 'active')
    expect(screen.getByTestId('stage-node-merged')).toHaveAttribute('data-state', 'pending')
    expect(screen.getByText('CI')).toBeInTheDocument()
  })

  it('falls back to a skeleton when model is undefined', () => {
    render(<StageTrack model={undefined} states={undefined} updatedAt={Date.now()} />)
    expect(screen.getByTestId('stage-node-ci')).toBeInTheDocument()
    expect(screen.getByTestId('stage-node-merged')).toBeInTheDocument()
  })

  it('shows ETA on an active CI node', () => {
    render(
      <StageTrack
        model={model}
        etaSeconds={75}
        states={[{ id: 'ci', state: 'active' }]}
        updatedAt={Date.now()}
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
      etaSeconds={180} etaLowSeconds={60} etaHighSeconds={420} etaPercent={40}
      updatedAt={Date.now()} />)
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
    ]} updatedAt={Date.now()} />)
    expect(screen.getByTestId('stage-node-deploy:qa')).toHaveAttribute('data-state', 'done')
    expect(screen.getByTestId('stage-node-deploy:staging')).toHaveAttribute('data-state', 'active')
    expect(screen.getByText('staging')).toBeInTheDocument()
    expect(screen.getByText('production')).toBeInTheDocument()
  })
})

describe('StageTrack live tick', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('advances the connector band solid width as now moves past the poll time', () => {
    const updatedAt = 1_000_000
    // snapshot render: now == updatedAt — flush the useNow setTimeout(0) catch-up
    vi.spyOn(Date, 'now').mockReturnValue(updatedAt)
    const a = render(
      <StageTrack model={CI_MODEL} states={CI_ACTIVE} updatedAt={updatedAt}
        etaSeconds={300} etaLowSeconds={200} etaHighSeconds={600} etaPercent={50} />,
    )
    act(() => { vi.advanceTimersByTime(0) })
    const w0 = solidWidth(a.container)
    a.unmount()
    // advanced render: now 120s later — flush the catch-up so useNow picks up the new value
    vi.spyOn(Date, 'now').mockReturnValue(updatedAt + 120_000)
    const b = render(
      <StageTrack model={CI_MODEL} states={CI_ACTIVE} updatedAt={updatedAt}
        etaSeconds={300} etaLowSeconds={200} etaHighSeconds={600} etaPercent={50} />,
    )
    act(() => { vi.advanceTimersByTime(0) })
    expect(solidWidth(b.container)).toBeGreaterThan(w0)
  })

  it('switches the band to the amber overdue color on overrun', () => {
    const updatedAt = 1_000_000
    vi.spyOn(Date, 'now').mockReturnValue(updatedAt + 700_000) // 700s > etaHigh 600s
    const { container } = render(
      <StageTrack model={CI_MODEL} states={CI_ACTIVE} updatedAt={updatedAt}
        etaSeconds={300} etaLowSeconds={200} etaHighSeconds={600} etaPercent={50} />,
    )
    act(() => { vi.advanceTimersByTime(0) })
    const solid = container.querySelector('[data-testid="ci-band"] span') as HTMLElement
    expect(solid.className).toContain('bg-amber-500')
    expect(solid.style.width).toBe('100%')
  })
})
