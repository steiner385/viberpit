// The client's two passkey-era duties (#1410 FR-G12.12): a 401 that names the
// passkey login sends the SPA to `/login` once, and a 429 carries Retry-After.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

type ClientModule = typeof import('../client')

const replace = vi.fn()

function respond(status: number, body: unknown, headers: Record<string, string> = {}) {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(body), {
    status, headers: { 'Content-Type': 'application/json', ...headers },
  })))
}

const PASSKEY_401 = { error: { code: 'UNAUTHORIZED', message: 'No valid identity', login: 'passkey' } }

async function freshClient(): Promise<ClientModule> {
  // The redirect latch is module state, so each test gets its own module.
  vi.resetModules()
  return import('../client')
}

beforeEach(() => {
  replace.mockReset()
  vi.stubGlobal('location', { ...window.location, pathname: '/sessions/s1', search: '?x=1', hash: '', replace })
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('api client — 401 with a passkey login', () => {
  it('replaces the location with /login?next=<current path> and still rejects', async () => {
    const { api, ApiRequestError } = await freshClient()
    respond(401, PASSKEY_401)
    await expect(api.get('/sessions')).rejects.toBeInstanceOf(ApiRequestError)
    expect(replace).toHaveBeenCalledExactlyOnceWith('/login?next=%2Fsessions%2Fs1%3Fx%3D1')
  })

  it('redirects once however many requests fail together', async () => {
    const { api } = await freshClient()
    respond(401, PASSKEY_401)
    await Promise.allSettled([api.get('/a'), api.get('/b'), api.get('/c')])
    expect(replace).toHaveBeenCalledTimes(1)
  })

  it('never redirects while already on /login', async () => {
    vi.stubGlobal('location', { ...window.location, pathname: '/login', search: '', hash: '', replace })
    const { api } = await freshClient()
    respond(401, PASSKEY_401)
    await expect(api.get('/auth/me')).rejects.toThrow()
    expect(replace).not.toHaveBeenCalled()
  })

  it('leaves a 401 without `login: "passkey"` alone (today’s behaviour)', async () => {
    const { api } = await freshClient()
    respond(401, { error: { code: 'UNAUTHORIZED', message: 'No valid identity' } })
    await expect(api.get('/sessions')).rejects.toThrow()
    expect(replace).not.toHaveBeenCalled()
  })

  it('does not redirect on a 403 that happens to carry the field', async () => {
    const { api } = await freshClient()
    respond(403, PASSKEY_401)
    await expect(api.get('/sessions')).rejects.toThrow()
    expect(replace).not.toHaveBeenCalled()
  })
})

describe('api client — Retry-After', () => {
  it('exposes Retry-After seconds on a 429', async () => {
    const { api, ApiRequestError } = await freshClient()
    respond(429, { error: { code: 'RATE_LIMITED' } }, { 'Retry-After': '30' })
    const err = await api.post('/auth/webauthn/login/begin', {}).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiRequestError)
    expect((err as InstanceType<typeof ApiRequestError>).retryAfterS).toBe(30)
  })

  it('is null when the header is absent or not a number of seconds', async () => {
    const { api } = await freshClient()
    respond(429, {}, { 'Retry-After': 'Wed, 21 Oct 2015 07:28:00 GMT' })
    const err = await api.get('/x').catch((e: unknown) => e)
    expect(err).toMatchObject({ status: 429, retryAfterS: null })
  })
})
