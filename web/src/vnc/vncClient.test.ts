// web/src/vnc/vncClient.test.ts
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

type Handler = (e?: { detail?: { clean?: boolean; code?: number } }) => void

let lastRfb: {
  scaleViewport: boolean
  addEventListener: (ev: string, cb: Handler) => void
  disconnect: () => void
  handlers: Record<string, Handler>
}
vi.mock('@novnc/novnc', () => ({
  default: class {
    scaleViewport = false
    handlers: Record<string, Handler> = {}
    addEventListener = vi.fn((ev: string, cb: Handler) => { this.handlers[ev] = cb })
    disconnect = vi.fn()
    // eslint-disable-next-line @typescript-eslint/no-this-alias
    constructor() { lastRfb = this }
  },
}))

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

describe('VncClient fit', () => {
  it('enables scaleViewport (base fit) on connect', () => {
    const c = new VncClient('ws://x')
    c.connect(document.createElement('div'))
    expect(lastRfb.scaleViewport).toBe(true)
  })
  it('refit() leaves base fit enabled so noVNC recomputes scale', () => {
    const c = new VncClient('ws://x')
    c.connect(document.createElement('div'))
    lastRfb.scaleViewport = true
    c.refit()
    expect(lastRfb.scaleViewport).toBe(true)
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

  it('actually reconnects (builds a new RFB) after a reconnectable close, not just a label', () => {
    const c = new VncClient('ws://x/ws/vnc?session=s')
    c.connect(document.createElement('div'))
    const firstRfb = lastRfb
    firstRfb.handlers.disconnect({ detail: { clean: true, code: 1011 } })

    vi.runAllTimers()

    expect(lastRfb).not.toBe(firstRfb) // a fresh RFB was constructed — this is the actual reconnect
  })

  it('emits reconnecting immediately, then connect once the retry lands', () => {
    const c = new VncClient('ws://x/ws/vnc?session=s')
    const states: string[] = []
    c.on('reconnecting', (s) => states.push(s))
    c.on('connect', (s) => states.push(s))
    c.connect(document.createElement('div'))
    lastRfb.handlers.disconnect({ detail: { clean: true, code: 1011 } })
    expect(states).toEqual(['reconnecting'])

    vi.runAllTimers()
    lastRfb.handlers.connect()
    expect(states).toEqual(['reconnecting', 'connected'])
  })

  it('does not schedule a reconnect for terminal disabled/nodisplay closes', () => {
    const c = new VncClient('ws://x/ws/vnc?session=s')
    c.connect(document.createElement('div'))
    const firstRfb = lastRfb
    firstRfb.handlers.disconnect({ detail: { clean: true, code: 4404 } })

    vi.runAllTimers()

    expect(lastRfb).toBe(firstRfb) // no new RFB was built
  })

  it('cancels a pending reconnect on disconnect() so an unmounted tab never reconnects', () => {
    const c = new VncClient('ws://x/ws/vnc?session=s')
    c.connect(document.createElement('div'))
    const firstRfb = lastRfb
    firstRfb.handlers.disconnect({ detail: { clean: true, code: 1011 } })
    c.disconnect()

    vi.runAllTimers()

    expect(lastRfb).toBe(firstRfb) // no reconnect fired after teardown
  })
})
