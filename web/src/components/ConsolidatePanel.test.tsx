// web/src/components/ConsolidatePanel.test.tsx
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { ConsolidatePanel } from './ConsolidatePanel'

const mutate = vi.fn()
const baseGroup = () => ({
  candidates: [
    { number: 1, title: 'fix(deploy): a', url: 'u1', checks: 'passing', mergeable: 'MERGEABLE', updated_at: '', scope: 'deploy' },
    { number: 2, title: 'fix(deploy): b', url: 'u2', checks: 'passing', mergeable: 'MERGEABLE', updated_at: '', scope: 'deploy' },
  ],
  clusters: [{ numbers: [1, 2], rationale: '2 PRs share scope `deploy`, all green', risk: 'low' }],
  active: [] as { session_id: string; numbers: number[] }[],
})
let group = baseGroup()
vi.mock('@/api/hooks', () => ({
  useConsolidation: () => ({
    data: { repos: { 'o/r': group } },
    isLoading: false,
  }),
  useConsolidate: () => ({ mutateAsync: mutate, isPending: false }),
  useAddCopilotAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useCopilotAddStatus: vi.fn(() => ({ data: undefined, isLoading: false })),
  useCancelCopilotAdd: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useAssessDispatch: vi.fn(() => ({ assess: vi.fn() })),
}))

function selectBothAndRun() {
  fireEvent.click(screen.getByLabelText('select PR 1'))
  fireEvent.click(screen.getByLabelText('select PR 2'))
  fireEvent.click(screen.getByRole('button', { name: /consolidate/i }))
}

describe('ConsolidatePanel', () => {
  beforeEach(() => {
    group = baseGroup()
    mutate.mockReset()
    mutate.mockResolvedValue({ status: 'spawned', level: 'ok', message: 'ok' })
  })

  it('renders candidate rows and a cluster chip', () => {
    render(<ConsolidatePanel enabled />)
    expect(screen.getByText(/fix\(deploy\): a/)).toBeInTheDocument()
    expect(screen.getByText(/2 PRs share scope/)).toBeInTheDocument()
  })

  it('button is disabled until at least 2 are selected, then posts {repo, numbers}', async () => {
    render(<ConsolidatePanel enabled />)
    const btn = screen.getByRole('button', { name: /consolidate/i })
    expect(btn).toBeDisabled()                              // nothing selected
    fireEvent.click(screen.getByLabelText('select PR 1'))
    fireEvent.click(screen.getByLabelText('select PR 2'))
    expect(btn).toBeEnabled()
    fireEvent.click(btn)
    await waitFor(() => expect(mutate).toHaveBeenCalledWith({ repo: 'o/r', numbers: [1, 2] }))
  })

  it('a cluster chip selects its numbers', () => {
    render(<ConsolidatePanel enabled />)
    fireEvent.click(screen.getByText(/2 PRs share scope/))
    expect(screen.getByLabelText('select PR 1')).toBeChecked()
    expect(screen.getByLabelText('select PR 2')).toBeChecked()
  })

  it('shows no underway indicator when no consolidation session is live', () => {
    render(<ConsolidatePanel enabled />)
    expect(screen.queryByRole('button', { name: /consolidation underway/i })).toBeNull()
  })

  it('flags rows covered by a live consolidation and links to its session', () => {
    group.active = [{ session_id: 's1', numbers: [1, 2] }]
    const onOpenSession = vi.fn()
    render(<ConsolidatePanel enabled onOpenSession={onOpenSession} />)
    const badges = screen.getAllByRole('button', { name: /consolidation underway/i })
    expect(badges).toHaveLength(2)
    fireEvent.click(badges[0])
    expect(onOpenSession).toHaveBeenCalledWith('s1')
  })

  it('renders an ok outcome inline as a status and clears the selection', async () => {
    mutate.mockResolvedValue({ status: 'spawned', level: 'ok', message: 'Consolidation started' })
    render(<ConsolidatePanel enabled />)
    selectBothAndRun()
    await waitFor(() =>
      expect(screen.getByRole('status')).toHaveTextContent('Consolidation started'),
    )
    expect(screen.getByLabelText('select PR 1')).not.toBeChecked()
    expect(screen.getByLabelText('select PR 2')).not.toBeChecked()
  })

  it('renders a warn outcome inline as a status and keeps the selection', async () => {
    mutate.mockResolvedValue({ status: 'skipped', level: 'warn', message: 'Already underway' })
    render(<ConsolidatePanel enabled />)
    selectBothAndRun()
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Already underway'))
    expect(screen.getByLabelText('select PR 1')).toBeChecked()
  })

  it('renders an error outcome inline as an alert', async () => {
    mutate.mockResolvedValue({ status: 'error', level: 'error', message: 'No capacity' })
    render(<ConsolidatePanel enabled />)
    selectBothAndRun()
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('No capacity'))
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('renders a thrown mutation as an inline error', async () => {
    mutate.mockRejectedValue(new Error('network error'))
    render(<ConsolidatePanel enabled />)
    selectBothAndRun()
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Failed to consolidate'))
  })
})
