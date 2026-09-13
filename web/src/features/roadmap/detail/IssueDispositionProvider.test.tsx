import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import type { Leaf } from '@/api/roadmap'
import { QK } from '@/api/hooks'
import { cap, product } from '../__fixtures__/map'
import { NeedsYouList } from './NeedsYouList'
import { IssueDispositionProvider } from './IssueDispositionProvider'

vi.mock('@/initiatives/IncidentQueue', () => ({ IncidentQueue: () => null }))
const leaf: Leaf = {
  id: 'w1', ref: 'o/r#1', repo: 'o/r', number: 1, kind: 'issue', title: 'Account warning', url: null, state: 'open',
  triageState: null, score: null, reach: null, impact: null, effort: null, confidence: null, tier: 'needs-you',
  blockedBy: [], parentRef: null, sessionStatus: null, ghCreatedAt: null, featureId: null, reason: null, bounces: 0,
}
const issue = { id: 'w1', repo: 'o/r', number: 1, title: 'Account warning', body: 'Original description', state: 'open', url: null, version: 'v1' }
const reply = (data: unknown, status = 200) => ({ ok: status < 400, status, json: async () => data })
afterEach(() => new Promise((resolve) => setTimeout(resolve, 200)))

describe('page-owned issue review', () => {
  it.each(['leaf', 'group'])('retains a draft and pending failure when polling removes the last attention %s', async (removed) => {
    let finishWrite!: (value: ReturnType<typeof reply>) => void
    const write = new Promise<ReturnType<typeof reply>>((resolve) => { finishWrite = resolve })
    const fetchMock = vi.fn((path: string, init: RequestInit) => {
      if (init.method === 'POST') return write
      return Promise.resolve(reply(path.includes('/leaves?') ? { items: [leaf] } : issue))
    })
    vi.stubGlobal('fetch', fetchMock)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
    const page = (count: number) => {
      const capability = cap({ slug: 'accounts', counts: { openIssues: count, needsYou: count, blocked: 0, inFlight: 0 } })
      return <QueryClientProvider client={qc}><MemoryRouter><IssueDispositionProvider>
        <div data-page-scroll=""><NeedsYouList products={[product({ slug: 'p', capabilities: [capability] })]} capability={capability} /></div>
      </IssueDispositionProvider></MemoryRouter></QueryClientProvider>
    }
    const { rerender } = render(page(1))
    fireEvent.click(await screen.findByRole('button', { name: 'Review and decide' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Edit details' }))
    fireEvent.change(screen.getByLabelText('Description'), { target: { value: 'My unsaved requirements' } })
    if (removed === 'leaf') {
      act(() => { qc.setQueryData(QK.leaves('capability:f-accounts'), []) })
    } else rerender(page(0))
    expect(screen.getByLabelText('Description')).toHaveValue('My unsaved requirements')
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => init.method === 'POST')).toBe(true))
    // A further map update during the write must not remove its eventual error.
    rerender(page(0))
    expect(screen.getByRole('button', { name: 'Close' })).toBeDisabled()
    await act(async () => { finishWrite(reply({ detail: 'GitHub unavailable' }, 502)); await write })
    expect(await screen.findByRole('alert')).toHaveTextContent('GitHub unavailable')
    expect(screen.getByLabelText('Description')).toHaveValue('My unsaved requirements')
  })
})
