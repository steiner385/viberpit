import { describe, it, expect, afterEach } from 'vitest'
import { render } from '@testing-library/react'
import {
  computeToastInsets,
  flushToastInsets,
  registerToastAnchor,
  useToastAnchor,
  TOAST_INSET_VARS,
} from './toastInsets'

function rect(top: number, left: number, right: number, bottom: number): DOMRect {
  return { top, left, right, bottom, width: right - left, height: bottom - top, x: left, y: top, toJSON: () => ({}) }
}

function boxed(r: DOMRect): HTMLElement {
  const el = document.createElement('div')
  el.getBoundingClientRect = () => r
  document.body.appendChild(el)
  return el
}

function vars(): Record<'top' | 'left' | 'right', string> {
  const s = document.documentElement.style
  return {
    top: s.getPropertyValue(TOAST_INSET_VARS.top),
    left: s.getPropertyValue(TOAST_INSET_VARS.left),
    right: s.getPropertyValue(TOAST_INSET_VARS.right),
  }
}

describe('computeToastInsets', () => {
  it('is null without a band — the toaster falls back to the viewport', () => {
    expect(computeToastInsets(null, [], 1000)).toBeNull()
  })

  it('bounds the toast by the band: below its top, inside its left and right edges', () => {
    expect(computeToastInsets(rect(40, 64, 900, 700), [], 1000)).toEqual({ top: 40, left: 64, right: 100 })
  })

  it('drops the toast below the tallest registered header', () => {
    const band = rect(40, 0, 1000, 700)
    const listHead = rect(40, 0, 300, 88)
    const sessionHead = rect(40, 300, 1000, 96)
    expect(computeToastInsets(band, [listHead, sessionHead], 1000)?.top).toBe(96)
  })

  it('ignores a header that is not laid out (hidden page)', () => {
    const band = rect(40, 0, 1000, 700)
    expect(computeToastInsets(band, [rect(0, 0, 0, 0)], 1000)?.top).toBe(40)
  })

  it('never reports a negative inset', () => {
    expect(computeToastInsets(rect(-10, -5, 1200, 700), [], 1000)).toEqual({ top: 0, left: 0, right: 0 })
  })
})

describe('toast inset registry', () => {
  const cleanups: Array<() => void> = []
  afterEach(() => {
    cleanups.splice(0).forEach((c) => c())
    flushToastInsets()
    document.body.replaceChildren()
  })

  it('publishes the band as CSS variables on :root', () => {
    // jsdom's viewport is 1024px wide; keep the band inside it so `right` is real.
    cleanups.push(registerToastAnchor(boxed(rect(48, 72, 900, 700)), 'band'))
    flushToastInsets()
    expect(vars()).toEqual({ top: '48px', left: '72px', right: `${window.innerWidth - 900}px` })
  })

  it('raises the top past a registered header, and lowers it again when the header leaves', () => {
    cleanups.push(registerToastAnchor(boxed(rect(48, 0, 800, 700)), 'band'))
    const off = registerToastAnchor(boxed(rect(48, 0, 800, 104)), 'clear')
    flushToastInsets()
    expect(vars().top).toBe('104px')
    off()
    flushToastInsets()
    expect(vars().top).toBe('48px')
  })

  it('clears the variables when the band unregisters, so a crashed shell leaves no stale inset', () => {
    const off = registerToastAnchor(boxed(rect(48, 0, 800, 700)), 'band')
    flushToastInsets()
    expect(vars().top).toBe('48px')
    off()
    flushToastInsets()
    expect(vars()).toEqual({ top: '', left: '', right: '' })
  })

  it('useToastAnchor registers on mount and unregisters on unmount', () => {
    function Band() {
      const ref = useToastAnchor('band')
      return <div ref={(el) => { if (el) el.getBoundingClientRect = () => rect(30, 10, 500, 600); return ref(el) }} />
    }
    const view = render(<Band />)
    flushToastInsets()
    expect(vars().top).toBe('30px')
    view.unmount()
    flushToastInsets()
    expect(vars().top).toBe('')
  })
})
