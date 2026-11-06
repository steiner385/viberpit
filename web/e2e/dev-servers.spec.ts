/**
 * Dev servers in the Browser tab (D-18). The session's detected dev servers are
 * listed with an Open that goes to the VIEWER's display by the route the server
 * picked: a link opens exactly the server's URL in a new tab, and a `vnc` Open
 * posts the port and switches the tab to the session's display. The tab is listed
 * for a session with a dev server even when the edge has no X11. Neither width
 * scrolls sideways. The API is mocked with `page.route`, like the other session specs.
 */
import { test, expect, type Page } from '@playwright/test'
import { replayFrame, wire } from './fixtures/wsFrames'

const sid = 'dev-servers'

const SERVERS = [
  {
    port: 5173, address: '127.0.0.1', bind: 'loopback', pid: 4242,
    label: 'npm run dev -- --config vite.config.with.a.rather.long.name.ts --strictPort',
    title: '<img src=x onerror="window.__pwned=1"> Vite App — a page title long enough to need truncating on a phone',
    open: { kind: 'local-link', url: 'http://127.0.0.1:5173/' },
  },
  {
    port: 8080, address: '127.0.0.1', bind: 'all', pid: 4300, label: 'node vite --host',
    note: 'Also reachable from your network',
    open: { kind: 'vnc' },
  },
]

interface Opts {
  x11: boolean
  servers?: unknown[]
  openReply?: { status: number; json: unknown }
  /** Close the preview stream with this code once it opens (default: hold it open). */
  vncClose?: number
}

async function openSession(page: Page, width: number, { x11, servers = SERVERS, openReply, vncClose }: Opts) {
  await page.setViewportSize({ width, height: 900 })
  // The preview stream (`/ws/vnc`) is a stand-in socket the REAL noVNC accepts:
  // it opens, sends nothing, and — with `vncClose` — is closed by the "server" as
  // the daemon closes a preview display that is gone (4404). Not `routeWebSocket`:
  // noVNC refuses Playwright's routed socket ("Raw channel missing property").
  // Every other socket goes to the original constructor.
  await page.addInitScript((closeCode) => {
    // Playwright's own socket routing (for `/ws`) installs its constructor after
    // this script; keep ours outermost and route everything else to whichever
    // constructor was installed last.
    let Native = window.WebSocket
    class StandIn extends EventTarget {
      binaryType = 'blob'; protocol = ''; readyState = 0; url: string
      onopen: ((e: Event) => void) | null = null
      onmessage: ((e: Event) => void) | null = null
      onerror: ((e: Event) => void) | null = null
      onclose: ((e: CloseEvent) => void) | null = null
      send = () => {}
      close = () => { this.finish(1000) }
      constructor(url: string) {
        super()
        this.url = url
        setTimeout(() => {
          this.readyState = 1
          const e = new Event('open')
          this.dispatchEvent(e)
          this.onopen?.(e)
          if (closeCode) setTimeout(() => this.finish(closeCode), 200)
        }, 50)
      }
      finish(code: number) {
        if (this.readyState === 3) return
        this.readyState = 3
        const e = new CloseEvent('close', { code, reason: 'no display', wasClean: true })
        this.dispatchEvent(e)
        this.onclose?.(e)
      }
    }
    const Patched = function (url: string | URL, protocols?: string | string[]) {
      return String(url).includes('/ws/vnc') ? new StandIn(String(url)) : new Native(url, protocols)
    } as unknown as typeof WebSocket
    Object.assign(Patched, { CONNECTING: 0, OPEN: 1, CLOSING: 2, CLOSED: 3, prototype: Native.prototype })
    Object.defineProperty(window, 'WebSocket', {
      configurable: true,
      get: () => Patched,
      set: (v: typeof WebSocket) => { Native = v },
    })
  }, vncClose ?? 0)
  // `window.open` is recorded, never followed.
  await page.addInitScript(() => {
    const w = window as unknown as { __opened: unknown[][] }
    w.__opened = []
    window.open = (...args: unknown[]) => { w.__opened.push(args); return null }
  })
  await page.route('**/api/**', (route) => route.fulfill({ json: [] }))
  await page.route('**/api/sessions', (route) => route.fulfill({ json: [{
    id: sid, name: 'Dev servers', accountName: 'alice', engine: 'claude_code', status: 'idle',
    displayMode: 'native', host: 'dobby',
    createdAt: '2026-11-05T10:00:00Z', updatedAt: '2026-11-05T10:00:00Z',
  }] }))
  await page.route('**/api/config', (route) => route.fulfill({ json: { repoRoot: '', adoptionEnabled: false } }))
  await page.route('**/api/daemon/health', (route) => route.fulfill({ json: { status: 'ok' } }))
  await page.route('**/api/x11/metrics', (route) => route.fulfill({ json: x11 ? { active_displays: 0 } : { enabled: false } }))
  await page.route(`**/api/sessions/${sid}/diff**`, (route) => route.fulfill({ status: 404, json: { detail: 'no worktree' } }))
  const posts: string[] = []
  await page.route(`**/api/sessions/${sid}/dev-servers**`, (route) => {
    const req = route.request()
    if (req.method() === 'POST') {
      posts.push(new URL(req.url()).pathname)
      return route.fulfill(openReply ?? { json: { ok: true, port: 8080, url: 'http://127.0.0.1:8080/' } })
    }
    return route.fulfill({ json: { host: 'dobby', servers } })
  })
  await page.routeWebSocket('**/ws', (ws) => ws.onMessage((msg) => {
    const frame = JSON.parse(msg.toString()) as { type: string }
    if (frame.type === 'attach') ws.send(wire(replayFrame(sid, [], { totalTurns: 0 })))
  }))
  await page.goto('/')
  await page.getByRole('button', { name: /open session dev servers/i }).click()
  return posts
}

async function noOverflow(page: Page) {
  const panel = page.locator('#tab-panel-browser')
  const p = await panel.evaluate((el) => ({ scrollWidth: el.scrollWidth, clientWidth: el.clientWidth }))
  expect(p.scrollWidth).toBeLessThanOrEqual(p.clientWidth)
  const doc = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth,
  }))
  expect(doc.scrollWidth).toBeLessThanOrEqual(doc.clientWidth)
}

const section = (page: Page) => page.getByRole('region', { name: 'Dev servers' })

for (const width of [1440, 390]) {
  test(`X11 off: the tab is listed for the dev servers, links open as given, vnc opens the viewer at ${width}px`, async ({ page }) => {
    const posts = await openSession(page, width, { x11: false })
    await page.getByRole('tab', { name: 'Browser' }).click()
    await expect(page).toHaveURL(/view=browser/)

    const list = section(page)
    await expect(list.getByText(':5173')).toBeVisible()
    await expect(list.getByText(':8080')).toBeVisible()
    await expect(list.getByText('Also reachable from your network')).toHaveCount(1)
    // The untrusted title is text: the markup shows, nothing runs.
    await expect(list.getByText(/<img src=x/)).toBeVisible()
    expect(await page.evaluate(() => (window as unknown as { __pwned?: number }).__pwned)).toBeUndefined()
    // Nothing streams before an Open.
    await expect(page.getByLabel('Live browser view')).toHaveCount(0)
    await noOverflow(page)
    await page.screenshot({ path: `/tmp/d18-${width}.png` })

    await list.getByRole('button', { name: 'Open :5173 in a new tab' }).click()
    const opened = await page.evaluate(() => (window as unknown as { __opened: unknown[][] }).__opened)
    expect(opened).toEqual([['http://127.0.0.1:5173/', '_blank', 'noopener,noreferrer']])
    expect(posts).toEqual([])

    await list.getByRole('button', { name: "Open :8080 in the session's browser" }).click()
    await expect(page.getByLabel('Live browser view')).toBeAttached()
    expect(posts).toEqual([`/api/sessions/${sid}/dev-servers/8080/open`])
    await noOverflow(page)
  })
}

test('X11 on, nothing detected: the empty state sits above the native notice', async ({ page }) => {
  await openSession(page, 1440, { x11: true, servers: [] })
  await page.getByRole('tab', { name: 'Browser' }).click()
  await expect(section(page).getByText(
    'No dev servers detected — when the session starts one (e.g. npm run dev), it shows up here.')).toBeVisible()
  await expect(page.getByText(/opens windows on dobby's desktop/i)).toBeVisible()
})

test('a refused vnc Open says why in plain words and stays on the list', async ({ page }) => {
  await openSession(page, 390, {
    x11: true,
    openReply: { status: 409, json: { detail: { code: 'PREVIEW_NO_DISPLAY', message: 'x11 disabled' } } },
  })
  await page.getByRole('tab', { name: 'Browser' }).click()
  await section(page).getByRole('button', { name: "Open :8080 in the session's browser" }).click()
  await expect(section(page).getByRole('alert')).toHaveText(/^No preview open/)
  await expect(page.getByLabel('Live browser view')).toHaveCount(0)
})

test('a native preview whose display is gone (4404) reads "No preview open" — real noVNC, not a reconnect loop', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(e.message))
  await openSession(page, 1440, { x11: true, vncClose: 4404 })
  await page.getByRole('tab', { name: 'Browser' }).click()
  await section(page).getByRole('button', { name: "Open :8080 in the session's browser" }).click()
  await expect(section(page).getByRole('status')).toHaveText('No preview open')
  await expect(page.getByLabel('Live browser view')).toHaveCount(0)
  await expect(page.getByText('Reconnecting…')).toHaveCount(0)
  await expect(page.getByText(/opens windows on dobby's desktop/i)).toBeVisible()
  // noVNC took the socket and failed cleanly — no exception (e.g. a refused channel).
  expect(errors).toEqual([])
})

