import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { vi } from 'vitest'
import { DependencyPanel } from '../DependencyPanel'
import * as pm from '@/api/pm'

const mockUseProjectDeps = vi.fn()

vi.mock('@/api/pm', async (importOriginal) => ({
  ...(await importOriginal<typeof pm>()),
  fetchProjectDeps: vi.fn(),
  useProjectDeps: (...args: unknown[]) => mockUseProjectDeps(...args),
}))

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <DependencyPanel slug="ccswitch" />
    </QueryClientProvider>,
  )
}

it('renders blocked-by edges grouped by source item and drift counts', async () => {
  mockUseProjectDeps.mockReturnValue({
    data: {
      project: { number: 7, title: 'cc-account-switcher', lastSyncedAt: null },
      edges: [
        { src: 'o/r#950', dst: 'o/r#12', source: 'native', active: true, dstState: 'open' },
        { src: 'o/r#950', dst: 'o/r#949', source: 'prose', active: true, dstState: 'closed' },
        { src: 'o/r#951', dst: 'o/r#950', source: 'order', active: false, dstState: 'open' },
      ],
      drift: { proseOnly: [{ src: 'o/r#950', dst: 'o/r#949' }], unresolvedTargets: [],
               missingFromProject: ['o/r#960'], checklistEpics: [] },
    },
    isLoading: false,
  })
  renderPanel()
  await waitFor(() => expect(screen.getByText('o/r#950')).toBeInTheDocument())
  expect(screen.getByText('o/r#12')).toBeInTheDocument()
  // inactive edges are not rendered
  expect(screen.queryByText('o/r#951')).not.toBeInTheDocument()
  expect(screen.getByText(/1 prose-only/i)).toBeInTheDocument()
  expect(screen.getByText(/1 off board/i)).toBeInTheDocument()
})

it('renders an empty state when the mirror has no edges', async () => {
  mockUseProjectDeps.mockReturnValue({
    data: {
      project: null, edges: [],
      drift: { proseOnly: [], unresolvedTargets: [], missingFromProject: [], checklistEpics: [] },
    },
    isLoading: false,
  })
  renderPanel()
  await waitFor(() =>
    expect(screen.getByText(/no mirrored dependencies/i)).toBeInTheDocument())
})
