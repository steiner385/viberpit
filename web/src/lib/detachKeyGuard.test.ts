import { describe, it, expect, afterEach } from 'vitest'
import { detachKeyYieldsToTextEntry, type DetachKey } from './detachKeyGuard'

const KEYS: DetachKey[] = ['ArrowLeft', 'Escape', 'CtrlZ']

function mountTextarea(opts: { label?: string; value?: string; caret?: number | [number, number] } = {}) {
  const el = document.createElement('textarea')
  if (opts.label) el.setAttribute('aria-label', opts.label)
  el.value = opts.value ?? ''
  document.body.appendChild(el)
  if (opts.caret !== undefined) {
    const [start, end] = Array.isArray(opts.caret) ? opts.caret : [opts.caret, opts.caret]
    el.setSelectionRange(start, end)
  }
  return el
}

afterEach(() => { document.body.innerHTML = '' })

describe('detachKeyYieldsToTextEntry', () => {
  it('never yields when nothing (or a non-text element) is focused', () => {
    const button = document.createElement('button')
    for (const key of KEYS) {
      expect(detachKeyYieldsToTextEntry(key, null)).toBe(false)
      expect(detachKeyYieldsToTextEntry(key, button)).toBe(false)
    }
  })

  it('never yields to a checkbox input (value is "on", but it is not a text entry)', () => {
    const checkbox = document.createElement('input')
    checkbox.type = 'checkbox'
    for (const key of KEYS) {
      expect(detachKeyYieldsToTextEntry(key, checkbox)).toBe(false)
    }
  })

  describe('composer (aria-label "Message") — historical contract', () => {
    it('ArrowLeft detaches from an empty composer', () => {
      const el = mountTextarea({ label: 'Message' })
      expect(detachKeyYieldsToTextEntry('ArrowLeft', el)).toBe(false)
    })

    it('ArrowLeft detaches mid-draft from the left edge (caret 0, no selection)', () => {
      const el = mountTextarea({ label: 'Message', value: 'wip', caret: 0 })
      expect(detachKeyYieldsToTextEntry('ArrowLeft', el)).toBe(false)
    })

    it('ArrowLeft yields mid-draft when the caret is past the start', () => {
      const el = mountTextarea({ label: 'Message', value: 'wip', caret: 3 })
      expect(detachKeyYieldsToTextEntry('ArrowLeft', el)).toBe(true)
    })

    it('ArrowLeft yields when text is selected from the start (collapse, not navigate)', () => {
      const el = mountTextarea({ label: 'Message', value: 'wip', caret: [0, 3] })
      expect(detachKeyYieldsToTextEntry('ArrowLeft', el)).toBe(true)
    })

    it('Escape and Ctrl+Z yield only while the composer holds a draft', () => {
      const empty = mountTextarea({ label: 'Message' })
      const draft = mountTextarea({ label: 'Message', value: 'wip', caret: 0 })
      for (const key of ['Escape', 'CtrlZ'] as const) {
        expect(detachKeyYieldsToTextEntry(key, empty)).toBe(false)
        expect(detachKeyYieldsToTextEntry(key, draft)).toBe(true)
      }
    })
  })

  describe('any other text entry (question-card draft, search, rename …)', () => {
    it('every detach key yields while the field has text — even with the caret at 0', () => {
      const el = mountTextarea({ label: 'Write a response', value: 'my custom answer', caret: 0 })
      for (const key of KEYS) {
        expect(detachKeyYieldsToTextEntry(key, el)).toBe(true)
      }
    })

    it('ArrowLeft detaches from an empty field (caret is necessarily at the left edge)', () => {
      const el = mountTextarea({ label: 'Write a response' })
      expect(detachKeyYieldsToTextEntry('ArrowLeft', el)).toBe(false)
      expect(detachKeyYieldsToTextEntry('Escape', el)).toBe(false)
    })

    it('applies to text-like <input> elements too', () => {
      const input = document.createElement('input')
      input.type = 'search'
      input.value = 'query'
      document.body.appendChild(input)
      expect(detachKeyYieldsToTextEntry('ArrowLeft', input)).toBe(true)
      expect(detachKeyYieldsToTextEntry('Escape', input)).toBe(true)
    })
  })
})
