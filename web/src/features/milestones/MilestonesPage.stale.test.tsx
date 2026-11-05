// A concurrent advance (409 `stale_current`), end to end through the REAL hooks and
// query cache: the refetch moves the current milestone, which unmounts the card's
// dialog, and the page itself must still say what happened. Only `fetch` and the
// project read are stubbed; the milestones hooks are not mocked.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { MilestonesPage } from './MilestonesPage'
import { ladderView } from './__fixtures__/ladder'

vi.mock('@/api/hooks', async (orig) => ({
  ...(await orig<typeof import('@/api/hooks')>()),
  useProject: () => ({ data: { name: 'KinDash', repos: [] } }),
}))

const before = ladderView()
const after = ladderView({ currentKey: 'friendly' }, {
  milestones: before.milestones.map((m) =>
    m.key === 'household' ? { ...m, state: 'done' as const }
      : m.key === 'friendly' ? { ...m, state: 'current' as const } : m),
})

let gets = 0
const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
  const json = (status: number, body: unknown) => ({ status, ok: status < 400, json: async () => body })
  if (url === '/api/pm/projects/kindash/milestones' && (!init?.method || init.method === 'GET')) {
    gets += 1
    return json(200, gets === 1 ? before : after)
  }
  if (url === '/api/pm/projects/kindash/milestones/advance') {
    return json(409, { detail: { error: 'stale_current', current: 'friendly' } })
  }
  return json(200, {})
})

beforeEach(() => {
  gets = 0
  fetchMock.mockClear()
  vi.stubGlobal('fetch', fetchMock)
})
afterEach(() => vi.unstubAllGlobals())

describe('MilestonesPage — someone else advanced the ladder', () => {
  it('refetches, the dialog goes with the old current card, and the page alert stays', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/roadmap/kindash/milestones']}>
          <Routes><Route path="/roadmap/:productSlug/milestones" element={<MilestonesPage />} /></Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    )
    fireEvent.click(await screen.findByRole('button', { name: 'Advance with override…' }))
    const dialog = screen.getByRole('dialog', { name: 'Advance with override' })
    fireEvent.change(within(dialog).getByLabelText(/Reason/), { target: { value: 'owner accepted the risk' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Advance with override' }))

    // The 409 refetched the view: Phase 1 is now the current step.
    await waitFor(() => expect(gets).toBe(2))
    await waitFor(() =>
      expect(screen.getByRole('listitem', { name: /Phase 1/ })).toHaveAttribute('aria-current', 'step'))
    expect(screen.queryByRole('dialog')).toBeNull()
    const strip = screen.getByRole('region', { name: 'Where we are' })
    expect(within(strip).getByRole('alert')).toHaveTextContent('Someone else advanced this ladder')
  })
})
