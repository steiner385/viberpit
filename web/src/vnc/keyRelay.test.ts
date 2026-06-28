import { describe, it, expect, vi } from 'vitest'
import { charToKeysym, relayInput, relayKeydown, XK_RETURN, XK_BACKSPACE } from './keyRelay'

describe('charToKeysym', () => {
  it('maps Latin-1 codepoints one-to-one', () => {
    expect(charToKeysym(0x41)).toBe(0x41) // 'A'
    expect(charToKeysym(0x20)).toBe(0x20) // space
    expect(charToKeysym(0xff)).toBe(0xff) // ÿ (upper Latin-1 boundary)
  })

  it('maps non-Latin-1 codepoints to the Unicode keysym form', () => {
    expect(charToKeysym(0x2603)).toBe(0x01000000 | 0x2603) // ☃ snowman
    expect(charToKeysym(0x1f600)).toBe(0x01000000 | 0x1f600) // 😀 (astral)
  })
})

describe('relayInput', () => {
  it('sends one key per character with down omitted (press+release)', () => {
    const send = vi.fn()
    relayInput('Hi', send)
    // send(keysym, code) — `down` omitted, so noVNC sends a press+release pair.
    expect(send.mock.calls).toEqual([
      [0x48, null], // 'H'
      [0x69, null], // 'i'
    ])
  })

  it('iterates by code point so astral characters stay intact', () => {
    const send = vi.fn()
    relayInput('😀', send)
    expect(send).toHaveBeenCalledTimes(1)
    expect(send).toHaveBeenCalledWith(0x01000000 | 0x1f600, null)
  })

  it('ignores empty input', () => {
    const send = vi.fn()
    relayInput('', send)
    expect(send).not.toHaveBeenCalled()
  })
})

describe('relayKeydown', () => {
  it('handles Enter as XK_Return and reports handled', () => {
    const send = vi.fn()
    expect(relayKeydown({ key: 'Enter', code: 'Enter' }, send)).toBe(true)
    expect(send).toHaveBeenCalledWith(XK_RETURN, 'Enter')
  })

  it('handles Backspace as XK_BackSpace and reports handled', () => {
    const send = vi.fn()
    expect(relayKeydown({ key: 'Backspace', code: 'Backspace' }, send)).toBe(true)
    expect(send).toHaveBeenCalledWith(XK_BACKSPACE, 'Backspace')
  })

  it('ignores printable keys (those arrive via relayInput) and reports unhandled', () => {
    const send = vi.fn()
    expect(relayKeydown({ key: 'a', code: 'KeyA' }, send)).toBe(false)
    expect(send).not.toHaveBeenCalled()
  })
})
