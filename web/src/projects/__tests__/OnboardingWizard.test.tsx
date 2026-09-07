import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'

import { ApiRequestError } from '@/api/client'

import { OnboardingWizard } from '../OnboardingWizard'

const classify = vi.fn().mockResolvedValue({ dispatched: 3 })
const proposeMap = vi.fn().mockResolvedValue({ sessionId: null })
const fetchOnboarding = vi.fn()
const advanceMap = vi.fn().mockResolvedValue({ onboardingState: 'map_merged' })
const advanceLive = vi.fn().mockResolvedValue({ onboardingState: 'live' })

vi.mock('@/api/pm', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/pm')>()),
  fetchOnboarding: (...a: unknown[]) => fetchOnboarding(...a),
  runClassify: (...a: unknown[]) => classify(...a),
  proposeMap: (...a: unknown[]) => proposeMap(...a),
  advanceMap: (...a: unknown[]) => advanceMap(...a),
  advanceLive: (...a: unknown[]) => advanceLive(...a),
}))

const BASE_COUNTS = {
  repoCount: 1, backfillDoneCount: 1, featureCount: 4,
  openUnclassifiedCount: 30, pendingProposalCount: 0,
}

const MAP_PR = {
  branch: 'pm/feature-map-ccswitch',
  url: 'https://github.com/o/r/pulls?q=is%3Apr+head%3Apm%2Ffeature-map-ccswitch',
}

function renderWizard() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter><OnboardingWizard slug="ccswitch" /></MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('OnboardingWizard', () => {
  it('offers the Classify action for a map_merged project', async () => {
    fetchOnboarding.mockResolvedValueOnce({
      state: 'map_merged', action: { type: 'classify' },
      counts: BASE_COUNTS,
      mapPr: null,
    })
    renderWizard()
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /classify backlog/i }))
        .toBeInTheDocument())
    expect(screen.getByText(/30 open items/i)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /classify backlog/i }))
    await waitFor(() => expect(classify).toHaveBeenCalledWith('ccswitch'))
  })

  it('offers the Propose capability map button for a propose_map action', async () => {
    fetchOnboarding.mockResolvedValueOnce({
      state: 'registered', action: { type: 'propose_map' },
      counts: BASE_COUNTS,
      mapPr: null,
    })
    renderWizard()
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /propose capability map/i }))
        .toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /propose capability map/i }))
    await waitFor(() => expect(proposeMap).toHaveBeenCalledWith('ccswitch'))
  })

  it('renders the server error inline under Propose capability map when it fails', async () => {
    fetchOnboarding.mockResolvedValueOnce({
      state: 'registered', action: { type: 'propose_map' },
      counts: BASE_COUNTS,
      mapPr: null,
    })
    proposeMap.mockRejectedValueOnce(
      new ApiRequestError(409, { detail: { message: 'nope' } }))
    renderWizard()
    fireEvent.click(await screen.findByRole('button', { name: /propose capability map/i }))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('nope')
    expect(alert).toHaveClass('pm-error')
  })

  it('renders the server error inline under Classify backlog when it fails', async () => {
    fetchOnboarding.mockResolvedValueOnce({
      state: 'map_merged', action: { type: 'classify' },
      counts: BASE_COUNTS,
      mapPr: null,
    })
    classify.mockRejectedValueOnce(
      new ApiRequestError(409, { detail: { message: 'nope' } }))
    renderWizard()
    fireEvent.click(await screen.findByRole('button', { name: /classify backlog/i }))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('nope')
    expect(alert).toHaveClass('pm-error')
  })

  it('await_map_pr: links to the PR search, offers Map merged and Propose again', async () => {
    fetchOnboarding.mockResolvedValueOnce({
      state: 'map_proposed', action: { type: 'await_map_pr' },
      counts: BASE_COUNTS, mapPr: MAP_PR,
    })
    renderWizard()
    const link = await screen.findByRole('link', { name: /open the capability-map pr/i })
    expect(link).toHaveAttribute('href', MAP_PR.url)
    fireEvent.click(screen.getByRole('button', { name: /map merged/i }))
    await waitFor(() => expect(advanceMap).toHaveBeenCalledWith('ccswitch'))
    fireEvent.click(screen.getByRole('button', { name: /propose again/i }))
    await waitFor(() => expect(proposeMap).toHaveBeenCalledWith('ccswitch'))
  })

  it('await_map_pr without a primary repo still offers the advance, without a link', async () => {
    fetchOnboarding.mockResolvedValueOnce({
      state: 'map_proposed', action: { type: 'await_map_pr' },
      counts: BASE_COUNTS, mapPr: null,
    })
    renderWizard()
    await screen.findByRole('button', { name: /map merged/i })
    expect(screen.queryByRole('link')).toBeNull()
  })

  it('await_triage: links to the inbox scoped to this product and offers Go live', async () => {
    fetchOnboarding.mockResolvedValueOnce({
      state: 'classifying', action: { type: 'await_triage' },
      counts: { ...BASE_COUNTS, pendingProposalCount: 4 }, mapPr: MAP_PR,
    })
    renderWizard()
    const link = await screen.findByRole('link', { name: /review 4 proposals in triage/i })
    expect(link).toHaveAttribute('href', '/triage?projectSlug=ccswitch')
    fireEvent.click(screen.getByRole('button', { name: /go live/i }))
    await waitFor(() => expect(advanceLive).toHaveBeenCalledWith('ccswitch'))
  })

  it('register: says plainly that no repository is attached', async () => {
    fetchOnboarding.mockResolvedValueOnce({
      state: 'registered', action: { type: 'register' },
      counts: { ...BASE_COUNTS, repoCount: 0 }, mapPr: null,
    })
    renderWizard()
    await screen.findByText(/no repository is attached to this product/i)
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('renders the reason text for a needs_human action', async () => {
    fetchOnboarding.mockResolvedValueOnce({
      state: 'registered',
      action: { type: 'needs_human', reason: 'GitHub App not installed' },
      counts: BASE_COUNTS,
      mapPr: null,
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
      mapPr: null,
    })
    const { container } = renderWizard()
    // Give the query time to resolve, then assert the wizard is absent
    await waitFor(() => expect(fetchOnboarding).toHaveBeenCalled())
    // The live guard returns null — no wizard content should exist
    expect(container.firstChild).toBeNull()
  })
})
