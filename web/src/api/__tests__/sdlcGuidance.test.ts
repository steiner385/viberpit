import { describe, it, expect, vi } from 'vitest'

const get = vi.fn()
vi.mock('../client', () => ({
  api: { get: (...a: unknown[]) => get(...a) },
  apiErrorCode: () => null,
  ApiRequestError: class extends Error {},
}))

describe('sdlc guidance fetchers', () => {
  it('fetchGates reads the registry endpoint', async () => {
    const { fetchGates } = await import('../sdlc')
    get.mockResolvedValueOnce({ gates: [], tolerances: ['cautious'], autoresolveEnabled: true })
    const r = await fetchGates()
    expect(get).toHaveBeenCalledWith('/sdlc/gates')
    expect(r.tolerances).toEqual(['cautious'])
  })

  it('the query key for gates lives under sdlc', async () => {
    const { QK } = await import('../hooks')
    expect(QK.gates).toEqual(['sdlc', 'gates'])
  })
})
