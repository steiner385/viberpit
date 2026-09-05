// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { sessionOpenState, readInitialPrompt } from '../sessionNavState'

// The two halves of one contract: what the open-a-session navigation WRITES and
// what the session route READS. They were a bare object literal in App.tsx and a
// separate inline cast 75 lines later; a round-trip test is only possible now
// that both live in one module.
describe('sessionNavState', () => {
  it('round-trips a prompt from the navigate() options back out of location.state', () => {
    const opts = sessionOpenState('fix the bug')
    expect(readInitialPrompt(opts?.state)).toBe('fix the bug')
  })

  it('produces no navigation state when there is no prompt', () => {
    // `undefined` is what `navigate(path)` takes for a plain navigation, so the
    // caller can pass the result through unconditionally.
    expect(sessionOpenState(undefined)).toBeUndefined()
    expect(sessionOpenState('')).toBeUndefined()
  })

  it('reads nothing from a deep link, where router state is null', () => {
    expect(readInitialPrompt(null)).toBeUndefined()
  })

  it('reads nothing from unrelated router state', () => {
    expect(readInitialPrompt({ from: '/pipeline' })).toBeUndefined()
  })

  it('rejects a non-string prompt rather than seeding it', () => {
    // Router state survives a reload and can have been written by an older
    // build, so its shape is validated, not asserted.
    expect(readInitialPrompt({ initialPrompt: 42 })).toBeUndefined()
    expect(readInitialPrompt({ initialPrompt: '' })).toBeUndefined()
    expect(readInitialPrompt('fix the bug')).toBeUndefined()
  })
})
