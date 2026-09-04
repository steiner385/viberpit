import { describe, it, expect, afterEach } from 'vitest'
import {
  SHORTCUTS,
  matchShortcut,
  inGroup,
  shortcutsByGroup,
  shortcutAllowed,
  displayKeys,
  isApplePlatform,
  plain,
  meta,
  ctrl,
  cmdOrCtrl,
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
    yieldsToTextEntry: false,
    ...over,
  }
}

/**
 * The two row-scoped lookups the focused-row components make, spelled the way
 * `SessionCard` and `GroupHeader` spell them: the one selection rule, scoped by
 * group, with no context — see `matchShortcut`'s docstring for why the row's own
 * handler has none to give.
 */
const listAction = (e: KeyboardLike) => matchShortcut(e, inGroup('list-mode'))?.action
const headerAction = (e: KeyboardLike) => matchShortcut(e, inGroup('list-mode-header'))?.action
/**
 * The shell's lookup: the rows `AppShell` hands a handler to, and `when`
 * decides among them. Mirrors the handler map's ids, deliberately — the real map
 * is asserted end-to-end in `useShortcuts.test.tsx`; here the subject is the
 * RULE, and naming the candidates is what makes "a row the shell cannot act on
 * is skipped, not swallowed" assertable.
 */
const SHELL_HANDLED = [
  'app.help', 'app.assistant',
  'compose.help', 'compose.nav', 'insession.detach', 'insession.forceDetach',
]
const shellRow = (e: KeyboardLike, c: ShortcutContext) =>
  matchShortcut(e, (s) => SHELL_HANDLED.includes(s.id), c)?.id

describe('shortcuts registry — list mode', () => {
  it('maps each unmodified key to the right action', () => {
    expect(listAction(ev({ key: 'c' }))).toEqual({ type: 'continue' })
    expect(listAction(ev({ key: 'r' }))).toEqual({ type: 'rename' })
    expect(listAction(ev({ key: 'a' }))).toEqual({ type: 'archive' })
    expect(listAction(ev({ key: 'Backspace' }))).toEqual({ type: 'archive' })
    expect(listAction(ev({ key: 'd' }))).toEqual({ type: 'permanent-delete' })
    expect(listAction(ev({ key: 'Delete' }))).toEqual({ type: 'permanent-delete' })
    expect(listAction(ev({ key: 'Enter' }))).toEqual({ type: 'open' })
    expect(listAction(ev({ key: ' ' }))).toEqual({ type: 'open' })
    expect(listAction(ev({ key: 'ArrowRight' }))).toEqual({ type: 'open' })
    expect(listAction(ev({ key: 'ArrowUp' }))).toEqual({ type: 'navigate', dir: 'up' })
    expect(listAction(ev({ key: 'ArrowDown' }))).toEqual({ type: 'navigate', dir: 'down' })
    expect(listAction(ev({ key: 'Escape' }))).toEqual({ type: 'exit' })
    expect(listAction(ev({ key: 'ArrowLeft' }))).toEqual({ type: 'exit' })
    expect(listAction(ev({ key: 'Tab' }))).toEqual({ type: 'exit' })
  })

  it('does NOT match when a Ctrl/Alt/Meta modifier is held', () => {
    expect(listAction(ev({ key: 'a', ctrlKey: true }))).toBeUndefined() // Ctrl+A select-all
    expect(listAction(ev({ key: 'c', metaKey: true }))).toBeUndefined() // Cmd+C copy
    expect(listAction(ev({ key: 'd', altKey: true }))).toBeUndefined()
  })

  // DEC-5: `plain()` deliberately bails on Meta, which is why nothing in the app
  // binds Cmd today. If it were loosened, Cmd+A would stop selecting text in the
  // prompt and start ARCHIVING the focused session — hence a row-level guard,
  // not just the helper-level one below.
  it('Cmd+A archives nothing: every list-mode row bails on Meta', () => {
    for (const e of ['a', 'A', 'c', 'r', 'd', 'Enter', ' ', 'ArrowUp', 'ArrowDown', 'Escape']) {
      expect(listAction(ev({ key: e, metaKey: true })), `Cmd+${e}`).toBeUndefined()
      expect(headerAction(ev({ key: e, metaKey: true })), `Cmd+${e}`).toBeUndefined()
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
    expect(headerAction(ev({ key: 'ArrowRight' }))).toEqual({ type: 'expand' })
    expect(headerAction(ev({ key: 'ArrowLeft' }))).toEqual({ type: 'collapse' })
    expect(headerAction(ev({ key: 'Enter' }))).toEqual({ type: 'toggle' })
    expect(headerAction(ev({ key: ' ' }))).toEqual({ type: 'toggle' })
    expect(headerAction(ev({ key: 'ArrowUp' }))).toEqual({ type: 'navigate', dir: 'up' })
    expect(headerAction(ev({ key: 'ArrowDown' }))).toEqual({ type: 'navigate', dir: 'down' })
    expect(headerAction(ev({ key: 'Escape' }))).toEqual({ type: 'exit' })
    expect(headerAction(ev({ key: 'Tab' }))).toEqual({ type: 'exit' })
  })

  it('header keys never leak into the row scope (rows keep their meaning)', () => {
    expect(listAction(ev({ key: 'ArrowRight' }))).toEqual({ type: 'open' })
    expect(listAction(ev({ key: 'ArrowLeft' }))).toEqual({ type: 'exit' })
  })

  it('ignores header keys when a Ctrl/Alt/Meta modifier is held', () => {
    expect(headerAction(ev({ key: 'ArrowRight', metaKey: true }))).toBeUndefined()
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
  if (/^[a-z?;/]$/.test(glyph)) return glyph // 'c', 'r', 'a', 'd', '?', ';', '/'
  return null
}

/**
 * A whole display string read as ONE chord (`Ctrl+Z`, `⌘/`), or null.
 *
 * Tried BEFORE the `/`-split below, because `/` is both this file's "or"
 * separator and a bindable key: `Ctrl+/` splits into `['Ctrl+', '']`, and the
 * guard would have reported "no synthetic event could be built" for a row whose
 * binding is perfectly real. Parsing the chord first is the fix; loosening the
 * guard to skip unparseable strings would have been the bug, since "this row's
 * display string promises a key its matcher rejects" is the whole point.
 */
function chordFromKeys(keys: string): KeyboardLike | null {
  const asCtrl = /^Ctrl\+(.+)$/.exec(keys)
  if (asCtrl) {
    const key = keyForGlyph(asCtrl[1].toLowerCase())
    return key === null ? null : ev({ key, ctrlKey: true })
  }
  const asMeta = /^⌘(.+)$/.exec(keys)
  if (asMeta) {
    const key = keyForGlyph(asMeta[1].toLowerCase())
    return key === null ? null : ev({ key, metaKey: true })
  }
  return null
}

/**
 * Every key a row's own display string promises, as a synthetic event.
 *
 * Modifier chords are parsed too, not just bare glyphs: `insession.forceDetach`
 * is documented as `Ctrl+Z` and `app.help` as `Ctrl+/` / `⌘/`, and a
 * reachability guard that skipped every modifier combo would be blind to exactly
 * the rows whose matcher has to spell a modifier out.
 */
function eventsFromKeys(keys: string): KeyboardLike[] {
  const chord = chordFromKeys(keys.trim())
  if (chord !== null) return [chord]
  return keys
    .split('/')
    .map((part) => {
      const key = keyForGlyph(part.trim())
      return key === null ? null : ev({ key })
    })
    .filter((e): e is KeyboardLike => e !== null)
}

/**
 * Rows deliberately shipped WITHOUT a `match`: documented in the registry (the
 * one place the help sheet reads from) but dispatched by the component that owns
 * the mode — all six of them by `DispatchBar`, which owns the prompt's keys.
 *
 * Named individually rather than counted, so a wired row that silently loses its
 * `match` lands in this list and fails, instead of a count quietly staying equal.
 *
 * PR 4 removed FOUR entries from this list, and they are the whole point of that
 * PR: `compose.help`, `compose.nav`, `insession.detach` and
 * `insession.forceDetach` were dispatched by `useGlobalHelpKey`,
 * `useGlobalListNav` and `useInSessionKeys` — three listeners with three private
 * ideas of when a key may fire. They carry a `match` and a `when` now, and the
 * shell's one listener runs them. Their behaviour is pinned end-to-end in
 * `shell/useShortcuts.test.tsx` and, for the in-session three,
 * `features/sessions/MainPaneSession.keyboard.test.tsx`.
 */
const DISPLAY_ONLY_ROWS = [
  'compose.enterList',
  'compose.create',
  'compose.open',
  'compose.newline',
  'compose.jump',
  'compose.deselect',
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
      // BOTH display strings, not just `keys`: a Mac is shown `keysMac` and
      // nothing else, so a `keysMac` the matcher rejects is a binding that is
      // dead on exactly one platform — the failure mode no non-Apple test run
      // can see.
      for (const keys of [s.keys, s.keysMac].filter((k): k is string => k !== undefined)) {
        const events = eventsFromKeys(keys)
        expect(
          events.length,
          `${s.id}: no synthetic event could be built from keys "${keys}"`,
        ).toBeGreaterThan(0)
        for (const e of events) {
          expect(
            s.match(e),
            `${s.id}: match() rejects "${e.key}", which its own keys string "${keys}" promises`,
          ).toBe(true)
        }
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

  it('SHIPPED rows exercise the Apple branch — not only the fixtures above', () => {
    // Until Task 5 no row in `SHORTCUTS` carried a `keysMac`, so `displayKeys`'
    // Apple branch was reachable only from the two literals in this file. A
    // registry that loses its last `keysMac` makes the branch dead code again.
    const withMac = SHORTCUTS.filter((s) => s.keysMac !== undefined)
    expect(withMac.map((s) => s.id), 'no shipped row carries keysMac').not.toHaveLength(0)

    setPlatform('MacIntel')
    for (const s of withMac) expect(displayKeys(s), `${s.id} on MacIntel`).toBe(s.keysMac)
    setPlatform('Win32')
    for (const s of withMac) expect(displayKeys(s), `${s.id} on Win32`).toBe(s.keys)
  })

  it('a platform-varying row is a ⌘ chord on Apple and the SAME key on Ctrl elsewhere', () => {
    for (const s of SHORTCUTS.filter((x) => x.keysMac !== undefined)) {
      expect(s.keysMac, `${s.id} keysMac`).toMatch(/^⌘/)
      expect(s.keys, `${s.id} keys`).toMatch(/^Ctrl\+/)
      // The two must name the same terminal key, or a Mac user is told to press
      // something the matcher does not accept.
      expect(s.keysMac?.slice(1), `${s.id}`).toBe(s.keys.replace(/^Ctrl\+/, ''))
    }
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

describe('ctrl() / cmdOrCtrl()', () => {
  const ctrlA = ctrl((e) => e.key === 'a')
  const chordA = cmdOrCtrl((e) => e.key === 'a')

  it('ctrl() REQUIRES Ctrl and bails on Alt/Meta, but allows Shift', () => {
    expect(ctrlA(ev({ key: 'a', ctrlKey: true }))).toBe(true)
    expect(ctrlA(ev({ key: 'a', ctrlKey: true, shiftKey: true }))).toBe(true)
    expect(ctrlA(ev({ key: 'a' }))).toBe(false)
    expect(ctrlA(ev({ key: 'a', ctrlKey: true, metaKey: true }))).toBe(false)
    expect(ctrlA(ev({ key: 'a', ctrlKey: true, altKey: true }))).toBe(false)
  })

  it('cmdOrCtrl() accepts EITHER modifier — the same binding on both platforms', () => {
    expect(chordA(ev({ key: 'a', metaKey: true }))).toBe(true)
    expect(chordA(ev({ key: 'a', ctrlKey: true }))).toBe(true)
  })

  it('cmdOrCtrl() takes EXACTLY one: bare, both-together and Alt all fail', () => {
    // The failure a hand-rolled `(metaKey || ctrlKey) && !altKey` would have:
    // ⌘+Ctrl+a is a distinct chord (window managers and remote-desktop clients
    // bind it) and must not read as the app's.
    expect(chordA(ev({ key: 'a' }))).toBe(false)
    expect(chordA(ev({ key: 'a', metaKey: true, ctrlKey: true }))).toBe(false)
    expect(chordA(ev({ key: 'a', metaKey: true, altKey: true }))).toBe(false)
    expect(chordA(ev({ key: 'a', ctrlKey: true, altKey: true }))).toBe(false)
  })

  it('cmdOrCtrl() is disjoint from plain(), so no chord steals an unmodified key', () => {
    const plainA2 = plain((e) => e.key === 'a')
    for (const e of [
      ev({ key: 'a' }),
      ev({ key: 'a', metaKey: true }),
      ev({ key: 'a', ctrlKey: true }),
      ev({ key: 'a', shiftKey: true }),
    ]) {
      expect(plainA2(e) && chordA(e)).toBe(false)
    }
  })

  it('still honours the underlying predicate', () => {
    expect(ctrlA(ev({ key: 'b', ctrlKey: true }))).toBe(false)
    expect(chordA(ev({ key: 'b', metaKey: true }))).toBe(false)
  })
})

// ─── `matchShortcut` — the one selection rule ────────────────────────────────
//
// The unit-level half of PR 4. `shell/useShortcuts.test.tsx` asserts the same
// guards as behaviour through the real shell; these say WHICH ROW the rule picks
// and, for each guard, that it is the guard doing the blocking rather than the
// key failing to match at all.

describe('matchShortcut — the shell scope', () => {
  const input = document.createElement('input')
  const select = document.createElement('select')

  it('`?` and Shift+/ both reach the help row', () => {
    expect(shellRow(ev({ key: '?' }), ctx())).toBe('compose.help')
    expect(shellRow(ev({ key: '/', shiftKey: true }), ctx())).toBe('compose.help')
  })

  it('help is blocked by an overlay, by kiosk, and by a focused field', () => {
    expect(shellRow(ev({ key: '?' }), ctx({ overlayOpen: true }))).toBeUndefined()
    // Kiosk is read-only and has no drawer region to open into. This is the
    // context guard `when` was added for; without it a wall display's keyboard
    // could open panels nobody is there to close.
    expect(shellRow(ev({ key: '?' }), ctx({ interactive: false }))).toBeUndefined()
    expect(shellRow(ev({ key: '?' }), ctx({ active: input }))).toBeUndefined()
  })

  it('↑/↓ reach the list-nav row, but only in the Sessions section', () => {
    expect(shellRow(ev({ key: 'ArrowUp' }), ctx())).toBe('compose.nav')
    expect(shellRow(ev({ key: 'ArrowDown' }), ctx())).toBe('compose.nav')
    expect(shellRow(ev({ key: 'ArrowUp' }), ctx({ inSessions: false }))).toBeUndefined()
    expect(shellRow(ev({ key: 'ArrowUp' }), ctx({ overlayOpen: true }))).toBeUndefined()
    // A `<select>` owns its own arrows — it is not a text entry, so the
    // detach guard would not have caught it.
    expect(shellRow(ev({ key: 'ArrowDown' }), ctx({ active: select }))).toBeUndefined()
  })

  it('Esc and ← detach only with a session open, and yield to a card or a draft', () => {
    const open = ctx({ sessionOpen: true })
    expect(shellRow(ev({ key: 'Escape' }), open)).toBe('insession.detach')
    expect(shellRow(ev({ key: 'ArrowLeft' }), open)).toBe('insession.detach')
    expect(shellRow(ev({ key: 'Escape' }), ctx())).toBeUndefined()
    expect(shellRow(ev({ key: 'Escape' }), ctx({ sessionOpen: true, hasOpenAction: true }))).toBeUndefined()
    expect(shellRow(ev({ key: 'ArrowLeft' }), ctx({ sessionOpen: true, hasOpenAction: true }))).toBeUndefined()
    expect(shellRow(ev({ key: 'Escape' }), ctx({ sessionOpen: true, yieldsToTextEntry: true }))).toBeUndefined()
    expect(shellRow(ev({ key: 'Escape' }), ctx({ sessionOpen: true, overlayOpen: true }))).toBeUndefined()
  })

  it('Ctrl+Z force-detaches THROUGH an open card — the escape hatch — but not through a draft', () => {
    const carded = ctx({ sessionOpen: true, hasOpenAction: true })
    expect(shellRow(ev({ key: 'z', ctrlKey: true }), carded)).toBe('insession.forceDetach')
    expect(shellRow(ev({ key: 'Z', ctrlKey: true }), carded)).toBe('insession.forceDetach')
    // A draft still wins: Ctrl+Z in a text entry is native undo.
    expect(shellRow(ev({ key: 'z', ctrlKey: true }), ctx({ sessionOpen: true, yieldsToTextEntry: true })))
      .toBeUndefined()
    expect(shellRow(ev({ key: 'z', ctrlKey: true }), ctx())).toBeUndefined()
    // Cmd+Z is the platform's undo, not a detach.
    expect(shellRow(ev({ key: 'z', metaKey: true }), ctx({ sessionOpen: true }))).toBeUndefined()
  })

  // ── the app-global chords (Task 5) ─────────────────────────────────────────

  it('⌘/ and Ctrl+/ both reach the app-global help row, on either platform', () => {
    expect(shellRow(ev({ key: '/', metaKey: true }), ctx())).toBe('app.help')
    expect(shellRow(ev({ key: '/', ctrlKey: true }), ctx())).toBe('app.help')
  })

  it('⌘; and Ctrl+; both reach the assistant row', () => {
    expect(shellRow(ev({ key: ';', metaKey: true }), ctx())).toBe('app.assistant')
    expect(shellRow(ev({ key: ';', ctrlKey: true }), ctx())).toBe('app.assistant')
  })

  it('the chords fire WITH a text entry focused — unlike `?`, which is a character there', () => {
    // The whole reason `app.help` is a second row rather than a widened
    // `compose.help`: one row cannot carry two `when`s, and these two guards
    // genuinely differ.
    expect(shellRow(ev({ key: '?' }), ctx({ active: input }))).toBeUndefined()
    expect(shellRow(ev({ key: '/', metaKey: true }), ctx({ active: input }))).toBe('app.help')
    expect(shellRow(ev({ key: ';', metaKey: true }), ctx({ active: input }))).toBe('app.assistant')
    // …and even mid-prompt, where the composer claims the raw key.
    expect(shellRow(ev({ key: '/', metaKey: true }), ctx({ yieldsToTextEntry: true }))).toBe('app.help')
  })

  it('the chords are blocked in kiosk — a wall display has no drawer to open into', () => {
    // The first row-level `when` assertion for a CONTEXT-blocked row: kiosk is
    // `interactive: false`, and `REGION_VISIBILITY['app.drawer']` is false
    // there, so a handler that fired would open a region that is not mounted.
    expect(shellRow(ev({ key: '/', metaKey: true }), ctx({ interactive: false }))).toBeUndefined()
    expect(shellRow(ev({ key: '/', ctrlKey: true }), ctx({ interactive: false }))).toBeUndefined()
    expect(shellRow(ev({ key: ';', metaKey: true }), ctx({ interactive: false }))).toBeUndefined()
    expect(shellRow(ev({ key: ';', ctrlKey: true }), ctx({ interactive: false }))).toBeUndefined()
  })

  it('the chords are blocked under a modal or an open menu', () => {
    expect(shellRow(ev({ key: '/', metaKey: true }), ctx({ overlayOpen: true }))).toBeUndefined()
    expect(shellRow(ev({ key: ';', metaKey: true }), ctx({ overlayOpen: true }))).toBeUndefined()
  })

  it('the chords need EXACTLY one modifier — bare / and ; still type a character', () => {
    expect(shellRow(ev({ key: '/' }), ctx())).toBeUndefined()
    expect(shellRow(ev({ key: ';' }), ctx())).toBeUndefined()
    expect(shellRow(ev({ key: '/', metaKey: true, ctrlKey: true }), ctx())).toBeUndefined()
    expect(shellRow(ev({ key: '/', metaKey: true, altKey: true }), ctx())).toBeUndefined()
  })

  it('⌘⇧/ — the macOS Help-menu chord — is not ours: it reports "?", not "/"', () => {
    // `meta()` allows Shift, so the only thing keeping ⌘⇧/ out is that the
    // browser reports the shifted character. Pinned so a future matcher written
    // against `e.code` does not silently start swallowing it.
    expect(shellRow(ev({ key: '?', metaKey: true, shiftKey: true }), ctx())).toBeUndefined()
  })

  it('a row the caller cannot act on is skipped, and the walk continues past it', () => {
    // `list.up` matches ArrowUp and sits FIRST in the registry, ahead of
    // `compose.nav`. The shell has no handler for it, so the walk goes on rather
    // than stopping on a row it cannot run — which is how a focused row's keys
    // and the chrome's keys share one array.
    expect(matchShortcut(ev({ key: 'ArrowUp' }), () => true, ctx())?.id).toBe('list.up')
    expect(shellRow(ev({ key: 'ArrowUp' }), ctx())).toBe('compose.nav')
    expect(matchShortcut(ev({ key: 'ArrowUp' }), () => false, ctx())).toBeUndefined()
  })
})

// ─── The Desktop-parity rows that CANNOT be bound in a browser ───────────────
//
// Claude Code Desktop is Electron and owns its window; a web page does not.
// Chrome and Safari do not dispatch the keydown for a shortcut their own UI
// reserves, so there is no event to `preventDefault()` — deliberately, because
// a page that could swallow them could trap a keyboard-only user inside itself
// (w3c/uievents#7, Masayuki Nakano, 2016-01, which enumerates the sets).
//
// jsdom reserves nothing and will happily deliver every one of these, so this
// file cannot prove a browser withholds them. What it CAN prove, and what
// matters, is the half that is ours: the registry binds nothing to them and the
// help sheet therefore promises nothing about them. A row here that did nothing
// in a real browser would be a lie the sheet repeats every time it is opened.
describe('the un-bindable Desktop chords bind nothing', () => {
  /** Maximally permissive: every guard satisfied, so a match would be the ROW
   *  existing, never a context accidentally blocking it. */
  const anywhere = ctx({ sessionOpen: true, inSessions: true, interactive: true })

  const RESERVED: [string, KeyboardLike][] = [
    ['⌘N (new window)', ev({ key: 'n', metaKey: true })],
    ['Ctrl+N (new window)', ev({ key: 'n', ctrlKey: true })],
    ['⌘W (close tab)', ev({ key: 'w', metaKey: true })],
    ['Ctrl+W (close tab)', ev({ key: 'w', ctrlKey: true })],
    ['Ctrl+Tab (next tab)', ev({ key: 'Tab', ctrlKey: true })],
    ['Ctrl+Shift+Tab (previous tab)', ev({ key: 'Tab', ctrlKey: true, shiftKey: true })],
    // The spec's own fallback for next/previous session — Safari's and Chrome's
    // next/previous-TAB bindings on macOS. Dropped too; no replacement invented.
    ['⌘⇧] (next tab)', ev({ key: ']', metaKey: true, shiftKey: true })],
    ['⌘⇧[ (previous tab)', ev({ key: '[', metaKey: true, shiftKey: true })],
  ]

  it.each(RESERVED)('%s reaches no row in the whole registry', (label, e) => {
    // `() => true` for `canAct`: not "the shell has no handler", but "no row
    // anywhere — list, header, compose, in-session — matches this event".
    expect(matchShortcut(e, () => true, anywhere), label).toBeUndefined()
    // …and it is not selectable by a focused row or group header either.
    expect(matchShortcut(e, () => true), label).toBeUndefined()
  })

  it('the dropped row ids are absent, so the drop is a decision and not an oversight', () => {
    const ids = SHORTCUTS.map((s) => s.id)
    for (const id of ['app.newSession', 'session.close', 'session.next', 'session.prev']) {
      expect(ids, `${id} is un-bindable in a browser and must stay out`).not.toContain(id)
    }
  })

  it('no row DOCUMENTS a reserved chord — the help sheet renders from `keys`', () => {
    // The stronger half: a row could bind something else and still print
    // "⌘W" beside it, which is the promise this whole decision is about.
    const reserved = /(⌘|Cmd|Ctrl)\s*\+?\s*(N|W|Tab)\b|⌘\s*⇧\s*[[\]]/i
    for (const s of SHORTCUTS) {
      expect(s.keys, `${s.id} keys`).not.toMatch(reserved)
      expect(s.keysMac ?? '', `${s.id} keysMac`).not.toMatch(reserved)
    }
  })
})

describe('matchShortcut — the row scope has no context', () => {
  it('every list-mode and header row is context-free, so the row scope cannot lie', () => {
    // The invariant behind `matchShortcut`'s optional context: a focused row's
    // own keydown handler cannot answer `hasOpenAction` or `sessionOpen`. If a
    // row in these two groups ever gains a `when`, this fails and the call sites
    // in `SessionCard`/`GroupHeader` must be given a real context instead.
    for (const s of SHORTCUTS.filter((x) => x.group === 'list-mode' || x.group === 'list-mode-header')) {
      expect(s.when, `${s.id} carries a when the row scope cannot evaluate`).toBeUndefined()
    }
  })

  it('without a context, a row carrying a `when` is not selectable', () => {
    // The failure mode is "the row stops firing", never "the row fires under a
    // guessed context".
    expect(matchShortcut(ev({ key: '?' }), () => true)).toBeUndefined()
    expect(matchShortcut(ev({ key: 'ArrowUp' }), () => true)?.id).toBe('list.up')
  })

  it('the row scope keeps compose and in-session rows out, now that four of them match', () => {
    // The old `matchListShortcut` filtered `group !== 'list-mode-header'`, which
    // made every compose and in-session row a candidate for a focused row. That
    // was harmless only while none of them had a matcher.
    expect(listAction(ev({ key: '?' }))).toBeUndefined()
    expect(listAction(ev({ key: 'z', ctrlKey: true }))).toBeUndefined()
    expect(headerAction(ev({ key: '?' }))).toBeUndefined()
  })
})
