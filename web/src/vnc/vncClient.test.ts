// web/src/vnc/vncClient.test.ts
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

type Handler = (e?: { detail?: { clean?: boolean } }) => void

/** A WebSocket as far as VncClient and noVNC touch it. `serverClose` behaves like
 *  a browser: listeners registered before `onclose` was set run first. */
class FakeSocket extends EventTarget {
  static last: FakeSocket
  onclose: ((e: CloseEvent) => void) | null = null
  readonly url: string
  constructor(url: string) {
    super()
    this.url = url
    FakeSocket.last = this
  }
  serverClose(code: number) {
    const e = new CloseEvent('close', { code, wasClean: true })
    this.dispatchEvent(e)
    this.onclose?.(e)
  }
}

let lastRfb: {
  scaleViewport: boolean
  channel: unknown
  addEventListener: (ev: string, cb: Handler) => void
  disconnect: () => void
  handlers: Record<string, Handler>
}
// Shaped like noVNC 1.7's RFB: it takes over the channel's `onclose` and reports a
// close as `disconnect` with ONLY `{clean}` — never the code (core/rfb.js).
vi.mock('@novnc/novnc', () => ({
  default: class {
    scaleViewport = false
    handlers: Record<string, Handler> = {}
    addEventListener = vi.fn((ev: string, cb: Handler) => { this.handlers[ev] = cb })
    disconnect = vi.fn()
    channel: unknown
    constructor(_target: Element, channel: FakeSocket) {
      this.channel = channel
      channel.onclose = () => this.handlers.disconnect?.({ detail: { clean: true } })
      // eslint-disable-next-line @typescript-eslint/no-this-alias
      lastRfb = this
    }
  },
}))

vi.stubGlobal('WebSocket', FakeSocket)

import { VncClient, classifyClose } from './vncClient'

describe('classifyClose', () => {
  it('maps distinct close codes to terminal states', () => {
    expect(classifyClose(4401)).toBe('disabled')
    expect(classifyClose(4404)).toBe('nodisplay')
  })
  it('treats other codes as reconnectable', () => {
    expect(classifyClose(1006)).toBe('reconnect')
  })
})

// `connect()` awaits a dynamic import of @novnc/novnc (it is the app's largest
// module and no longer sits in the entry chunk — see vncClient.ts), so every
// assertion about the RFB it builds has to await the connect first.
describe('VncClient fit', () => {
  it('enables scaleViewport (base fit) on connect', async () => {
    const c = new VncClient('ws://x')
    await c.connect(document.createElement('div'))
    expect(lastRfb.scaleViewport).toBe(true)
  })
  it('refit() leaves base fit enabled so noVNC recomputes scale', async () => {
    const c = new VncClient('ws://x')
    await c.connect(document.createElement('div'))
    lastRfb.scaleViewport = true
    c.refit()
    expect(lastRfb.scaleViewport).toBe(true)
  })
  it('never attaches a viewer after disconnect() raced the chunk load', async () => {
    const c = new VncClient('ws://x')
    const before = lastRfb
    const pending = c.connect(document.createElement('div'))
    c.disconnect()
    await pending
    expect(lastRfb).toBe(before) // no RFB was constructed for a torn-down client
  })
})

describe('VncClient.sendKey', () => {
  it('no-ops when no RFB is connected', () => {
    const c = new VncClient('ws://x/ws/vnc?session=s')
    // Never called connect(): rfb is null. Must not throw.
    expect(() => c.sendKey(0x41, null)).not.toThrow()
  })

  it('forwards keysym/code/down to the underlying rfb', () => {
    const c = new VncClient('ws://x/ws/vnc?session=s')
    const fake = { sendKey: vi.fn() }
    // Inject a fake RFB without opening a real socket.
    ;(c as unknown as { rfb: unknown }).rfb = fake
    c.sendKey(0xff0d, 'Enter', true)
    expect(fake.sendKey).toHaveBeenCalledWith(0xff0d, 'Enter', true)
  })
})

describe('VncClient reconnect', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('actually reconnects (builds a new RFB) after a reconnectable close, not just a label', async () => {
    const c = new VncClient('ws://x/ws/vnc?session=s')
    await c.connect(document.createElement('div'))
    const firstRfb = lastRfb
    FakeSocket.last.serverClose(1011)

    await vi.runAllTimersAsync()

    expect(lastRfb).not.toBe(firstRfb) // a fresh RFB was constructed — this is the actual reconnect
  })

  it('emits reconnecting immediately, then connect once the retry lands', async () => {
    const c = new VncClient('ws://x/ws/vnc?session=s')
    const states: string[] = []
    c.on('reconnecting', (s) => states.push(s))
    c.on('connect', (s) => states.push(s))
    await c.connect(document.createElement('div'))
    FakeSocket.last.serverClose(1011)
    expect(states).toEqual(['reconnecting'])

    await vi.runAllTimersAsync()
    lastRfb.handlers.connect()
    expect(states).toEqual(['reconnecting', 'connected'])
  })

  it('does not schedule a reconnect for terminal disabled/nodisplay closes', async () => {
    const c = new VncClient('ws://x/ws/vnc?session=s')
    await c.connect(document.createElement('div'))
    const firstRfb = lastRfb
    FakeSocket.last.serverClose(4404)

    await vi.runAllTimersAsync()

    expect(lastRfb).toBe(firstRfb) // no new RFB was built
  })

  it('cancels a pending reconnect on disconnect() so an unmounted tab never reconnects', async () => {
    const c = new VncClient('ws://x/ws/vnc?session=s')
    await c.connect(document.createElement('div'))
    const firstRfb = lastRfb
    FakeSocket.last.serverClose(1011)
    c.disconnect()

    await vi.runAllTimersAsync()

    expect(lastRfb).toBe(firstRfb) // no reconnect fired after teardown
  })
})

describe('VncClient close codes (noVNC reports only {clean})', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  async function connected() {
    const c = new VncClient('ws://x/ws/vnc?session=s')
    const states: string[] = []
    c.on('disconnect', (s) => states.push(s))
    c.on('reconnecting', (s) => states.push(s))
    await c.connect(document.createElement('div'))
    return { c, states }
  }

  it('hands noVNC its own socket for the session url', async () => {
    await connected()
    expect(lastRfb.channel).toBe(FakeSocket.last)
    expect(FakeSocket.last.url).toBe('ws://x/ws/vnc?session=s')
  })

  it('a 4404 close is nodisplay — no reconnect', async () => {
    const { states } = await connected()
    const first = lastRfb
    FakeSocket.last.serverClose(4404)
    await vi.runAllTimersAsync()
    expect(states).toEqual(['nodisplay'])
    expect(lastRfb).toBe(first)
  })

  it('a 4401 close is disabled', async () => {
    const { states } = await connected()
    FakeSocket.last.serverClose(4401)
    expect(states).toEqual(['disabled'])
  })

  it.each([1000, 1006, 1011])('any other close (%i) still reconnects', async (code) => {
    const { states } = await connected()
    const first = lastRfb
    FakeSocket.last.serverClose(code)
    expect(states).toEqual(['reconnecting'])
    await vi.runAllTimersAsync()
    expect(lastRfb).not.toBe(first)
  })

  it('a disconnect with no socket close (noVNC gave up) reconnects', async () => {
    const { states } = await connected()
    lastRfb.handlers.disconnect({ detail: { clean: false } })
    expect(states).toEqual(['reconnecting'])
  })
})
