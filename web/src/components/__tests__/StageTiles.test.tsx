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

const failingPr = (n: number, over: Partial<PipelinePr> = {}): PipelinePr =>
  ({ number: n, url: 'u', state: 'open', checks: 'failing', title: 't', deployed: false,
     repo: 'o/r', session: null, node_states: [{ id: 'queue', state: 'failed' }], ...over } as unknown as PipelinePr)

describe('StageTiles — Failed filter', () => {
  it('counts failing PRs in the Failed tile', () => {
    render(<StageTiles prs={[...prs, failingPr(9)]} stageModels={{ 'o/r': model }}
      active={new Set()} onToggle={() => {}} failedActive={false} onToggleFailed={() => {}} />)
    expect(screen.getByTestId('pr-filter-failed')).toHaveTextContent('1')
  })

  it('disables the Failed tile when no PRs are failing', () => {
    render(<StageTiles prs={prs} stageModels={{ 'o/r': model }}
      active={new Set()} onToggle={() => {}} failedActive={false} onToggleFailed={() => {}} />)
    expect(screen.getByTestId('pr-filter-failed')).toBeDisabled()
  })

  it('marks the Failed tile pressed and toggles on click', async () => {
    const onToggleFailed = vi.fn()
    const user = userEvent.setup()
    render(<StageTiles prs={[failingPr(9)]} stageModels={{ 'o/r': model }}
      active={new Set()} onToggle={() => {}} failedActive onToggleFailed={onToggleFailed} />)
    expect(screen.getByTestId('pr-filter-failed')).toHaveAttribute('aria-pressed', 'true')
    await user.click(screen.getByTestId('pr-filter-failed'))
    expect(onToggleFailed).toHaveBeenCalled()
  })

  it('excludes a closed PR with a frozen failing rollup from the count', () => {
    render(<StageTiles prs={[failingPr(8, { state: 'closed' })]} stageModels={{ 'o/r': model }}
      active={new Set()} onToggle={() => {}} failedActive={false} onToggleFailed={() => {}} />)
    expect(screen.getByTestId('pr-filter-failed')).toHaveTextContent('0')
    expect(screen.getByTestId('pr-filter-failed')).toBeDisabled()
  })

  it('labels the filter row as a PR filter group', () => {
    render(<StageTiles prs={prs} stageModels={{ 'o/r': model }}
      active={new Set()} onToggle={() => {}} failedActive={false} onToggleFailed={() => {}} />)
    expect(screen.getByRole('group', { name: /filter/i })).toBeInTheDocument()
  })
})
