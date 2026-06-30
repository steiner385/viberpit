// web/src/components/__tests__/StageTiles.test.tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { StageTiles } from '@/components/StageTiles'
import type { PipelinePr, StageModel } from '@/api/types'

const model: StageModel = { repo: 'o/r', nodes: [
  { id: 'queue', label: 'Queue', kind: 'queue', order: 0, source: 'derived' },
  { id: 'merged', label: 'Merged', kind: 'merged', order: 1, source: 'derived' },
] }
const pr = (n: number, ns: { id: string; state: 'active' | 'done' | 'pending' | 'failed' }[]): PipelinePr =>
  ({ number: n, url: 'u', state: 'open', checks: 'pending', title: 't', deployed: false,
     repo: 'o/r', session: null, node_states: ns } as unknown as PipelinePr)

const prs = [
  pr(1, [{ id: 'queue', state: 'active' }]),
  pr(2, [{ id: 'merged', state: 'done' }]),
  pr(3, [{ id: 'merged', state: 'done' }]),
]

describe('StageTiles', () => {
  it('counts PRs per bucket', () => {
    render(<StageTiles prs={prs} stageModels={{ 'o/r': model }} active={new Set()} onToggle={() => {}} />)
    expect(screen.getByTestId('stage-tile-queue')).toHaveTextContent('1')
    expect(screen.getByTestId('stage-tile-merged')).toHaveTextContent('2')
  })
  it('disables a zero-count tile', () => {
    render(<StageTiles prs={prs} stageModels={{ 'o/r': model }} active={new Set()} onToggle={() => {}} />)
    expect(screen.getByTestId('stage-tile-ci')).toBeDisabled()
  })
  it('marks an active tile pressed and toggles on click', async () => {
    const onToggle = vi.fn()
    const user = userEvent.setup()
    render(<StageTiles prs={prs} stageModels={{ 'o/r': model }} active={new Set(['queue'])} onToggle={onToggle} />)
    expect(screen.getByTestId('stage-tile-queue')).toHaveAttribute('aria-pressed', 'true')
    await user.click(screen.getByTestId('stage-tile-merged'))
    expect(onToggle).toHaveBeenCalledWith('merged')
  })
  it('renders tiles in CI · Ready · Queue · Merged · Deploy order', () => {
    render(<StageTiles prs={[]} stageModels={{}} active={new Set()} onToggle={() => {}} />)
    const order = screen.getAllByTestId(/^stage-tile-/).map((el) => el.getAttribute('data-testid'))
    expect(order).toEqual([
      'stage-tile-ci', 'stage-tile-ready', 'stage-tile-queue', 'stage-tile-merged', 'stage-tile-deploy',
    ])
  })
})
