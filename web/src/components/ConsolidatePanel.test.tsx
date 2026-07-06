// web/src/components/ConsolidatePanel.test.tsx
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { ConsolidatePanel } from './ConsolidatePanel'

const mutate = vi.fn()
vi.mock('@/api/hooks', () => ({
  useConsolidation: () => ({
    data: { repos: { 'o/r': {
      candidates: [
        { number: 1, title: 'fix(deploy): a', url: 'u1', checks: 'passing', mergeable: 'MERGEABLE', updated_at: '', scope: 'deploy' },
        { number: 2, title: 'fix(deploy): b', url: 'u2', checks: 'passing', mergeable: 'MERGEABLE', updated_at: '', scope: 'deploy' },
      ],
      clusters: [{ numbers: [1, 2], rationale: '2 PRs share scope `deploy`, all green', risk: 'low' }],
    } } },
    isLoading: false,
  }),
  useConsolidate: () => ({ mutateAsync: mutate, isPending: false }),
  useAddCopilotAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useCopilotAddStatus: vi.fn(() => ({ data: undefined, isLoading: false })),
  useCancelCopilotAdd: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
}))
vi.mock('sonner', () => ({ toast: { success: vi.fn(), warning: vi.fn(), error: vi.fn() } }))

describe('ConsolidatePanel', () => {
  beforeEach(() => { mutate.mockReset(); mutate.mockResolvedValue({ status: 'spawned', level: 'ok', message: 'ok' }) })

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
})
