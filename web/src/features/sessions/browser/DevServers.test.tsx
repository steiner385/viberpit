import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { DevServer } from '@/api/types'
import { DevServers } from './DevServers'
import { DEV_SERVERS_COPY } from './devServersCopy'

const SID = 'S'

function server(over: Partial<DevServer> = {}): DevServer {
  return {
    port: 5173, address: '127.0.0.1', bind: 'loopback', pid: 42, label: 'npm run dev',
    open: { kind: 'vnc' }, ...over,
  }
}

interface Reply { status: number; body: unknown }

/** A fetch stub: only the POST …/open is expected (the list is a prop). */
function stubFetch(open: Reply = { status: 200, body: { ok: true, port: 5173, url: 'x' } }) {
  const fn = vi.fn<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>(async (input) => {
    const url = String(input)
    if (!url.startsWith(`/api/sessions/${SID}/dev-servers/`)) throw new Error(`unexpected ${url}`)
    return new Response(JSON.stringify(open.body), { status: open.status, headers: { 'Content-Type': 'application/json' } })
  })
  vi.stubGlobal('fetch', fn)
  return fn
}

function renderList(servers: DevServer[], props: Partial<React.ComponentProps<typeof DevServers>> = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const onPreviewOpened = vi.fn()
  const refetch = vi.fn()
  const utils = render(
    <QueryClientProvider client={qc}>
      <DevServers sessionId={SID} servers={servers} refetch={refetch} notice={null}
                  onPreviewOpened={onPreviewOpened} {...props} />
    </QueryClientProvider>,
  )
  return { ...utils, onPreviewOpened, refetch }
}

let windowOpen: ReturnType<typeof vi.fn>
beforeEach(() => {
  windowOpen = vi.fn(() => null)
  vi.stubGlobal('open', windowOpen)
  stubFetch()
})
afterEach(() => vi.unstubAllGlobals())

describe('DevServers', () => {
  it('lists each server with its label, port, title and the bind note for an all-address bind', async () => {
    renderList([
      server({ title: 'Vite App' }),
      server({ port: 8080, bind: 'all', label: 'node vite --host', open: { kind: 'tailnet-link', url: 'http://100.1.2.3:8080/' } }),
    ])
    expect(await screen.findByText('npm run dev')).toBeInTheDocument()
    expect(screen.getByText(':5173')).toBeInTheDocument()
    expect(screen.getByText('Vite App')).toHaveAttribute('title', 'Vite App')
    expect(screen.getByText(':8080')).toBeInTheDocument()
    // The note is on the all-address server only.
    expect(screen.getAllByText(DEV_SERVERS_COPY.reachable)).toHaveLength(1)
    expect(screen.getByRole('heading', { name: 'Dev servers' })).toBeInTheDocument()
  })

  it('renders an untrusted label and title as text, never markup (Review Focus 5)', async () => {
    const evil = '<img src=x onerror="window.pwned=1">'
    const { container } = renderList([server({ label: evil, title: `<b>${evil}</b>` })])
    expect(await screen.findByText(evil)).toBeInTheDocument()
    expect(container.querySelector('img, b')).toBeNull()
  })

  it('truncates a very long title, keeping it in the title attribute', async () => {
    const long = 'T'.repeat(500)
    renderList([server({ title: long })])
    const el = await screen.findByTitle(/^T+…$/)
    expect(el.textContent!.length).toBeLessThan(long.length)
    expect(el.getAttribute('title')!.length).toBeLessThan(long.length)
  })

  it.each([
    ['local-link', 'http://127.0.0.1:5173/'],
    ['tailnet-link', 'http://100.101.102.103:5173/'],
  ] as const)('a %s Open opens exactly the server-provided URL in a new tab, without a POST', async (kind, url) => {
    const fetchFn = stubFetch()
    renderList([server({ open: { kind, url } })])
    fireEvent.click(await screen.findByRole('button', { name: /open :5173/i }))
    expect(windowOpen).toHaveBeenCalledWith(url, '_blank', 'noopener,noreferrer')
    expect(fetchFn.mock.calls.some(([, init]) => init?.method === 'POST')).toBe(false)
  })

  it('never opens a link URL that is not http(s)', async () => {
    renderList([server({ open: { kind: 'local-link', url: 'javascript:alert(1)' } })])
    fireEvent.click(await screen.findByRole('button', { name: /open :5173/i }))
    expect(windowOpen).not.toHaveBeenCalled()
    expect(await screen.findByRole('alert')).toHaveTextContent(DEV_SERVERS_COPY.badLink)
  })

  it('a vnc Open posts to the port and hands over to the viewer', async () => {
    const fetchFn = stubFetch()
    const { onPreviewOpened } = renderList([server()])
    fireEvent.click(await screen.findByRole('button', { name: /open :5173/i }))
    await waitFor(() => expect(onPreviewOpened).toHaveBeenCalledTimes(1))
    const post = fetchFn.mock.calls.find(([, init]) => init?.method === 'POST')
    expect(post?.[0]).toBe(`/api/sessions/${SID}/dev-servers/5173/open`)
    expect(windowOpen).not.toHaveBeenCalled()
  })

  it.each([
    ['PREVIEW_NO_DISPLAY', DEV_SERVERS_COPY.errors.PREVIEW_NO_DISPLAY],
    ['PREVIEW_SESSION_NOT_LIVE', DEV_SERVERS_COPY.errors.PREVIEW_SESSION_NOT_LIVE],
    ['PREVIEW_LAUNCH_FAILED', DEV_SERVERS_COPY.errors.PREVIEW_LAUNCH_FAILED],
    ['DEV_SERVER_NOT_DETECTED', DEV_SERVERS_COPY.errors.DEV_SERVER_NOT_DETECTED],
    ['SESSION_CONTROL_HUMAN_ONLY', DEV_SERVERS_COPY.errors.SESSION_CONTROL_HUMAN_ONLY],
    ['daemon_outdated', DEV_SERVERS_COPY.errors.daemon_outdated],
    ['DEV_SERVER_DETECT_DISABLED', DEV_SERVERS_COPY.errors.DEV_SERVER_DETECT_DISABLED],
  ])('a vnc Open refused with %s says so in plain words', async (code, copy) => {
    expect(copy).toBeTruthy()
    stubFetch({ status: 409, body: { detail: { code, message: 'x' } } })
    const { onPreviewOpened } = renderList([server()])
    fireEvent.click(await screen.findByRole('button', { name: /open :5173/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent(copy)
    expect(onPreviewOpened).not.toHaveBeenCalled()
  })

  it("a server the viewer has no way to open says so, with no Open button", async () => {
    // The session's host has no X11, and the viewer is not on that host: no route.
    renderList([
      server({ open: { kind: 'none', reason: 'host_no_display' } }),
      server({ port: 8080, bind: 'all', open: { kind: 'tailnet-link', url: 'http://100.1.2.3:8080/' } }),
    ])
    expect(await screen.findByText(DEV_SERVERS_COPY.unavailable)).toBeInTheDocument()
    expect(DEV_SERVERS_COPY.unavailable).toBe('Not available from this device')
    expect(screen.queryByRole('button', { name: /open :5173/i })).toBeNull()
    expect(screen.getByRole('button', { name: /open :8080/i })).toBeInTheDocument()
  })

  it('"no display" reads as no preview open', () => {
    expect(DEV_SERVERS_COPY.errors.PREVIEW_NO_DISPLAY).toMatch(/^No preview open/)
  })

  it('shows the empty state when nothing is detected', async () => {
    renderList([])
    expect(await screen.findByText(DEV_SERVERS_COPY.empty)).toBeInTheDocument()
  })

  it('shows a notice from the viewer (e.g. no preview open)', async () => {
    renderList([], { notice: 'No preview open' })
    expect(await screen.findByRole('status')).toHaveTextContent('No preview open')
  })


  it('an Open whose port is gone re-polls the list', async () => {
    stubFetch({ status: 404, body: { detail: { code: 'DEV_SERVER_NOT_DETECTED', message: 'x' } } })
    const { refetch } = renderList([server()])
    fireEvent.click(screen.getByRole('button', { name: /open :5173/i }))
    await waitFor(() => expect(refetch).toHaveBeenCalledTimes(1))
  })

  it("an unknown code shows the server's own message", async () => {
    stubFetch({ status: 409, body: { detail: { code: 'SOMETHING_NEW', message: 'The preview pool is draining.' } } })
    renderList([server()])
    fireEvent.click(screen.getByRole('button', { name: /open :5173/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent('The preview pool is draining.')
  })

  it('an error with neither a known code nor a message is the generic copy, not "couldn\'t reach"', async () => {
    stubFetch({ status: 500, body: { detail: 'boom' } })
    renderList([server()])
    fireEvent.click(screen.getByRole('button', { name: /open :5173/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent(DEV_SERVERS_COPY.failed)
  })

  it('only a request that never got an answer says the server could not be reached', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Failed to fetch') }))
    renderList([server()])
    fireEvent.click(screen.getByRole('button', { name: /open :5173/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent(DEV_SERVERS_COPY.unreachable)
  })

  it('one Open at a time: the other rows wait while one is in flight, so no error is lost', async () => {
    let answer: (r: Response) => void = () => {}
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>((r) => { answer = r })))
    renderList([server(), server({ port: 5174, label: 'storybook' })])
    fireEvent.click(screen.getByRole('button', { name: /open :5173/i }))
    const other = screen.getByRole('button', { name: /open :5174/i })
    await waitFor(() => expect(other).toBeDisabled())
    answer(new Response(JSON.stringify({ detail: { code: 'PREVIEW_LAUNCH_FAILED', message: 'x' } }), { status: 409 }))
    expect(await screen.findByRole('alert')).toHaveTextContent(DEV_SERVERS_COPY.errors.PREVIEW_LAUNCH_FAILED)
    expect(other).toBeEnabled()
  })
})
