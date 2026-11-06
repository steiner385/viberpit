import { describe, it, expect } from 'vitest'
import { snoozePresets, snoozeUntilLabel } from './snoozePresets'

// Built from LOCAL fields so the assertions hold in any test-runner timezone:
// the presets are defined in the viewer's local time.
const local = (y: number, mo: number, d: number, h = 0, mi = 0) => new Date(y, mo - 1, d, h, mi)
const byId = (now: Date) => Object.fromEntries(snoozePresets(now).map((p) => [p.id, p]))

describe('snoozePresets', () => {
  it('lists the four presets in menu order', () => {
    expect(snoozePresets(local(2026, 11, 6, 15)).map((p) => p.label)).toEqual([
      'Snooze 1 hour',
      'Snooze until tomorrow 9am',
      'Snooze until Monday 9am',
      'Snooze until I wake it',
    ])
  })

  it('1 hour is exactly 60 minutes out, as ISO UTC', () => {
    const now = local(2026, 11, 6, 15, 30)
    const until = byId(now)['1h'].until!
    expect(until.endsWith('Z')).toBe(true)
    expect(Date.parse(until) - now.getTime()).toBe(60 * 60 * 1000)
  })

  it('tomorrow is 09:00 local the next calendar day, across a month end', () => {
    const until = new Date(byId(local(2026, 11, 30, 23, 50)).tomorrow.until!)
    expect([until.getFullYear(), until.getMonth() + 1, until.getDate(), until.getHours(), until.getMinutes()])
      .toEqual([2026, 12, 1, 9, 0])
  })

  it('Monday is the NEXT Monday at 09:00 local', () => {
    // 2026-11-06 is a Friday.
    const fri = new Date(byId(local(2026, 11, 6, 15)).monday.until!)
    expect([fri.getMonth() + 1, fri.getDate(), fri.getDay(), fri.getHours()]).toEqual([11, 9, 1, 9])
    // On a Monday (even before 9am) it is a week out, never today.
    const mon = new Date(byId(local(2026, 11, 9, 7)).monday.until!)
    expect([mon.getMonth() + 1, mon.getDate(), mon.getHours()]).toEqual([11, 16, 9])
    // Sunday -> the next day.
    const sun = new Date(byId(local(2026, 11, 8, 20)).monday.until!)
    expect(sun.getDate()).toBe(9)
  })

  it('"until I wake it" is indefinite', () => {
    expect(byId(local(2026, 11, 6)).manual.until).toBeNull()
  })
})

describe('snoozeUntilLabel', () => {
  it('names the wake time, or says it waits for a human', () => {
    expect(snoozeUntilLabel(null)).toBe('until you wake it')
    expect(snoozeUntilLabel('not a date')).toBe('until you wake it')
    expect(snoozeUntilLabel('2026-11-07T14:00:00Z')).toMatch(/^until .*Nov/)
  })
})
