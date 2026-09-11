/**
 * `fetchMaterialization`'s 404 discrimination.
 *
 * The route answers 404 for two unrelated things — `no_proposal` (this
 * initiative has never been decomposed, which is the ordinary case and not a
 * failure) and `not_found` (the initiative does not exist, which is). Only the
 * first may resolve `null`; collapsing both would render a dead address as a
 * calm, empty Track tab.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { api, ApiRequestError } from '../client'
import { createdIssueCount, fetchMaterialization } from '../sdlc'

// Only `api` is replaced: `ApiRequestError` has to stay the REAL class, because
// what is under test is an `instanceof` check against it.
vi.mock('../client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../client')>()
  return { ...actual, api: { ...actual.api, get: vi.fn() } }
})

const get = vi.mocked(api.get)

describe('fetchMaterialization', () => {
  beforeEach(() => { get.mockReset() })

  it('asks the initiative-scoped route and hands the proposal back', async () => {
    get.mockResolvedValueOnce({ id: 'mp1', status: 'pending' })
    await expect(fetchMaterialization('a b/c')).resolves.toMatchObject({ id: 'mp1' })
    expect(get).toHaveBeenCalledWith('/sdlc/initiatives/a%20b%2Fc/materialization')
  })

  it('a 404 that says no_proposal is "nothing to decide", not an error', async () => {
    get.mockRejectedValueOnce(new ApiRequestError(404, {
      detail: { error: 'no_proposal', message: "initiative 'views' has no materialization proposal" },
    }))
    await expect(fetchMaterialization('views')).resolves.toBeNull()
  })

  it('a 404 for a missing initiative is still an error', async () => {
    const err = new ApiRequestError(404, { detail: { error: 'not_found' } })
    get.mockRejectedValueOnce(err)
    await expect(fetchMaterialization('ghost')).rejects.toBe(err)
  })

  it('a 404 with no code at all is still an error', async () => {
    const err = new ApiRequestError(404, {})
    get.mockRejectedValueOnce(err)
    await expect(fetchMaterialization('views')).rejects.toBe(err)
  })

  it('any other status is still an error', async () => {
    const err = new ApiRequestError(503, { detail: { error: 'no_proposal' } })
    get.mockRejectedValueOnce(err)
    await expect(fetchMaterialization('views')).rejects.toBe(err)
  })
})

/**
 * `createdIssueCount` reads the apply's resume state, so the card can say what a
 * re-tap will NOT create again. It counts ISSUES only: `repository_id` is a
 * remembered constant, and counting it would claim an issue that does not exist.
 */
describe('createdIssueCount', () => {
  it('is zero for a proposal nothing has been applied for', () => {
    expect(createdIssueCount(null)).toBe(0)
    expect(createdIssueCount(undefined)).toBe(0)
    expect(createdIssueCount({})).toBe(0)
  })

  it('does not count the remembered repository id', () => {
    expect(createdIssueCount({ repository_id: 'R_1' })).toBe(0)
  })

  it('counts the epic and every task the journal recorded', () => {
    expect(createdIssueCount({
      repository_id: 'R_1',
      epic: { node_id: 'I_1', number: 101 },
      tasks: { '0': { node_id: 'I_2', number: 102 }, '1': { node_id: 'I_3', number: 103 } },
    })).toBe(3)
  })

  it('counts the tasks of an apply that failed on the epic-less path', () => {
    // Defensive: the apply always creates the epic first, so this shape should
    // not occur — but a count that silently dropped the tasks would understate
    // what exists on GitHub, which is the one thing this number must not do.
    expect(createdIssueCount({ tasks: { '0': { node_id: 'I_2' } } })).toBe(1)
  })
})
