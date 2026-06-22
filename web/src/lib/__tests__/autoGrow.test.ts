import { describe, it, expect } from 'vitest'
import { autoGrow } from '../autoGrow'

describe('autoGrow', () => {
  it('is a no-op and does not throw when element is null', () => {
    expect(() => autoGrow(null)).not.toThrow()
  })

  it('sets the textarea height based on scrollHeight, capped at maxPx', () => {
    const el = document.createElement('textarea')
    Object.defineProperty(el, 'scrollHeight', { value: 500, configurable: true })
    autoGrow(el, 192)
    expect(el.style.height).toBe('192px')
  })

  it('uses scrollHeight when below the cap', () => {
    const el = document.createElement('textarea')
    Object.defineProperty(el, 'scrollHeight', { value: 60, configurable: true })
    autoGrow(el, 192)
    expect(el.style.height).toBe('60px')
  })
})
