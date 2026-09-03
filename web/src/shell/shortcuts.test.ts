import { describe, it, expect } from 'vitest'
import { SHORTCUTS, matchListShortcut, matchHeaderShortcut, shortcutsByGroup, type KeyboardLike } from './shortcuts'

function ev(over: Partial<KeyboardLike>): KeyboardLike {
  return { key: '', code: '', altKey: false, ctrlKey: false, metaKey: false, shiftKey: false, ...over }
}

describe('shortcuts registry — list mode', () => {
  it('maps each unmodified key to the right action', () => {
    expect(matchListShortcut(ev({ key: 'c' }))).toEqual({ type: 'continue' })
    expect(matchListShortcut(ev({ key: 'r' }))).toEqual({ type: 'rename' })
    expect(matchListShortcut(ev({ key: 'a' }))).toEqual({ type: 'archive' })
    expect(matchListShortcut(ev({ key: 'Backspace' }))).toEqual({ type: 'archive' })
    expect(matchListShortcut(ev({ key: 'd' }))).toEqual({ type: 'permanent-delete' })
    expect(matchListShortcut(ev({ key: 'Delete' }))).toEqual({ type: 'permanent-delete' })
    expect(matchListShortcut(ev({ key: 'Enter' }))).toEqual({ type: 'open' })
    expect(matchListShortcut(ev({ key: ' ' }))).toEqual({ type: 'open' })
    expect(matchListShortcut(ev({ key: 'ArrowRight' }))).toEqual({ type: 'open' })
    expect(matchListShortcut(ev({ key: 'ArrowUp' }))).toEqual({ type: 'navigate', dir: 'up' })
    expect(matchListShortcut(ev({ key: 'ArrowDown' }))).toEqual({ type: 'navigate', dir: 'down' })
    expect(matchListShortcut(ev({ key: 'Escape' }))).toEqual({ type: 'exit' })
    expect(matchListShortcut(ev({ key: 'ArrowLeft' }))).toEqual({ type: 'exit' })
    expect(matchListShortcut(ev({ key: 'Tab' }))).toEqual({ type: 'exit' })
  })

  it('does NOT match when a Ctrl/Alt/Meta modifier is held', () => {
    expect(matchListShortcut(ev({ key: 'a', ctrlKey: true }))).toBeUndefined() // Ctrl+A select-all
    expect(matchListShortcut(ev({ key: 'c', metaKey: true }))).toBeUndefined() // Cmd+C copy
    expect(matchListShortcut(ev({ key: 'd', altKey: true }))).toBeUndefined()
  })

  it('exposes display entries for compose and in-session groups', () => {
    expect(shortcutsByGroup('compose').length).toBeGreaterThan(0)
    expect(shortcutsByGroup('in-session').length).toBeGreaterThan(0)
    expect(shortcutsByGroup('list-mode').length).toBeGreaterThan(0)
    for (const s of SHORTCUTS.filter((x) => x.match)) expect(s.keys).toMatch(/\S/)
  })
})

describe('shortcuts registry — group headers', () => {
  it('maps header keys to expand/collapse/toggle/navigate/exit', () => {
    expect(matchHeaderShortcut(ev({ key: 'ArrowRight' }))).toEqual({ type: 'expand' })
    expect(matchHeaderShortcut(ev({ key: 'ArrowLeft' }))).toEqual({ type: 'collapse' })
    expect(matchHeaderShortcut(ev({ key: 'Enter' }))).toEqual({ type: 'toggle' })
    expect(matchHeaderShortcut(ev({ key: ' ' }))).toEqual({ type: 'toggle' })
    expect(matchHeaderShortcut(ev({ key: 'ArrowUp' }))).toEqual({ type: 'navigate', dir: 'up' })
    expect(matchHeaderShortcut(ev({ key: 'ArrowDown' }))).toEqual({ type: 'navigate', dir: 'down' })
    expect(matchHeaderShortcut(ev({ key: 'Escape' }))).toEqual({ type: 'exit' })
    expect(matchHeaderShortcut(ev({ key: 'Tab' }))).toEqual({ type: 'exit' })
  })

  it('header keys never leak into matchListShortcut (rows keep their meaning)', () => {
    expect(matchListShortcut(ev({ key: 'ArrowRight' }))).toEqual({ type: 'open' })
    expect(matchListShortcut(ev({ key: 'ArrowLeft' }))).toEqual({ type: 'exit' })
  })

  it('ignores header keys when a Ctrl/Alt/Meta modifier is held', () => {
    expect(matchHeaderShortcut(ev({ key: 'ArrowRight', metaKey: true }))).toBeUndefined()
  })

  it('exposes display entries for the list-mode-header group', () => {
    expect(shortcutsByGroup('list-mode-header').length).toBeGreaterThan(0)
  })
})
