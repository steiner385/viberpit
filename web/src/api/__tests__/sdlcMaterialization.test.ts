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
import { fetchMaterialization } from '../sdlc'

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
