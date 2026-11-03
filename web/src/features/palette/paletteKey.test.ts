/**
 * The ⌘K / Ctrl+K row's key rule (D-10 fix round 1). It lives here, not in
 * `shell/shortcuts.test.ts`, beside the palette it opens.
 *
 * `e.key` first — the letter the user's layout puts on the key is the K they
 * mean (Dvorak's K sits on the QWERTY V key) — and the physical `KeyK` only
 * when the layout reports no Latin letter at all (Cyrillic, Greek …).
 */
import { describe, expect, it } from 'vitest'
import { SHORTCUTS, type KeyboardLike } from '@/shell/shortcuts'

const row = SHORTCUTS.find((s) => s.id === 'app.palette')!
const ev = (over: Partial<KeyboardLike>): KeyboardLike =>
  ({ key: '', code: '', altKey: false, ctrlKey: false, metaKey: false, shiftKey: false, ...over })

describe('app.palette — which key is K', () => {
  it.each([
    ['Ctrl', { ctrlKey: true }],
    ['⌘', { metaKey: true }],
  ] as const)('%s+K on QWERTY matches', (_, mod) => {
    expect(row.match!(ev({ key: 'k', code: 'KeyK', ...mod }))).toBe(true)
  })

  it('Dvorak: the QWERTY K key types "t" — not the palette', () => {
    expect(row.match!(ev({ key: 't', code: 'KeyK', ctrlKey: true }))).toBe(false)
  })

  it('Dvorak: the key that types "k" (QWERTY V) IS the palette', () => {
    expect(row.match!(ev({ key: 'k', code: 'KeyV', ctrlKey: true }))).toBe(true)
  })

  it('Russian layout: a non-Latin key on the physical K falls back to the code', () => {
    expect(row.match!(ev({ key: 'л', code: 'KeyK', ctrlKey: true }))).toBe(true)
    expect(row.match!(ev({ key: 'л', code: 'KeyL', ctrlKey: true }))).toBe(false)
  })

  it('Shift is never the palette (Ctrl+Shift+K is Firefox’s console)', () => {
    expect(row.match!(ev({ key: 'K', code: 'KeyK', ctrlKey: true, shiftKey: true }))).toBe(false)
  })
})
