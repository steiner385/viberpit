// The milestone write hooks (spec 2026-11-05 §10): every write answers with the
// full view, which REPLACES the cached one (no refetch); a 409 `stale_current`
// is the one failure that refetches, since the ladder moved under the caller.
// Every error code `server/pm/milestone_actions.py` (and the write wrapper in
// `server/api/pm_milestones.py`) can raise has its own plain-English copy.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import {
  MILESTONE_ERROR_FALLBACK, milestoneErrorCopy, milestonesKey, useAddNote, useAdvanceMilestone, useSetStatusNote,
  useTickCondition, useUpdateNote,
} from './milestones'
import { ladderView } from '@/features/milestones/__fixtures__/ladder'

/** Every code the server raises — `grep MilestoneActionError server/pm/milestone_actions.py`
 *  plus `no_ladder` from the route wrapper. A new server code fails this list's test. */
const SERVER_CODES = [
  'person_required', 'person_or_session_required', 'unknown_milestone', 'unknown_condition', 'not_manual', 'bad_tick',
  'evidence_required', 'owner_required', 'too_long', 'text_required', 'bad_status', 'bad_kind',
  'unknown_note', 'nothing_to_update', 'ladder_complete', 'stale_current', 'no_milestones',
  'override_not_needed', 'gate_unmet', 'reason_too_short', 'override_requires_owner', 'no_ladder', 'bad_url',
  'concurrent_update',
]

function setup() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  return { qc, wrapper }
}

const fetchMock = vi.fn()
function respond(status: number, body: unknown) {
  fetchMock.mockResolvedValueOnce({ status, ok: status < 400, json: async () => body })
}

beforeEach(() => {
  fetchMock.mockReset()
  vi.stubGlobal('fetch', fetchMock)
})
afterEach(() => vi.unstubAllGlobals())

describe('milestoneErrorCopy', () => {
  it.each(SERVER_CODES)('has its own copy for %s', (code) => {
    const copy = milestoneErrorCopy(code)
    expect(copy).not.toBe(MILESTONE_ERROR_FALLBACK)
    expect(copy.length).toBeGreaterThan(10)
  })

  it('gives the brief copy for the owner and stale answers', () => {
    expect(milestoneErrorCopy('override_requires_owner')).toBe('Only the owner can override from the ViberPit app')
    expect(milestoneErrorCopy('stale_current')).toBe('Someone else advanced this ladder')
    expect(milestoneErrorCopy('concurrent_update')).toBe('Someone else just changed this. Reload and try again.')
  })

  it('falls back for an unknown or missing code', () => {
    expect(milestoneErrorCopy('nope')).toBe(MILESTONE_ERROR_FALLBACK)
    expect(milestoneErrorCopy(null)).toBe(MILESTONE_ERROR_FALLBACK)
  })
})

describe('milestone write hooks', () => {
  it('a tick PUTs to the condition and replaces the cached view without a refetch', async () => {
    const { qc, wrapper } = setup()
    qc.setQueryData(milestonesKey('kindash'), ladderView())
    const next = ladderView({ updatedAt: '2026-11-05T00:00:00Z' })
    respond(200, next)
    const { result } = renderHook(() => useTickCondition('kindash'), { wrapper })
    await act(() => result.current.mutateAsync({ scope: 'household', conditionKey: 'setup', met: true, evidence: 'call notes' }))
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/pm/projects/kindash/milestones/household/conditions/setup')
    expect(init.method).toBe('PUT')
    expect(JSON.parse(init.body)).toEqual({ met: true, evidence: 'call notes' })
    expect(qc.getQueryData(milestonesKey('kindash'))).toEqual(next)
  })

  it('the status line, notes and advance hit their routes and set the view', async () => {
    const { qc, wrapper } = setup()
    const view = ladderView()
    for (let i = 0; i < 4; i++) respond(200, view)
    const status = renderHook(() => useSetStatusNote('kindash'), { wrapper }).result
    const add = renderHook(() => useAddNote('kindash'), { wrapper }).result
    const update = renderHook(() => useUpdateNote('kindash'), { wrapper }).result
    const advance = renderHook(() => useAdvanceMilestone('kindash'), { wrapper }).result
    await act(() => status.current.mutateAsync({ key: 'household', statusNote: 'on track' }))
    await act(() => add.current.mutateAsync({ kind: 'ask', text: 'Name the families' }))
    await act(() => update.current.mutateAsync({ id: 'n1', status: 'done' }))
    await act(() => advance.current.mutateAsync({ expectedCurrent: 'household', overrideReason: 'accepted the risk' }))
    const calls = fetchMock.mock.calls.map(([u, i]) => [i.method, u, JSON.parse(i.body)])
    expect(calls).toEqual([
      ['PATCH', '/api/pm/projects/kindash/milestones/household', { statusNote: 'on track' }],
      ['POST', '/api/pm/projects/kindash/milestones/notes', { kind: 'ask', text: 'Name the families' }],
      ['PATCH', '/api/pm/projects/kindash/milestones/notes/n1', { status: 'done' }],
      ['POST', '/api/pm/projects/kindash/milestones/advance', { expectedCurrent: 'household', overrideReason: 'accepted the risk' }],
    ])
    expect(qc.getQueryData(milestonesKey('kindash'))).toEqual(view)
  })

  it('a 409 stale_current refetches the view; other errors leave it alone', async () => {
    const { qc, wrapper } = setup()
    qc.setQueryData(milestonesKey('kindash'), ladderView())
    const spy = vi.spyOn(qc, 'invalidateQueries')
    respond(409, { detail: { error: 'stale_current', current: 'friendly' } })
    const { result } = renderHook(() => useAdvanceMilestone('kindash'), { wrapper })
    await act(async () => { await result.current.mutateAsync({ expectedCurrent: 'household' }).catch(() => {}) })
    await waitFor(() => expect(spy).toHaveBeenCalledWith({ queryKey: milestonesKey('kindash') }))
    spy.mockClear()
    respond(403, { detail: { error: 'override_requires_owner' } })
    await act(async () => { await result.current.mutateAsync({ expectedCurrent: 'household', overrideReason: 'x'.repeat(12) }).catch(() => {}) })
    expect(spy).not.toHaveBeenCalledWith({ queryKey: milestonesKey('kindash') })
  })
})
