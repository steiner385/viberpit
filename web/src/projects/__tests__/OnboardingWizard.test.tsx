import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { OnboardingWizard } from '../OnboardingWizard'

const classify = vi.fn().mockResolvedValue({ dispatched: 3 })

vi.mock('@/api/pm', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/pm')>()),
  fetchOnboarding: vi.fn().mockResolvedValue({
    state: 'map_merged', action: { type: 'classify' },
    counts: { repoCount: 1, backfillDoneCount: 1, featureCount: 4,
              openUnclassifiedCount: 30, pendingProposalCount: 0 },
  }),
  runClassify: (...a: unknown[]) => classify(...a),
}))

function renderWizard() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <OnboardingWizard slug="ccswitch" />
    </QueryClientProvider>,
  )
}

describe('OnboardingWizard', () => {
  it('offers the Classify action for a map_merged project', async () => {
    renderWizard()
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /classify backlog/i }))
        .toBeInTheDocument())
    expect(screen.getByText(/30 open items/i)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /classify backlog/i }))
    await waitFor(() => expect(classify).toHaveBeenCalledWith('ccswitch'))
  })
})
