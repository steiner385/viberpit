import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { ENGINE_PREFERENCE_KEY, getPreferredEngine, setPreferredEngine } from './enginePreference'

describe('enginePreference', () => {
  beforeEach(() => window.localStorage.clear())
  afterEach(() => vi.restoreAllMocks())

  it('is null until the user has explicitly chosen (never "claude_code" by default)', () => {
    // The distinction matters: the DispatchBar must fall back to Claude Code for
    // upgrade safety, and a stored "claude_code" it never wrote would make an
    // absent preference indistinguishable from a real one.
    expect(getPreferredEngine()).toBeNull()
  })

  it('round-trips an explicit choice under a ccswitch.* key', () => {
    setPreferredEngine('codex')
    expect(window.localStorage.getItem(ENGINE_PREFERENCE_KEY)).toBe('codex')
    expect(getPreferredEngine()).toBe('codex')
    setPreferredEngine('claude_code')
    expect(getPreferredEngine()).toBe('claude_code')
  })

  it('ignores a stored value that is not a known engine', () => {
    window.localStorage.setItem(ENGINE_PREFERENCE_KEY, 'gpt-9')
    expect(getPreferredEngine()).toBeNull()
  })

  it('degrades to null when localStorage throws (private mode / quota)', () => {
    vi.spyOn(window.localStorage.__proto__, 'getItem').mockImplementation(() => {
      throw new Error('denied')
    })
    expect(getPreferredEngine()).toBeNull()
  })

  it('never throws when a write is refused', () => {
    vi.spyOn(window.localStorage.__proto__, 'setItem').mockImplementation(() => {
      throw new Error('quota')
    })
    expect(() => setPreferredEngine('codex')).not.toThrow()
  })
})
