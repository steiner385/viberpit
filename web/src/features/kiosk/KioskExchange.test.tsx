import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { KioskExchange } from './KioskExchange'

const TOKEN = 'ccas2:display-secret'

let replace: ReturnType<typeof vi.fn>

function stubFetch(res: Partial<Response>) {
  const fetchMock = vi.fn<typeof fetch>(async () => {
    // The token must be gone from the address bar before the request leaves.
    expect(window.location.search).toBe('')
    return { ok: true, status: 200, redirected: false, url: '', ...res } as Response
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

beforeEach(() => {
  window.history.replaceState(null, '', `/kiosk?token=${encodeURIComponent(TOKEN)}`)
  replace = vi.fn()
  vi.stubGlobal('location', { ...window.location, replace, get search() { return window.document.location.search } })
})

afterEach(() => {
  vi.unstubAllGlobals()
  window.history.replaceState(null, '', '/')
})

describe('KioskExchange (#1408 FR-G10.3)', () => {
  it('strips the token from the URL, then posts it as a form body', async () => {
    const fetchMock = stubFetch({ ok: true, status: 200, redirected: true, url: 'https://edge/pipeline?kiosk=1' })
    render(<KioskExchange />)
    await vi.waitFor(() => expect(replace).toHaveBeenCalledWith('https://edge/pipeline?kiosk=1'))
    expect(document.location.pathname + document.location.search).toBe('/kiosk')
    const [url, init] = fetchMock.mock.calls[0]!
    expect(url).toBe('/api/kiosk/session')
    expect(init?.method).toBe('POST')
    expect(String(init?.body)).toBe(new URLSearchParams({ token: TOKEN }).toString())
    expect(String(url)).not.toContain('token')
  })

  it('renders the expired message on 401 and does not navigate', async () => {
    stubFetch({ ok: false, status: 401 })
    render(<KioskExchange />)
    expect(await screen.findByText('This display link has expired or been revoked')).toBeInTheDocument()
    expect(replace).not.toHaveBeenCalled()
    expect(document.location.search).toBe('')
  })

  it('says the link is incomplete when it carries no token, without calling the server', async () => {
    window.history.replaceState(null, '', '/kiosk')
    const fetchMock = stubFetch({})
    render(<KioskExchange />)
    expect(await screen.findByText('This display link is incomplete')).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('reports an unreachable server distinctly from a refused link', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('network') }))
    render(<KioskExchange />)
    expect(await screen.findByText('Could not open the display')).toBeInTheDocument()
  })
})
