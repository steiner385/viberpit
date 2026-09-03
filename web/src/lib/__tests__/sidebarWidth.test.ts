import { describe, it, expect, beforeEach, vi } from 'vitest'
import {
  SIDEBAR_MIN,
  SIDEBAR_MAX,
  SIDEBAR_DEFAULT,
  clampSidebarWidth,
  loadSidebarWidth,
  saveSidebarWidth,
  SIDEBAR_STORAGE_KEY,
} from '../sidebarWidth'

describe('clampSidebarWidth', () => {
  it('passes through an in-range width', () => {
    expect(clampSidebarWidth(400)).toBe(400)
  })

  it('clamps below the minimum', () => {
    expect(clampSidebarWidth(SIDEBAR_MIN - 50)).toBe(SIDEBAR_MIN)
  })

  it('clamps above the maximum', () => {
    expect(clampSidebarWidth(SIDEBAR_MAX + 50)).toBe(SIDEBAR_MAX)
  })

  it('rounds fractional widths', () => {
    expect(clampSidebarWidth(400.7)).toBe(401)
  })

  it('falls back to the default for non-finite input', () => {
    expect(clampSidebarWidth(NaN)).toBe(SIDEBAR_DEFAULT)
    expect(clampSidebarWidth(Infinity)).toBe(SIDEBAR_MAX)
  })
})

describe('loadSidebarWidth / saveSidebarWidth', () => {
  beforeEach(() => localStorage.clear())

  it('returns the default when nothing is stored', () => {
    expect(loadSidebarWidth()).toBe(SIDEBAR_DEFAULT)
  })

  it('round-trips a saved width', () => {
    saveSidebarWidth(420)
    expect(loadSidebarWidth()).toBe(420)
  })

  it('clamps a stored value that is out of range', () => {
    localStorage.setItem(SIDEBAR_STORAGE_KEY, String(SIDEBAR_MAX + 999))
    expect(loadSidebarWidth()).toBe(SIDEBAR_MAX)
  })

  it('returns the default for a corrupt stored value', () => {
    localStorage.setItem(SIDEBAR_STORAGE_KEY, 'not-a-number')
    expect(loadSidebarWidth()).toBe(SIDEBAR_DEFAULT)
  })
})

// ── DEC-11: the key moved to `ccswitch.listWidth` ────────────────────────────
//
// The band is deliberately NOT renamed along with the key: SessionCard derives
// its container-query breakpoints from SIDEBAR_MAX = 560 and
// SessionCard.layout.test.tsx asserts the consequence.

/** The pre-AppShell key. Spelled out here on purpose — it is a historical
 *  literal, and importing it from the module would make these tests agree with
 *  whatever the module currently says rather than with what actually shipped. */
const LEGACY_KEY = 'ccswitch.sidebarWidth'

describe('listWidth migration (DEC-11)', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    localStorage.clear()
  })

  it('stores under ccswitch.listWidth', () => {
    expect(SIDEBAR_STORAGE_KEY).toBe('ccswitch.listWidth')
    saveSidebarWidth(400)
    expect(localStorage.getItem('ccswitch.listWidth')).toBe('400')
    expect(localStorage.getItem(LEGACY_KEY)).toBeNull()
  })

  it('migrates a legacy value forward and removes the old key', () => {
    localStorage.setItem(LEGACY_KEY, '432')
    expect(loadSidebarWidth()).toBe(432)
    expect(localStorage.getItem(SIDEBAR_STORAGE_KEY)).toBe('432')
    expect(localStorage.getItem(LEGACY_KEY)).toBeNull()
  })

  it('clamps a legacy value as it migrates', () => {
    localStorage.setItem(LEGACY_KEY, String(SIDEBAR_MAX + 999))
    expect(loadSidebarWidth()).toBe(SIDEBAR_MAX)
    expect(localStorage.getItem(SIDEBAR_STORAGE_KEY)).toBe(String(SIDEBAR_MAX))
    expect(localStorage.getItem(LEGACY_KEY)).toBeNull()
  })

  it('prefers the new key when both exist, and leaves the legacy key alone', () => {
    localStorage.setItem(LEGACY_KEY, '300')
    localStorage.setItem(SIDEBAR_STORAGE_KEY, '500')
    expect(loadSidebarWidth()).toBe(500)
    // The migration never runs while the new key is present, so it must not
    // touch the legacy key either — this is a read-through, not a cleanup pass.
    expect(localStorage.getItem(LEGACY_KEY)).toBe('300')
  })

  it('falls back to the default for a corrupt legacy value, and still clears it', () => {
    localStorage.setItem(LEGACY_KEY, 'not-a-number')
    expect(loadSidebarWidth()).toBe(SIDEBAR_DEFAULT)
    expect(localStorage.getItem(SIDEBAR_STORAGE_KEY)).toBe(String(SIDEBAR_DEFAULT))
    // Cleared, or the bad value would be re-read and re-discarded on every load.
    expect(localStorage.getItem(LEGACY_KEY)).toBeNull()
  })

  it('is one-way: a later save does not resurrect the legacy key', () => {
    localStorage.setItem(LEGACY_KEY, '432')
    loadSidebarWidth()
    saveSidebarWidth(360)
    expect(loadSidebarWidth()).toBe(360)
    expect(localStorage.getItem(LEGACY_KEY)).toBeNull()
  })

  it('returns the default without throwing when localStorage.getItem throws', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('SecurityError: storage is disabled')
    })
    expect(loadSidebarWidth()).toBe(SIDEBAR_DEFAULT)
  })

  it('does not throw when the migration write itself throws', () => {
    localStorage.setItem(LEGACY_KEY, '432')
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('QuotaExceededError')
    })
    expect(loadSidebarWidth()).toBe(SIDEBAR_DEFAULT)
  })

  it('does not throw when saving into a storage that throws', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('QuotaExceededError')
    })
    expect(() => saveSidebarWidth(400)).not.toThrow()
  })
})
