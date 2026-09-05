import { describe, it, expect } from 'vitest'
import { readNotifyPref, writeNotifyPref, NOTIFY_PREF_KEY, LEGACY_NOTIFY_PREF_KEY } from './notifyPref'

describe('notifyPref', () => {
  it('migrates the legacy prdash key once (DEC-7)', () => {
    localStorage.setItem(LEGACY_NOTIFY_PREF_KEY, 'true')
    expect(readNotifyPref()).toBe(true)
    expect(localStorage.getItem(NOTIFY_PREF_KEY)).toBe('true')
    expect(localStorage.getItem(LEGACY_NOTIFY_PREF_KEY)).toBeNull()
    writeNotifyPref(false)
    expect(readNotifyPref()).toBe(false)
  })
})
