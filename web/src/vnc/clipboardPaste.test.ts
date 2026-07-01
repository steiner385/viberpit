import { describe, it, expect, vi } from 'vitest'
import { pasteClipboardToSession } from './clipboardPaste'

describe('pasteClipboardToSession', () => {
  it('types the clipboard text into the session via keysyms and reports "pasted"', async () => {
    const send = vi.fn()
    const result = await pasteClipboardToSession(async () => 'Hi', send)
    expect(result).toBe('pasted')
    // One press+release per character (down omitted), via the shared relay.
    expect(send.mock.calls).toEqual([
      [0x48, null], // 'H'
      [0x69, null], // 'i'
    ])
  })

  it('reports "empty" and sends nothing when the clipboard is empty', async () => {
    const send = vi.fn()
    const result = await pasteClipboardToSession(async () => '', send)
    expect(result).toBe('empty')
    expect(send).not.toHaveBeenCalled()
  })

  it('reports "denied" and sends nothing when clipboard access is blocked', async () => {
    const send = vi.fn()
    const result = await pasteClipboardToSession(async () => {
      throw new DOMException('denied', 'NotAllowedError')
    }, send)
    expect(result).toBe('denied')
    expect(send).not.toHaveBeenCalled()
  })
})
