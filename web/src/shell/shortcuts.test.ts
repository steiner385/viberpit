import { describe, it, expect, afterEach } from 'vitest'
import {
  SHORTCUTS,
  matchListShortcut,
  matchHeaderShortcut,
  shortcutsByGroup,
  shortcutAllowed,
  displayKeys,
  isApplePlatform,
  plain,
  meta,
  type KeyboardLike,
  type Shortcut,
  type ShortcutContext,
} from './shortcuts'

function ev(over: Partial<KeyboardLike>): KeyboardLike {
  return { key: '', code: '', altKey: false, ctrlKey: false, metaKey: false, shiftKey: false, ...over }
}

/** A fully-populated context; each test overrides only the field it is about. */
function ctx(over: Partial<ShortcutContext> = {}): ShortcutContext {
  return {
    overlayOpen: false,
    hasOpenAction: false,
    sessionOpen: false,
    inSessions: true,
    interactive: true,
    active: null,
    ...over,
  }
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

  // DEC-5: `plain()` deliberately bails on Meta, which is why nothing in the app
  // binds Cmd today. If it were loosened, Cmd+A would stop selecting text in the
  // prompt and start ARCHIVING the focused session — hence a row-level guard,
  // not just the helper-level one below.
  it('Cmd+A archives nothing: every list-mode row bails on Meta', () => {
    for (const e of ['a', 'A', 'c', 'r', 'd', 'Enter', ' ', 'ArrowUp', 'ArrowDown', 'Escape']) {
      expect(matchListShortcut(ev({ key: e, metaKey: true })), `Cmd+${e}`).toBeUndefined()
      expect(matchHeaderShortcut(ev({ key: e, metaKey: true })), `Cmd+${e}`).toBeUndefined()
    }
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

// ─── Registry integrity ──────────────────────────────────────────────────────
//
// The guard these exist for: `Shift+Tab` sat in the help sheet for a release
// after its handler was deleted, because nothing checked that a documented row
// still corresponds to something the app can dispatch.

/** The display glyphs the registry uses, mapped to the `KeyboardEvent.key` a
 *  browser actually reports. Anything not here (a modifier combo, a range like
 *  `Alt+1–9`) yields no synthetic event and is only legal on a display-only row. */
const KEY_BY_GLYPH: Record<string, string> = {
  Enter: 'Enter',
  Space: ' ',
  Esc: 'Escape',
  Tab: 'Tab',
  Backspace: 'Backspace',
  Delete: 'Delete',
  '→': 'ArrowRight',
  '←': 'ArrowLeft',
  '↑': 'ArrowUp',
  '↓': 'ArrowDown',
}

function keyForGlyph(glyph: string): string | null {
  if (glyph in KEY_BY_GLYPH) return KEY_BY_GLYPH[glyph]
  if (/^[a-z]$/.test(glyph)) return glyph // 'c', 'r', 'a', 'd'
  return null
}

/** Every unmodified key a row's own display string promises, as synthetic events. */
function eventsFromKeys(keys: string): KeyboardLike[] {
  return keys
    .split('/')
    .map((part) => keyForGlyph(part.trim()))
    .filter((key): key is string => key !== null)
    .map((key) => ev({ key }))
}

/**
 * Rows deliberately shipped WITHOUT a `match`: documented in the registry (the
 * one place the help sheet reads from) but dispatched by the component that owns
 * the mode — `DispatchBar` for compose, `useInSessionKeys` in-session.
 *
 * Named individually rather than counted, so a wired row that silently loses its
 * `match` lands in this list and fails, instead of a count quietly staying equal.
 */
const DISPLAY_ONLY_ROWS = [
  'compose.enterList',
  'compose.nav',
  'compose.create',
  'compose.open',
  'compose.newline',
  'compose.jump',
  'compose.deselect',
  'compose.help',
  'insession.detach',
  'insession.forceDetach',
]

describe('shortcuts registry — integrity', () => {
  it('every row has a unique id', () => {
    const ids = SHORTCUTS.map((s) => s.id)
    expect(ids).toEqual([...new Set(ids)])
  })

  it('every row with an action also has a match (a wired row that cannot match is dead)', () => {
    for (const s of SHORTCUTS.filter((x) => x.action)) {
      expect(s.match, `${s.id} has an action but no match`).toBeDefined()
    }
  })

  it('exactly the named rows are display-only; every other row is dispatchable', () => {
    expect(SHORTCUTS.filter((s) => !s.match).map((s) => s.id).sort()).toEqual(
      [...DISPLAY_ONLY_ROWS].sort(),
    )
  })

  it('every display-only row belongs to a group nothing in the registry dispatches', () => {
    for (const s of SHORTCUTS.filter((x) => !x.match)) {
      expect(['compose', 'in-session'], `${s.id}`).toContain(s.group)
      expect(s.action, `${s.id} is display-only but carries an action`).toBeUndefined()
    }
  })

  it('every wired row is REACHABLE: its matcher fires for a key from its own display string', () => {
    for (const s of SHORTCUTS.filter((x): x is Shortcut & { match: NonNullable<Shortcut['match']> } => !!x.match)) {
      const events = eventsFromKeys(s.keys)
      expect(
        events.length,
        `${s.id}: no synthetic event could be built from keys "${s.keys}"`,
      ).toBeGreaterThan(0)
      for (const e of events) {
        expect(
          s.match(e),
          `${s.id}: match() rejects "${e.key}", which its own keys string "${s.keys}" promises`,
        ).toBe(true)
      }
    }
  })

  it('every row is documented — non-empty keys and description', () => {
    for (const s of SHORTCUTS) {
      expect(s.keys, `${s.id}`).toMatch(/\S/)
      expect(s.description, `${s.id}`).toMatch(/\S/)
    }
  })

  it('Shift+Tab stays undocumented — the binding and its handler are both gone (D8)', () => {
    for (const s of SHORTCUTS) expect(s.keys).not.toMatch(/Shift\s*\+\s*Tab/i)
  })
})

// ─── `when` — where a row is allowed to fire ─────────────────────────────────

describe('shortcutAllowed', () => {
  const row: Shortcut = { id: 'x', keys: 'x', description: 'x', group: 'compose' }

  it('a row without `when` is always allowed', () => {
    expect(shortcutAllowed(row, ctx())).toBe(true)
    expect(shortcutAllowed(row, ctx({ overlayOpen: true, interactive: false }))).toBe(true)
  })

  it('a row with `when` is allowed only when the predicate accepts the live context', () => {
    const mutating: Shortcut = { ...row, when: (c) => c.interactive && !c.overlayOpen }
    expect(shortcutAllowed(mutating, ctx())).toBe(true)
    expect(shortcutAllowed(mutating, ctx({ interactive: false }))).toBe(false)
    expect(shortcutAllowed(mutating, ctx({ overlayOpen: true }))).toBe(false)
  })

  it('passes the whole context through, so a predicate can read focus', () => {
    const input = document.createElement('input')
    const notWhileTyping: Shortcut = { ...row, when: (c) => c.active?.tagName !== 'INPUT' }
    expect(shortcutAllowed(notWhileTyping, ctx({ active: input }))).toBe(false)
    expect(shortcutAllowed(notWhileTyping, ctx({ active: null }))).toBe(true)
  })

  it('no shipped row is unreachable under a fully-permissive context', () => {
    for (const s of SHORTCUTS) {
      expect(shortcutAllowed(s, ctx({ sessionOpen: true })), `${s.id}`).toBe(true)
    }
  })
})

// ─── Platform display ────────────────────────────────────────────────────────

/** jsdom reports `navigator.platform` as ''. Redefine it rather than casting
 *  a stub navigator into place. */
function setPlatform(value: string): void {
  Object.defineProperty(window.navigator, 'platform', { value, configurable: true })
}

describe('displayKeys / isApplePlatform', () => {
  const original = window.navigator.platform
  afterEach(() => setPlatform(original))

  const both: Shortcut = {
    id: 'p', keys: 'Ctrl+K', keysMac: '⌘K', description: 'p', group: 'compose',
  }
  const plainRow: Shortcut = { id: 'q', keys: 'Enter', description: 'q', group: 'compose' }

  it.each(['MacIntel', 'iPhone', 'iPad', 'iPod'])('%s is an Apple platform', (p) => {
    setPlatform(p)
    expect(isApplePlatform()).toBe(true)
    expect(displayKeys(both)).toBe('⌘K')
  })

  it.each(['Win32', 'Linux x86_64', ''])('%s is not an Apple platform', (p) => {
    setPlatform(p)
    expect(isApplePlatform()).toBe(false)
    expect(displayKeys(both)).toBe('Ctrl+K')
  })

  it('a row without keysMac shows `keys` on both platforms', () => {
    setPlatform('MacIntel')
    expect(displayKeys(plainRow)).toBe('Enter')
    setPlatform('Win32')
    expect(displayKeys(plainRow)).toBe('Enter')
  })

  it('every shipped row renders something on both platforms', () => {
    for (const p of ['MacIntel', 'Win32']) {
      setPlatform(p)
      for (const s of SHORTCUTS) expect(displayKeys(s), `${s.id} on ${p}`).toMatch(/\S/)
    }
  })
})

// ─── Matchers ────────────────────────────────────────────────────────────────

describe('plain() / meta()', () => {
  const plainA = plain((e) => e.key === 'a')
  const metaA = meta((e) => e.key === 'a')

  it('plain() bails on Ctrl, Alt and Meta, but allows Shift', () => {
    expect(plainA(ev({ key: 'a' }))).toBe(true)
    expect(plainA(ev({ key: 'a', shiftKey: true }))).toBe(true)
    expect(plainA(ev({ key: 'a', metaKey: true }))).toBe(false)
    expect(plainA(ev({ key: 'a', ctrlKey: true }))).toBe(false)
    expect(plainA(ev({ key: 'a', altKey: true }))).toBe(false)
  })

  it('meta() REQUIRES Meta and bails on Ctrl/Alt, but allows Shift', () => {
    expect(metaA(ev({ key: 'a', metaKey: true }))).toBe(true)
    expect(metaA(ev({ key: 'a', metaKey: true, shiftKey: true }))).toBe(true)
    expect(metaA(ev({ key: 'a' }))).toBe(false)
    expect(metaA(ev({ key: 'a', metaKey: true, ctrlKey: true }))).toBe(false)
    expect(metaA(ev({ key: 'a', metaKey: true, altKey: true }))).toBe(false)
  })

  it('the two are disjoint: no event satisfies both', () => {
    for (const e of [
      ev({ key: 'a' }),
      ev({ key: 'a', metaKey: true }),
      ev({ key: 'a', ctrlKey: true }),
      ev({ key: 'a', shiftKey: true }),
    ]) {
      expect(plainA(e) && metaA(e)).toBe(false)
    }
  })

  it('both still honour the underlying predicate', () => {
    expect(plainA(ev({ key: 'b' }))).toBe(false)
    expect(metaA(ev({ key: 'b', metaKey: true }))).toBe(false)
  })
})
