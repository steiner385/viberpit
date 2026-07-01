import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { UseQueryResult } from '@tanstack/react-query'
import * as hooks from '../api/hooks'
import type { MergeTrainState, PipelinePr } from '../api/types'
import { PipelineView } from './PipelineView'

function stub<T>(data: T): UseQueryResult<T> {
  return { data, dataUpdatedAt: 0 } as unknown as UseQueryResult<T>
}

const testPr: PipelinePr = {
  number: 1,
  repo: 'cairnea/KinDash',
  title: 't',
  state: 'open',
  checks: 'pending',
  url: '',
  deployed: false,
  node_states: [],
  session: null,
}

const testTrain: MergeTrainState = {
  repo: 'cairnea/KinDash',
  depth: 5,
  trains_per_hour: 0.8,
  batch_success_pct: 38,
  ejects_24h: 46,
  oldest_wait_seconds: 5760,
  failing: [10823],
  building: null,
  blocked: [],
  awaiting_qa: 1,
  awaiting_prod: 42,
  merges_next: null,
}

function renderView() {
  vi.spyOn(hooks, 'useAllPrs').mockReturnValue(stub([testPr]))
  vi.spyOn(hooks, 'useStageModels').mockReturnValue(stub({}))
  vi.spyOn(hooks, 'useMergeTrains').mockReturnValue(stub({ 'cairnea/KinDash': testTrain }))
  vi.spyOn(hooks, 'useFixPr').mockReturnValue({ mutate: vi.fn() } as unknown as ReturnType<typeof hooks.useFixPr>)

  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}><PipelineView /></QueryClientProvider>
    </MemoryRouter>
  )
}

describe('PipelineView merge-train widget', () => {
  beforeEach(() => vi.clearAllMocks())

  it('shows the train widget for a repo with active queue data', () => {
    renderView()
    expect(screen.getByText(/depth 5/)).toBeInTheDocument()
    expect(screen.getByText(/#10823/)).toBeInTheDocument()
  })
})
