import { describe, it, expect } from 'vitest'
import { displayFallbackTitle, displayReadout } from './displayFallback'

describe('displayFallbackTitle', () => {
  it('is null without a reason', () => {
    expect(displayFallbackTitle(null)).toBeNull()
    expect(displayFallbackTitle(undefined)).toBeNull()
  })
  it.each([
    ['no_graphical_session', 'no one is logged in to the desktop'],
    ['cookie_missing', 'the desktop session changed or is not responding'],
    ['connect_refused', 'the desktop session changed or is not responding'],
    ['display_in_xvfb_pool', 'native display is not configured'],
    ['invalid_display', 'native display is not configured'],
    ['native_disabled', 'native display is not configured'],
    ['bridge_not_accepted', 'the target desktop does not accept this host'],
    ['bridge_unavailable', 'the display bridge to the target is unavailable'],
    ['bridge_wrong_user', "the target desktop belongs to a different user"],
    ['target_desktop_unavailable', 'no one is logged in to the target desktop'],
  ])('maps %s', (reason, copy) => {
    expect(displayFallbackTitle(reason)).toBe(`Desktop unavailable — opened on a virtual display: ${copy}`)
  })
  it('still explains an unknown reason generically', () => {
    expect(displayFallbackTitle('future_reason')).toBe('Desktop unavailable — opened on a virtual display')
  })
})

describe('displayReadout', () => {
  it('reads remote as the target desktop', () => {
    expect(displayReadout('remote', null, 'dobby')).toEqual({ value: 'remote', title: "On dobby's desktop" })
  })
  it('reads native as desktop', () => {
    expect(displayReadout('native', 'cookie_missing')).toEqual({ value: 'desktop', title: "Windows open on the host's desktop" })
  })
  it('reads a fallback reason as virtual, with the reason copy', () => {
    expect(displayReadout('virtual', 'native_disabled')).toEqual({
      value: 'virtual', title: 'Desktop unavailable — opened on a virtual display: native display is not configured'
    })
  })
  it('shows nothing for a plain virtual display or no mode', () => {
    expect(displayReadout('virtual', null)).toBeNull()
    expect(displayReadout(null, null)).toBeNull()
  })
})
