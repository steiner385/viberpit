import { describe, it, expect, beforeEach, vi } from 'vitest'
import { installState } from '../push'

const IPHONE_UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15'
const DESKTOP_UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0'

/** jsdom defines navigator.userAgent/platform/maxTouchPoints on the prototype as
 *  non-configurable getters, so `vi.spyOn(navigator, 'userAgent', 'get')` throws
 *  "property is not declared configurable". Redefining the own property works. */
function setEnv(opts: {
  ua: string
  standaloneMedia?: boolean
  navigatorStandalone?: boolean
  platform?: string
  maxTouchPoints?: number
}) {
  const define = (prop: string, value: unknown) =>
    Object.defineProperty(navigator, prop, { value, configurable: true, writable: true })
  define('userAgent', opts.ua)
  define('platform', opts.platform ?? 'iPhone')
  define('maxTouchPoints', opts.maxTouchPoints ?? 5)
  define('standalone', opts.navigatorStandalone)
  window.matchMedia = vi.fn().mockReturnValue({ matches: opts.standaloneMedia ?? false })
}

describe('installState', () => {
  // Every test calls setEnv with a complete environment, so no restore is
  // needed between them — but clear the matchMedia mock's call history.
  beforeEach(() => { vi.clearAllMocks() })

  it('is installed when the display-mode media query matches', () => {
    setEnv({ ua: DESKTOP_UA, standaloneMedia: true })
    expect(installState()).toBe('installed')
  })

  it('is installed on iOS via the legacy navigator.standalone flag', () => {
    setEnv({ ua: IPHONE_UA, navigatorStandalone: true })
    expect(installState()).toBe('installed')
  })

  it('needs install for iOS in a browser tab', () => {
    setEnv({ ua: IPHONE_UA })
    expect(installState()).toBe('needs-install')
  })

  it('needs install for iPadOS, which reports itself as a Mac', () => {
    // iPadOS 13+ sends a desktop Safari UA; the touch-points check is the
    // only reliable way to tell it from a real Mac.
    setEnv({
      ua: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15',
      platform: 'MacIntel',
      maxTouchPoints: 5,
    })
    expect(installState()).toBe('needs-install')
  })

  it('does not require install on a real Mac', () => {
    setEnv({
      ua: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15',
      platform: 'MacIntel',
      maxTouchPoints: 0,
    })
    expect(installState()).toBe('not-required')
  })

  it('does not require install on desktop Chrome', () => {
    setEnv({ ua: DESKTOP_UA, platform: 'Linux x86_64', maxTouchPoints: 0 })
    expect(installState()).toBe('not-required')
  })
})
