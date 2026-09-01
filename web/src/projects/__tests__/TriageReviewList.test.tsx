import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { TriageReviewList } from '../TriageReviewList'

const accept = vi.fn().mockResolvedValue({ id: 'p1', status: 'accepted' })
const reject = vi.fn().mockResolvedValue({ id: 'p1', status: 'rejected' })

vi.mock('@/api/triage', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/triage')>()),
  fetchTriage: vi.fn().mockResolvedValue([{
    id: 'p1', workItemId: 'w1', repo: 'o/r', number: 7, kind: 'issue',
    title: 'Fix sessions', url: 'https://github.com/o/r/issues/7',
    proposedFeatureId: 'f1', proposedFeatureSlug: 'sessions',
    noFitSuggestion: null, reach: 3, impact: 2, effort: 1, confidence: 2,
    rationale: 'touches sessions', confidenceLevel: 'high', status: 'pending',
    createdAt: null,
  }, {
    id: 'p2', workItemId: 'w2', repo: 'o/r', number: 8, kind: 'issue',
    title: 'Billing thing', url: null, proposedFeatureId: null,
    proposedFeatureSlug: null, noFitSuggestion: 'billing',
    reach: null, impact: null, effort: null, confidence: null,
    rationale: 'no fit', confidenceLevel: 'low', status: 'pending',
    createdAt: null,
  }]),
  acceptProposal: (...a: unknown[]) => accept(...a),
  rejectProposal: (...a: unknown[]) => reject(...a),
}))

function renderList() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}><TriageReviewList /></QueryClientProvider>,
  )
}

describe('TriageReviewList', () => {
  it('renders proposals with feature, no_fit suggestion and confidence', async () => {
    renderList()
    await waitFor(() => expect(screen.getByText('Fix sessions')).toBeInTheDocument())
    expect(screen.getByText('sessions')).toBeInTheDocument()
    expect(screen.getByText(/no fit.*billing/i)).toBeInTheDocument()  // no_fit suggestion
    expect(screen.getByText(/high/i)).toBeInTheDocument()
  })

  it('accepts a proposal', async () => {
    renderList()
    await waitFor(() => expect(screen.getByText('Fix sessions')).toBeInTheDocument())
    fireEvent.click(screen.getAllByRole('button', { name: /accept/i })[0])
    // Hook variable is { id, edits? }; the fetcher is called (id, edits).
    await waitFor(() => expect(accept).toHaveBeenCalledWith('p1', undefined))
  })

  it('rejects a proposal', async () => {
    renderList()
    await waitFor(() => expect(screen.getByText('Billing thing')).toBeInTheDocument())
    fireEvent.click(screen.getAllByRole('button', { name: /reject/i })[1])
    await waitFor(() => expect(reject).toHaveBeenCalledWith('p2'))
  })

  it('shows the empty state', async () => {
    const triage = await import('@/api/triage')
    vi.mocked(triage.fetchTriage).mockResolvedValueOnce([])
    renderList()
    await waitFor(() =>
      expect(screen.getByText(/nothing to triage/i)).toBeInTheDocument())
  })
})
