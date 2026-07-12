import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { PipelineTab, laneJumpTarget } from '../PipelineTab'

vi.stubGlobal('EventSource', class {
  onmessage = null; onerror = null
  addEventListener() {} close() {}
} as unknown as typeof EventSource)

it('renders the loading state before the first SSE frame', () => {
  const qc = new QueryClient()
  render(<MemoryRouter><QueryClientProvider client={qc}><PipelineTab /></QueryClientProvider></MemoryRouter>)
  expect(screen.getByRole('status')).toHaveTextContent('Loading pipeline…')
})

describe('laneJumpTarget', () => {
  it('maps pr-ci red → failed filter', () => {
    expect(laneJumpTarget('pr-ci', 'red')).toEqual({ kind: 'filter', bucket: 'failed' })
  })
  it('maps pr-ci non-red → running filter', () => {
    expect(laneJumpTarget('pr-ci', 'green')).toEqual({ kind: 'filter', bucket: 'running' })
    expect(laneJumpTarget('pr-ci', 'amber')).toEqual({ kind: 'filter', bucket: 'running' })
    expect(laneJumpTarget('pr-ci', null)).toEqual({ kind: 'filter', bucket: 'running' })
  })
  it('maps merge-queue → queued filter', () => {
    expect(laneJumpTarget('merge-queue', 'amber')).toEqual({ kind: 'filter', bucket: 'queued' })
  })
  it('maps deploy → deploy filter', () => {
    expect(laneJumpTarget('deploy', 'green')).toEqual({ kind: 'filter', bucket: 'deploy' })
  })
  it('maps failures → failed filter', () => {
    expect(laneJumpTarget('failures', 'red')).toEqual({ kind: 'filter', bucket: 'failed' })
  })
  it('maps main / scheduled / cost / null → insights', () => {
    expect(laneJumpTarget('main', 'green')).toEqual({ kind: 'insights' })
    expect(laneJumpTarget('scheduled', 'amber')).toEqual({ kind: 'insights' })
    expect(laneJumpTarget('cost', null)).toEqual({ kind: 'insights' })
    expect(laneJumpTarget(null, null)).toEqual({ kind: 'insights' })
  })
})
