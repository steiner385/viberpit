// web/src/vnc/vncClient.test.ts
import { describe, it, expect, vi } from 'vitest'

vi.mock('@novnc/novnc', () => ({ default: vi.fn() }))

import { classifyClose } from './vncClient'

describe('classifyClose', () => {
  it('maps distinct close codes to terminal states', () => {
    expect(classifyClose(4401)).toBe('disabled')
    expect(classifyClose(4404)).toBe('nodisplay')
  })
  it('treats other codes as reconnectable', () => {
    expect(classifyClose(1006)).toBe('reconnect')
  })
})
