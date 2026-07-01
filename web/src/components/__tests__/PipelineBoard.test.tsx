import { describe, it, expect, vi } from 'vitest'
import userEvent from '@testing-library/user-event'
import { render, screen, fireEvent } from '@testing-library/react'
import { PipelineBoard } from '../PipelineView'
import type { PipelinePr, StageModel } from '@/api/types'
import type { StageBucket } from '@/session/stageBucket'

const ciModel: StageModel = {
  repo: 'o/n',
  nodes: [
    { id: 'ci', label: 'CI', kind: 'ci', order: 0, source: 'derived' },
    { id: 'ready', label: 'Ready', kind: 'gate', order: 1, source: 'derived' },
    { id: 'merged', label: 'Merged', kind: 'merged', order: 2, source: 'derived' },
  ],
}

const models: Record<string, StageModel> = { 'o/n': ciModel }

function pr(over: Partial<PipelinePr> & { eta_seconds?: number | null; eta_overdue?: boolean }): PipelinePr {
  return {
    number: 1, url: 'u', state: 'open', checks: 'none', title: 't',
    deployed: false, repo: 'o/n', stage: 'ready', session: null,
    node_states: [
      { id: 'ci', state: 'done' },
      { id: 'ready', state: 'active' },
      { id: 'merged', state: 'pending' },
    ],
    ...over,
  }
}

describe('PipelineBoard', () => {
  it('groups PRs by repo and shows a GroupHeader per repo', () => {
    render(
      <PipelineBoard
        prs={[
          pr({ number: 1, repo: 'o/n', stage: 'ci' }),
          pr({ number: 2, repo: 'o/n', stage: 'ready' }),
          pr({ number: 3, repo: 'other/repo', stage: 'ci' }),
        ]}
        stageModels={{ ...models, 'other/repo': { repo: 'other/repo', nodes: ciModel.nodes } }}
        activeStages={new Set()}
        updatedAt={Date.now()}
      />,
    )
    // Both repo headers should be present
    expect(screen.getAllByRole('button').some((b) => b.textContent?.includes('o/n'))).toBe(true)
    expect(screen.getAllByRole('button').some((b) => b.textContent?.includes('other/repo'))).toBe(true)
  })

  it('sorts the repo with a failing PR first', () => {
    render(
      <PipelineBoard
        prs={[
          pr({ number: 1, repo: 'aaa/good', stage: 'ready', checks: 'passing' }),
          pr({ number: 2, repo: 'zzz/broken', stage: 'ci', checks: 'failing' }),
        ]}
        stageModels={{ 'aaa/good': ciModel, 'zzz/broken': ciModel }}
        activeStages={new Set()}
        updatedAt={Date.now()}
      />,
    )
    const headers = screen.getAllByRole('button')
    const names = headers.map((h) => h.textContent ?? '')
    const goodIdx = names.findIndex((n) => n.includes('aaa/good'))
    const brokenIdx = names.findIndex((n) => n.includes('zzz/broken'))
    expect(brokenIdx).toBeLessThan(goodIdx)
  })

  it('renders a StageTrack in each row', () => {
    render(
      <PipelineBoard
        prs={[pr({ number: 1 })]}
        stageModels={models}
        activeStages={new Set()}
        updatedAt={Date.now()}
      />,
    )
    expect(screen.getByTestId('stage-node-ci')).toBeInTheDocument()
  })

  it('shows an empty state when there are no PRs', () => {
    render(<PipelineBoard prs={[]} stageModels={{}} activeStages={new Set()} updatedAt={Date.now()} />)
    expect(screen.getByText(/no open prs/i)).toBeInTheDocument()
  })

  it('session-linked failing PR opens its session and offers Fix; unlinked does not', () => {
    const onOpenSession = vi.fn()
    const onFixPr = vi.fn()
    render(
      <PipelineBoard
        prs={[
          pr({ number: 5, stage: 'parked', checks: 'failing',
               session: { id: 's1', name: 'Fix me' } }),
          pr({ number: 6, stage: 'parked', checks: 'failing', session: null }),
        ]}
        stageModels={models}
        activeStages={new Set()}
        updatedAt={Date.now()}
        onOpenSession={onOpenSession}
        onFixPr={onFixPr}
      />,
    )
    fireEvent.click(screen.getByTestId('pipeline-row-5'))
    expect(onOpenSession).toHaveBeenCalledWith('s1')

    fireEvent.click(screen.getByTestId('pipeline-fix-5'))
    expect(onFixPr).toHaveBeenCalledWith('s1', 5)

    // Unlinked PR #6 has no Fix button.
    expect(screen.queryByTestId('pipeline-fix-6')).toBeNull()
  })

  it('marks the far-right label as the corresponding session (icon + tooltip)', () => {
    render(
      <PipelineBoard
        prs={[pr({ number: 7, stage: 'ready', session: { id: 's7', name: 'My session' } })]}
        stageModels={models}
        activeStages={new Set()}
        updatedAt={Date.now()}
      />,
    )
    const label = screen.getByTestId('pipeline-session-7')
    expect(label).toHaveTextContent('My session')
    expect(label).toHaveAttribute('title', 'Session: My session')
    expect(label.querySelector('svg')).not.toBeNull()
  })

  it('expands a PR row to show its check drill-down', async () => {
    const user = userEvent.setup()
    const prs = [{
      repo: 'o/r', number: 5, url: 'u', state: 'open', checks: 'passing', title: 'feat',
      deployed: false, session: null, node_states: [],
      check_groups: [{ kind: 'head', total_seconds: 60, cost_usd: 0.01, eta_seconds: null,
        checks: [{ name: 'lint', workflow: 'CI', status: 'COMPLETED', conclusion: 'success',
          duration_seconds: 60, required: true, overdue: false }] }],
    }] as unknown as PipelinePr[]
    render(<PipelineBoard prs={prs} stageModels={{}} activeStages={new Set()} updatedAt={Date.now()} />)
    expect(screen.queryByTestId('check-row-lint')).toBeNull()
    await user.click(screen.getByTestId('pipeline-expand-5'))
    expect(screen.getByTestId('check-row-lint')).toBeInTheDocument()
  })

  it('expand chevron does not trigger row navigation', async () => {
    const user = userEvent.setup()
    const onOpenSession = vi.fn()
    const prs = [{ repo: 'o/r', number: 5, url: 'u', state: 'open', checks: 'passing',
      title: 'feat', deployed: false, session: { id: 's1', name: 'sess' }, node_states: [],
      check_groups: [{ kind: 'head', total_seconds: 0, cost_usd: null, eta_seconds: null,
        checks: [] }] }] as unknown as PipelinePr[]
    render(<PipelineBoard prs={prs} stageModels={{}} activeStages={new Set()} updatedAt={Date.now()} onOpenSession={onOpenSession} />)
    await user.click(screen.getByTestId('pipeline-expand-5'))
    expect(onOpenSession).not.toHaveBeenCalled()
  })

  it('filters PRs to the active stage buckets and hides empty repos', () => {
    const model = { repo: 'o/r', nodes: [
      { id: 'queue', label: 'Queue', kind: 'queue', order: 0, source: 'derived' },
      { id: 'merged', label: 'Merged', kind: 'merged', order: 1, source: 'derived' },
    ] }
    const prs = [
      { repo: 'o/r', number: 1, url: 'u', state: 'open', checks: 'pending', title: 'q', deployed: false,
        session: null, node_states: [{ id: 'queue', state: 'active' }] },
      { repo: 'o/r', number: 2, url: 'u', state: 'merged', checks: 'passing', title: 'm', deployed: false,
        session: null, node_states: [{ id: 'merged', state: 'done' }] },
      { repo: 'x/y', number: 3, url: 'u', state: 'merged', checks: 'passing', title: 'm2', deployed: false,
        session: null, node_states: [{ id: 'merged', state: 'done' }] },
    ] as unknown as PipelinePr[]
    const models = { 'o/r': model, 'x/y': { ...model, repo: 'x/y' } } as Record<string, StageModel>
    // queue filter → only PR #1 and only repo o/r
    render(<PipelineBoard prs={prs} stageModels={models} activeStages={new Set<StageBucket>(['queue'])} updatedAt={Date.now()} />)
    expect(screen.getByTestId('pipeline-row-1')).toBeInTheDocument()
    expect(screen.queryByTestId('pipeline-row-2')).toBeNull()
    expect(screen.queryByTestId('pipeline-row-3')).toBeNull()
  })

  it('empty active set shows everything', () => {
    const prs = [{ repo: 'o/r', number: 1, url: 'u', state: 'open', checks: 'pending', title: 't',
      deployed: false, session: null, node_states: [] }] as unknown as PipelinePr[]
    render(<PipelineBoard prs={prs} stageModels={{}} activeStages={new Set()} updatedAt={Date.now()} />)
    expect(screen.getByTestId('pipeline-row-1')).toBeInTheDocument()
  })

  it('failedOnly shows only PRs whose checks are failing', () => {
    render(
      <PipelineBoard
        prs={[
          pr({ number: 1, repo: 'o/n', stage: 'ci', checks: 'failing' }),
          pr({ number: 2, repo: 'o/n', stage: 'ready', checks: 'passing' }),
        ]}
        stageModels={models}
        activeStages={new Set()}
        updatedAt={Date.now()}
        failedOnly
      />,
    )
    expect(screen.getByTestId('pipeline-row-1')).toBeInTheDocument()
    expect(screen.queryByTestId('pipeline-row-2')).toBeNull()
  })

  it('failedOnly excludes a closed PR with a frozen failing rollup', () => {
    render(
      <PipelineBoard
        prs={[
          pr({ number: 1, repo: 'o/n', stage: 'ci', checks: 'failing', state: 'open' }),
          pr({ number: 2, repo: 'o/n', stage: 'ci', checks: 'failing', state: 'closed' }),
        ]}
        stageModels={models}
        activeStages={new Set()}
        updatedAt={Date.now()}
        failedOnly
      />,
    )
    expect(screen.getByTestId('pipeline-row-1')).toBeInTheDocument()
    expect(screen.queryByTestId('pipeline-row-2')).toBeNull()
  })

  it('failedOnly AND-combines with the active stage filter', () => {
    const ciStates = [
      { id: 'ci', state: 'failed' as const },
      { id: 'ready', state: 'pending' as const },
      { id: 'merged', state: 'pending' as const },
    ]
    const readyStates = [
      { id: 'ci', state: 'done' as const },
      { id: 'ready', state: 'active' as const },
      { id: 'merged', state: 'pending' as const },
    ]
    render(
      <PipelineBoard
        prs={[
          // CI bucket + failing → kept by BOTH filters
          pr({ number: 1, repo: 'o/n', stage: 'ci', checks: 'failing', node_states: ciStates }),
          // CI bucket + passing → kept by stage, dropped by failedOnly
          pr({ number: 2, repo: 'o/n', stage: 'ci', checks: 'passing', node_states: ciStates }),
          // Ready bucket + failing → kept by failedOnly, dropped by the stage filter
          pr({ number: 3, repo: 'o/n', stage: 'ready', checks: 'failing', node_states: readyStates }),
        ]}
        stageModels={models}
        activeStages={new Set<StageBucket>(['ci'])}
        updatedAt={Date.now()}
        failedOnly
      />,
    )
    expect(screen.getByTestId('pipeline-row-1')).toBeInTheDocument()
    expect(screen.queryByTestId('pipeline-row-2')).toBeNull()
    expect(screen.queryByTestId('pipeline-row-3')).toBeNull()
  })

  it('failedOnly with no failing PRs shows a filtered empty state', () => {
    render(
      <PipelineBoard
        prs={[pr({ number: 1, repo: 'o/n', stage: 'ci', checks: 'passing' })]}
        stageModels={models}
        activeStages={new Set()}
        updatedAt={Date.now()}
        failedOnly
      />,
    )
    expect(screen.queryByTestId('pipeline-row-1')).toBeNull()
    expect(screen.getByText(/no prs match/i)).toBeInTheDocument()
  })
})
