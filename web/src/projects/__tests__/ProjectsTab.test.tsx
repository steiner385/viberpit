import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import { ApiRequestError } from '@/api/client'

import { ProjectsTab } from '../ProjectsTab'

// The embedded phase-2 triage list is gone; what is left is a LINK to /triage
// whose label carries the queue depth, so the tab still reads the triage query.
vi.mock('@/api/triage', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/triage')>()),
  fetchTriage: vi.fn().mockResolvedValue([{ id: 'p1' }, { id: 'p2' }]),
}))

vi.mock('@/api/pm', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/pm')>()),
  fetchProjects: vi.fn().mockResolvedValue([{
    id: '1', slug: 'example-product', name: 'Example product', description: null, status: 'active',
    portfolioTags: [], onboardingState: 'registered',
    createdAt: null, updatedAt: null,
    repos: [{ id: 'r1', repo: 'o/example-product', isPrimary: true,
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
      <MemoryRouter><ProjectsTab /></MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('ProjectsTab', () => {
  it('renders project cards with repo, state, and counts', async () => {
    renderTab()
    await waitFor(() => expect(screen.getByText('Example product')).toBeInTheDocument())
    expect(screen.getByText('o/example-product')).toBeInTheDocument()
    expect(screen.getByText(/registered/i)).toBeInTheDocument()
    expect(screen.getByText(/12 work items/i)).toBeInTheDocument()
  })

  it('links to the Triage inbox with the queue depth instead of embedding a list', async () => {
    renderTab()
    const cta = await screen.findByRole('link', { name: /review 2 proposals in triage/i })
    expect(cta).toHaveAttribute('href', '/triage')
    // The phase-2 list is gone: no Accept/Reject buttons live on this tab.
    expect(screen.queryByRole('button', { name: /^accept$/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /^reject$/i })).toBeNull()
  })

  it('shows the empty state when there are no projects', async () => {
    const pm = await import('@/api/pm')
    vi.mocked(pm.fetchProjects).mockResolvedValueOnce([])
    renderTab()
    await waitFor(() =>
      expect(screen.getByText(/no products yet/i)).toBeInTheDocument())
  })

  it('calls createProject with name and repo when the form is submitted', async () => {
    const user = userEvent.setup()
    const pm = await import('@/api/pm')
    renderTab()

    await user.click(screen.getByRole('button', { name: /new product/i }))

    await user.type(screen.getByPlaceholderText('My product'), 'MyApp')
    await user.type(screen.getByPlaceholderText('owner/repo'), 'org/my-app')

    await user.click(screen.getByRole('button', { name: /add product/i }))

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

    await user.click(screen.getByRole('button', { name: /new product/i }))
    await user.type(screen.getByPlaceholderText('My product'), 'MyApp')
    await user.type(screen.getByPlaceholderText('owner/repo'), 'org/my-app')
    await user.click(screen.getByRole('button', { name: /add product/i }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('nope')
    expect(alert).toHaveClass('pm-error')
    // Form stays open for a retry.
    expect(screen.getByPlaceholderText('My product')).toBeInTheDocument()
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
