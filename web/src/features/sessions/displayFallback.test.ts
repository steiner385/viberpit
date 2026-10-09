import { describe, it, expect } from 'vitest'
import { displayFallbackTitle } from './displayFallback'

describe('displayFallbackTitle', () => {
  it('is null without a reason', () => {
    expect(displayFallbackTitle(null)).toBeNull()
    expect(displayFallbackTitle(undefined)).toBeNull()
  })
  it.each([
    ['no_graphical_session', 'no one is logged in to the desktop'],
    ['cookie_missing', 'the desktop refused the connection (locked or session changed)'],
    ['connect_refused', 'the desktop refused the connection (locked or session changed)'],
    ['display_in_xvfb_pool', 'native display is not configured'],
    ['invalid_display', 'native display is not configured'],
    ['native_disabled', 'native display is not configured'],
  ])('maps %s', (reason, copy) => {
    expect(displayFallbackTitle(reason)).toBe(`Desktop unavailable — opened on a virtual display: ${copy}`)
  })
  it('still explains an unknown reason generically', () => {
    expect(displayFallbackTitle('future_reason')).toBe('Desktop unavailable — opened on a virtual display')
  })
})
