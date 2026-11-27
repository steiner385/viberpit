import { describe, it, expect, afterEach, vi } from 'vitest'
import { api, ApiRequestError } from './client'
import { displayGateBlocks, setDisplayReads } from './displayGate'

afterEach(() => { setDisplayReads(null); vi.unstubAllGlobals() })

describe('display request gate (#3544)', () => {
  it('blocks nothing until a display allowlist is adopted', () => {
    expect(displayGateBlocks('/sessions')).toBe(false)
  })

  it('blocks a GET outside the list, query string ignored, and passes the listed ones', () => {
    setDisplayReads(['/api/pipeline/snapshot$', '/api/auth/me$'])
    expect(displayGateBlocks('/pipeline/snapshot?repo=a/b')).toBe(false)
    expect(displayGateBlocks('/sessions')).toBe(true)
    expect(displayGateBlocks('/pipeline/snapshot/extra')).toBe(true)
  })

  it('lifts when the session stops being a display', () => {
    setDisplayReads(['/api/auth/me$'])
    setDisplayReads(null)
    expect(displayGateBlocks('/sessions')).toBe(false)
  })

  it('api.get refuses a blocked path with a 403 and never calls fetch', async () => {
    const spy = vi.fn()
    vi.stubGlobal('fetch', spy)
    setDisplayReads(['/api/auth/me$'])
    const err = await api.get('/sessions').catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiRequestError)
    expect((err as ApiRequestError).status).toBe(403)
    expect(spy).not.toHaveBeenCalled()
  })

  it('does not gate a mutation (those are the server\'s 403 to give)', async () => {
    const spy = vi.fn(async () => new Response('{}', { status: 200 }))
    vi.stubGlobal('fetch', spy)
    setDisplayReads(['/api/auth/me$'])
    await api.post('/auth/logout')
    expect(spy).toHaveBeenCalledOnce()
  })
})
