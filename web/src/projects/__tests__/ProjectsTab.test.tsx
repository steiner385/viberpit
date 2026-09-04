import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { ApiRequestError } from '@/api/client'

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
  createProject: vi.fn().mockResolvedValue({
    id: '2', slug: 'myapp', name: 'MyApp', description: null, status: 'active',
    portfolioTags: [], onboardingState: 'registered',
    createdAt: null, updatedAt: null,
    repos: [{ id: 'r2', repo: 'org/my-app', isPrimary: true,
              backfillDone: false, lastSyncedAt: null }],
    edges: [], featureCount: 0, workItemCount: 0,
  }),
  requestProjectSync: vi.fn().mockResolvedValue({ requested: true }),
  fetchProjectDeps: vi.fn().mockResolvedValue({
    project: null, edges: [],
    drift: { proseOnly: [], unresolvedTargets: [], missingFromProject: [], checklistEpics: [] },
  }),
  useProjectDeps: vi.fn().mockReturnValue({
    data: {
      project: null, edges: [],
      drift: { proseOnly: [], unresolvedTargets: [], missingFromProject: [], checklistEpics: [] },
    },
    isLoading: false,
  }),
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

  it('calls createProject with name and repo when the form is submitted', async () => {
    const user = userEvent.setup()
    const pm = await import('@/api/pm')
    renderTab()

    await user.click(screen.getByRole('button', { name: /new project/i }))

    await user.type(screen.getByPlaceholderText('KinDash'), 'MyApp')
    await user.type(screen.getByPlaceholderText('owner/repo'), 'org/my-app')

    await user.click(screen.getByRole('button', { name: /add project/i }))

    await waitFor(() =>
      expect(vi.mocked(pm.createProject)).toHaveBeenCalledWith({
        name: 'MyApp',
        repos: [{ repo: 'org/my-app', isPrimary: true }],
      }),
    )
  })

  it('renders the server error inline in the form when createProject fails', async () => {
    const user = userEvent.setup()
    const pm = await import('@/api/pm')
    vi.mocked(pm.createProject).mockRejectedValueOnce(
      new ApiRequestError(409, { detail: { message: 'nope' } }))
    renderTab()

    await user.click(screen.getByRole('button', { name: /new project/i }))
    await user.type(screen.getByPlaceholderText('KinDash'), 'MyApp')
    await user.type(screen.getByPlaceholderText('owner/repo'), 'org/my-app')
    await user.click(screen.getByRole('button', { name: /add project/i }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('nope')
    expect(alert).toHaveClass('pm-error')
    // Form stays open for a retry.
    expect(screen.getByPlaceholderText('KinDash')).toBeInTheDocument()
  })

  it('renders the server error inline next to Sync now when the sync request fails', async () => {
    const user = userEvent.setup()
    const pm = await import('@/api/pm')
    vi.mocked(pm.requestProjectSync).mockRejectedValueOnce(
      new ApiRequestError(409, { detail: { message: 'nope' } }))
    renderTab()

    await user.click(await screen.findByRole('button', { name: /sync now/i }))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('nope')
    expect(alert).toHaveClass('pm-error')
  })
})
