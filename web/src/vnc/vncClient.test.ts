// web/src/vnc/vncClient.test.ts
import { describe, it, expect, vi } from 'vitest'

let lastRfb: { scaleViewport: boolean; addEventListener: () => void }
vi.mock('@novnc/novnc', () => ({
  default: class {
    scaleViewport = false
    addEventListener = vi.fn()
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
