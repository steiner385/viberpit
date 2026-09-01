import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { ProjectsTab } from '../ProjectsTab'

vi.mock('@/api/pm', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/pm')>()),
  fetchProjects: vi.fn().mockResolvedValue([{
    id: '1', slug: 'kindash', name: 'KinDash', description: null, status: 'active',
    portfolioTags: [], onboardingState: 'registered',
    createdAt: null, updatedAt: null,
    repos: [{ id: 'r1', repo: 'steiner385/KinDash', isPrimary: true,
              backfillDone: false, lastSyncedAt: null }],
    edges: [], featureCount: 0, workItemCount: 12,
  }]),
}))

function renderTab() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <ProjectsTab />
    </QueryClientProvider>,
  )
}

describe('ProjectsTab', () => {
  it('renders project cards with repo, state, and counts', async () => {
    renderTab()
    await waitFor(() => expect(screen.getByText('KinDash')).toBeInTheDocument())
    expect(screen.getByText('steiner385/KinDash')).toBeInTheDocument()
    expect(screen.getByText(/registered/i)).toBeInTheDocument()
    expect(screen.getByText(/12 work items/i)).toBeInTheDocument()
  })

  it('shows the empty state when there are no projects', async () => {
    const pm = await import('@/api/pm')
    vi.mocked(pm.fetchProjects).mockResolvedValueOnce([])
    renderTab()
    await waitFor(() =>
      expect(screen.getByText(/no projects yet/i)).toBeInTheDocument())
  })
})
