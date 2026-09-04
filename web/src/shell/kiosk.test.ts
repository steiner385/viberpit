import { describe, it, expect } from 'vitest'
import { readKioskConfig, DEFAULT_CYCLE_SECONDS, MIN_CYCLE_SECONDS } from './kiosk'

describe('readKioskConfig', () => {
  it('is disabled with no params', () => {
    const c = readKioskConfig('')
    expect(c.enabled).toBe(false)
    expect(c.cycleSeconds).toBe(DEFAULT_CYCLE_SECONDS)
    expect(c.pages).toEqual(['pipeline'])
    expect(c.theme).toBeUndefined()
    expect(readKioskConfig('?foo=1').enabled).toBe(false)
  })

  it('enables only on kiosk=1', () => {
    expect(readKioskConfig('?kiosk=1').enabled).toBe(true)
    expect(readKioskConfig('?kiosk=0').enabled).toBe(false)
    expect(readKioskConfig('?kiosk=true').enabled).toBe(false)
  })

  it('clamps cycle to the minimum and rounds', () => {
    expect(MIN_CYCLE_SECONDS).toBe(10)
    expect(readKioskConfig('?cycle=60').cycleSeconds).toBe(60)
    expect(readKioskConfig('?cycle=3').cycleSeconds).toBe(MIN_CYCLE_SECONDS)
    expect(readKioskConfig('?cycle=-5').cycleSeconds).toBe(MIN_CYCLE_SECONDS)
    expect(readKioskConfig('?cycle=45.6').cycleSeconds).toBe(46)
  })

  it('ignores a non-numeric or empty cycle', () => {
    expect(readKioskConfig('?cycle=abc').cycleSeconds).toBe(DEFAULT_CYCLE_SECONDS)
    expect(readKioskConfig('?cycle=').cycleSeconds).toBe(DEFAULT_CYCLE_SECONDS)
  })

  it('parses a comma-separated page list', () => {
    expect(readKioskConfig('?pages=pipeline,insights').pages).toEqual(['pipeline', 'insights'])
  })

  it('drops unknown pages and pages that are not kiosk-safe', () => {
    // 'settings' has showInKiosk:false; 'nope' is not a NavId at all.
    expect(readKioskConfig('?pages=pipeline,settings,nope').pages).toEqual(['pipeline'])
  })

  it('falls back to the default when every requested page is rejected', () => {
    expect(readKioskConfig('?pages=settings').pages).toEqual(['pipeline'])
    expect(readKioskConfig('?pages=').pages).toEqual(['pipeline'])
  })

  it('de-duplicates the page list while preserving order', () => {
    expect(readKioskConfig('?pages=insights,pipeline,insights').pages).toEqual(['insights', 'pipeline'])
  })

  it('accepts only light or dark for theme', () => {
    expect(readKioskConfig('?theme=light').theme).toBe('light')
    expect(readKioskConfig('?theme=dark').theme).toBe('dark')
    expect(readKioskConfig('?theme=system').theme).toBeUndefined()
    expect(readKioskConfig('?theme=purple').theme).toBeUndefined()
  })

  it('parses a full kiosk URL', () => {
    const c = readKioskConfig('?kiosk=1&pages=pipeline,insights&cycle=15&theme=dark')
    expect(c).toEqual({ enabled: true, cycleSeconds: 15, pages: ['pipeline', 'insights'], theme: 'dark' })
  })
})
