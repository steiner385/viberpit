/**
 * The inbox's two fetchers and the tag they stamp.
 *
 * The server sends NO discriminator between the two card kinds: `/pm/triage`'s
 * `kind` means the work item's kind (issue/pr), and `/pm/decisions` has no such
 * field at all. So `proposalKind` is stamped HERE, at the one fetcher per list,
 * and every consumer downstream — the ordering spec, the card, the pane, the key
 * map — reads that tag rather than guessing a kind from which fields are present.
 * If these two ever stop tagging, the inbox renders decisions as classifications.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('@/api/client', () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), del: vi.fn() },
}))

import { api } from '@/api/client'
import {
  acceptDecision, fetchDecisions, fetchTriage, isDecisionProposal, isTriageProposal,
  rejectDecision,
} from '@/api/triage'

const get = vi.mocked(api.get)
const post = vi.mocked(api.post)

beforeEach(() => vi.clearAllMocks())

describe('inbox fetchers', () => {
  it('tags every classification row, scoped or not', async () => {
    get.mockResolvedValue([{ id: 'p1' }, { id: 'p2' }])
    const rows = await fetchTriage()
    expect(get).toHaveBeenCalledWith('/pm/triage')
    expect(rows.map((r) => r.proposalKind)).toEqual(['triage', 'triage'])
    expect(rows.every(isTriageProposal)).toBe(true)

    await fetchTriage('kin dash')
    expect(get).toHaveBeenLastCalledWith('/pm/triage?projectSlug=kin%20dash')
  })

  it('tags every decision row, from the one unscoped list', async () => {
    get.mockResolvedValue([{ id: 'd1', options: [] }])
    const rows = await fetchDecisions()
    // No `projectSlug` query: the endpoint takes none, and the `?product=` chip
    // narrows this list client-side.
    expect(get).toHaveBeenCalledWith('/pm/decisions')
    expect(rows.every(isDecisionProposal)).toBe(true)
  })

  it('answers empty lists without inventing rows', async () => {
    get.mockResolvedValue([])
    expect(await fetchTriage()).toEqual([])
    expect(await fetchDecisions()).toEqual([])
  })

  it('sends the chosen option under the key the server reads', async () => {
    post.mockResolvedValue({ id: 'd1', status: 'accepted', chosenKey: 'b' })
    await acceptDecision('d 1', 'b')
    expect(post).toHaveBeenCalledWith('/pm/decisions/d%201/accept', { chosen_key: 'b' })

    await rejectDecision('d 1')
    expect(post).toHaveBeenLastCalledWith('/pm/decisions/d%201/reject')
  })
})
