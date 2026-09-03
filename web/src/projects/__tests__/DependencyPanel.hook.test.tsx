import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { vi } from 'vitest'
import { DependencyPanel } from '../DependencyPanel'

// Mock ONLY the transport, so the real useProjectDeps hook + fetchProjectDeps
// run — this exercises the queryKey/endpoint wiring the wholesale-mocked
// DependencyPanel.test.tsx can't reach (issue #973).
const get = vi.fn()
vi.mock('@/api/client', () => ({
  api: { get: (...args: unknown[]) => get(...args) },
}))

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <DependencyPanel slug="ccswitch" />
    </QueryClientProvider>,
  )
}

it('drives the real hook: fetches the slug-scoped deps endpoint and renders edges', async () => {
  get.mockResolvedValue({
    project: null,
    edges: [{ src: 'o/r#1', dst: 'o/r#2', source: 'native', active: true, dstState: 'open' }],
    drift: { proseOnly: [], unresolvedTargets: [], missingFromProject: [], checklistEpics: [] },
  })
  renderPanel()
  await waitFor(() => expect(screen.getByText('o/r#1')).toBeInTheDocument())
  expect(get).toHaveBeenCalledWith('/pm/projects/ccswitch/deps')
})

it('renders an error state when the deps query fails, instead of null-ing forever', async () => {
  get.mockRejectedValue(new Error('boom'))
  renderPanel()
  await waitFor(() =>
    expect(screen.getByText(/failed to load dependencies/i)).toBeInTheDocument())
})
