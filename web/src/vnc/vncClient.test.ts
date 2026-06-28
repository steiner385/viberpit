// web/src/vnc/vncClient.test.ts
import { describe, it, expect, vi } from 'vitest'

let lastRfb: { scaleViewport: boolean; addEventListener: () => void }
vi.mock('@novnc/novnc', () => ({
  default: class {
    scaleViewport = false
    addEventListener = vi.fn()
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
