import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { ApiRequestError } from '@/api/client'

import { OnboardingWizard } from '../OnboardingWizard'

const classify = vi.fn().mockResolvedValue({ dispatched: 3 })
const proposeMap = vi.fn().mockResolvedValue({ sessionId: null })
const fetchOnboarding = vi.fn()

vi.mock('@/api/pm', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/pm')>()),
  fetchOnboarding: (...a: unknown[]) => fetchOnboarding(...a),
  runClassify: (...a: unknown[]) => classify(...a),
  proposeMap: (...a: unknown[]) => proposeMap(...a),
}))

const BASE_COUNTS = {
  repoCount: 1, backfillDoneCount: 1, featureCount: 4,
  openUnclassifiedCount: 30, pendingProposalCount: 0,
}

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
    fetchOnboarding.mockResolvedValueOnce({
      state: 'map_merged', action: { type: 'classify' },
      counts: BASE_COUNTS,
    })
    renderWizard()
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /classify backlog/i }))
        .toBeInTheDocument())
    expect(screen.getByText(/30 open items/i)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /classify backlog/i }))
    await waitFor(() => expect(classify).toHaveBeenCalledWith('ccswitch'))
  })

  it('offers the Propose feature map button for a propose_map action', async () => {
    fetchOnboarding.mockResolvedValueOnce({
      state: 'registered', action: { type: 'propose_map' },
      counts: BASE_COUNTS,
    })
    renderWizard()
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /propose feature map/i }))
        .toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /propose feature map/i }))
    await waitFor(() => expect(proposeMap).toHaveBeenCalledWith('ccswitch'))
  })

  it('renders the server error inline under Propose feature map when it fails', async () => {
    fetchOnboarding.mockResolvedValueOnce({
      state: 'registered', action: { type: 'propose_map' },
      counts: BASE_COUNTS,
    })
    proposeMap.mockRejectedValueOnce(
      new ApiRequestError(409, { detail: { message: 'nope' } }))
    renderWizard()
    fireEvent.click(await screen.findByRole('button', { name: /propose feature map/i }))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('nope')
    expect(alert).toHaveClass('pm-error')
  })

  it('renders the server error inline under Classify backlog when it fails', async () => {
    fetchOnboarding.mockResolvedValueOnce({
      state: 'map_merged', action: { type: 'classify' },
      counts: BASE_COUNTS,
    })
    classify.mockRejectedValueOnce(
      new ApiRequestError(409, { detail: { message: 'nope' } }))
    renderWizard()
    fireEvent.click(await screen.findByRole('button', { name: /classify backlog/i }))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('nope')
    expect(alert).toHaveClass('pm-error')
  })

  it('renders wait text and no action button for await_map_pr state', async () => {
    fetchOnboarding.mockResolvedValueOnce({
      state: 'map_proposed', action: { type: 'await_map_pr' },
      counts: BASE_COUNTS,
    })
    renderWizard()
    await waitFor(() =>
      expect(screen.getByText(/review.*merge the feature-map pr/i)).toBeInTheDocument())
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('renders the reason text for a needs_human action', async () => {
    fetchOnboarding.mockResolvedValueOnce({
      state: 'registered',
      action: { type: 'needs_human', reason: 'GitHub App not installed' },
      counts: BASE_COUNTS,
    })
    renderWizard()
    await waitFor(() =>
      expect(screen.getByText(/needs attention.*GitHub App not installed/i))
        .toBeInTheDocument())
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('renders nothing when state is live', async () => {
    fetchOnboarding.mockResolvedValueOnce({
      state: 'live', action: { type: 'live' },
      counts: BASE_COUNTS,
    })
    const { container } = renderWizard()
    // Give the query time to resolve, then assert the wizard is absent
    await waitFor(() => expect(fetchOnboarding).toHaveBeenCalled())
    // The live guard returns null — no wizard content should exist
    expect(container.firstChild).toBeNull()
  })
})
