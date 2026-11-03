import { describe, it, expect, afterEach, beforeEach } from 'vitest'
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
  alt,
  altDigit,
  type KeyboardLike,
  type Shortcut,
  type ShortcutContext,
} from './shortcuts'
import {
  DISPATCH_PROMPT_ID, SESSION_COMPOSER_ATTR, SESSION_TRANSCRIPT_ATTR, focusAllowsTranscriptSearch, textEntryClaimsKey,
} from '@/lib/detachKeyGuard'

function ev(over: Partial<KeyboardLike>): KeyboardLike {
  return { key: '', code: '', altKey: false, ctrlKey: false, metaKey: false, shiftKey: false, ...over }
}

/** A fully-populated context; each test overrides only the field it is about. */
function ctx(over: Partial<ShortcutContext> = {}): ShortcutContext {
  return {
    overlayOpen: false,
    hasOpenAction: false,
    hasDeniablePermission: false,
    sessionOpen: false,
    sessionBusy: false,
    inSessions: true,
    interactive: true,
    active: null,
    yieldsToTextEntry: false,
    transcriptOpen: false,
    transcriptSearchFocus: false,
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
  'app.help', 'app.assistant', 'app.palette',
  'compose.help', 'compose.nav', 'insession.stop', 'insession.detach', 'insession.detachEsc',
  'insession.forceDetach', 'insession.denyPermission',
  'session.listPrev', 'session.listNext', 'session.jumpN',
  'transcript.promptPrev', 'transcript.promptNext', 'transcript.search',
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
  if (/^[a-z?;/.[\]1-9]$/.test(glyph)) return glyph // 'c', 'r', 'a', 'd', '?', ';', '/', '.', '[', ']', '1'
  return null
}

/** The physical key (`KeyboardEvent.code`) a glyph sits on — what an Alt row
 *  matches, because macOS Option rewrites `key` (⌥1 reports "¡"). */
const CODE_BY_KEY: Record<string, string> = {
  '[': 'BracketLeft',
  ']': 'BracketRight',
  ArrowUp: 'ArrowUp',
  ArrowDown: 'ArrowDown',
}

function codeForKey(key: string): string | null {
  if (key in CODE_BY_KEY) return CODE_BY_KEY[key]
  if (/^[1-9]$/.test(key)) return `Digit${key}`
  return null
}

/**
 * The D-10 chords as synthetic events, or null when `keys` is not one:
 * `Alt+[` / `Alt+↑` (Alt), `Alt+Shift+1–9` (Alt+Shift) and `⌘⌥[` / `⌘⌥1–9` /
 * `⌘⌥↑` (Meta+Alt, the Apple form). A range (`1–9`) yields one event per key in
 * it, so the reachability guard checks all nine digits rather than the first.
 * A string whose terminal glyph has no `code` here (the menus' `Alt+Shift+M`)
 * returns null and falls through to `chordFromKeys`.
 *
 * Each event carries the plain glyph as `key`, not what a layout or Option
 * would report (`¡`, `[` on a German ⌥5): the rows match on `code`, and those
 * rewritten forms are pinned by their own cases below.
 */
function altChordsFromKeys(keys: string): KeyboardLike[] | null {
  const m = /^(⌘⌥|Alt\+Shift\+|Alt\+)(.+)$/.exec(keys)
  if (!m) return null
  const mods: Partial<KeyboardLike> =
    m[1] === '⌘⌥' ? { metaKey: true, altKey: true }
    : m[1] === 'Alt+Shift+' ? { altKey: true, shiftKey: true }
    : { altKey: true }
  const range = /^([1-9])–([1-9])$/.exec(m[2])
  const glyphs = range
    ? Array.from({ length: Number(range[2]) - Number(range[1]) + 1 }, (_, i) => String(Number(range[1]) + i))
    : [m[2]]
  const events: KeyboardLike[] = []
  for (const glyph of glyphs) {
    const key = keyForGlyph(glyph)
    const code = key === null ? null : codeForKey(key)
    if (key === null || code === null) return null
    events.push(ev({ key, code, ...mods }))
  }
  return events
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
  // Alt+Shift+M / ⌥⇧M: matched on `code`, because Option rewrites `key` on macOS.
  const asAltShift = /^(?:Alt\+Shift\+|⌥⇧)([A-Z])$/.exec(keys)
  if (asAltShift) {
    return ev({ key: asAltShift[1], code: `Key${asAltShift[1]}`, altKey: true, shiftKey: true })
  }
  // A letter carries its physical key too (`Ctrl+K` → `KeyK`): `app.palette`
  // matches on `code`, so a non-Latin layout's ⌘K still opens it.
  const letterCode = (key: string) => (/^[a-z]$/.test(key) ? `Key${key.toUpperCase()}` : '')
  const asCtrl = /^Ctrl\+(.+)$/.exec(keys)
  if (asCtrl) {
    const key = keyForGlyph(asCtrl[1].toLowerCase())
    return key === null ? null : ev({ key, code: letterCode(key), ctrlKey: true })
  }
  const asMeta = /^⌘(.+)$/.exec(keys)
  if (asMeta) {
    const key = keyForGlyph(asMeta[1].toLowerCase())
    return key === null ? null : ev({ key, code: letterCode(key), metaKey: true })
  }
  // `Shift+a` reports the UPPERCASE key, unlike the two above which lowercase —
  // that difference is the whole reason a Shift row needs its own parse rather
  // than a looser glyph rule. `roadmap-inbox`'s `Shift+A` is a DIFFERENT action
  // from its `a` (accept the selection vs accept the focused row), so a guard
  // that could not tell the two apart would pass while they collided.
  const asShift = /^Shift\+(.+)$/.exec(keys)
  if (asShift) {
    const key = keyForGlyph(asShift[1].toLowerCase())
    return key === null ? null : ev({ key: key.toUpperCase(), shiftKey: true })
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
  const alts = altChordsFromKeys(keys.trim())
  if (alts !== null) return alts
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
  'compose.deselect',
]

describe('shortcuts registry — integrity', () => {
  // The reachability and reserved-chord cases switch platforms; restore it even
  // when an assertion throws mid-loop, so no later case runs on the wrong one.
  afterEach(() => setPlatform(ORIGINAL_PLATFORM))

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
      // Each on its own platform: the D-10 rows are platform-aware (⌘⌥ on
      // Apple, Alt elsewhere), so `keysMac` is only a promise on a Mac.
      const shown: [string, string][] = [['Win32', s.keys]]
      if (s.keysMac !== undefined) shown.push(['MacIntel', s.keysMac])
      for (const [platform, keys] of shown) {
        setPlatform(platform)
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

// ─── The Roadmap Inbox group (#1494) ─────────────────────────────────────────
//
// The keys live here so the help sheet can list them (`DrawerHost.test.tsx`
// asserts every group and every description renders); the LISTENER stays in
// `features/roadmap/inbox/InboxScopeBody`, which resolves them through
// `matchShortcut(e, inGroup('roadmap-inbox'))`.

describe('shortcuts registry — roadmap-inbox', () => {
  const resolve = (e: Partial<KeyboardLike>) =>
    matchShortcut(ev(e), inGroup('roadmap-inbox'))?.id

  it('resolves the scope\'s whole key map', () => {
    expect(resolve({ key: 'j' })).toBe('inbox.next')
    expect(resolve({ key: 'k' })).toBe('inbox.prev')
    expect(resolve({ key: 'a' })).toBe('inbox.accept')
    expect(resolve({ key: 'r' })).toBe('inbox.reject')
    expect(resolve({ key: 'e' })).toBe('inbox.edit')
    expect(resolve({ key: 'x' })).toBe('inbox.select')
  })

  it('keeps `a` and Shift+A apart — they are different, irreversible actions', () => {
    // One accepts the focused row, the other the whole ticked selection. A single
    // case-insensitive row (the way `list.archive` matches both) would make one
    // keystroke do the other's work.
    expect(resolve({ key: 'A', shiftKey: true })).toBe('inbox.acceptSelected')
    expect(resolve({ key: 'a' })).toBe('inbox.accept')
  })

  it('keeps `r` and Shift+R apart the same way', () => {
    expect(resolve({ key: 'R', shiftKey: true })).toBe('inbox.rejectSelected')
    expect(resolve({ key: 'r' })).toBe('inbox.reject')
  })

  it('keeps `d` (dismiss one) and Shift+D (dismiss the selection) apart too', () => {
    expect(resolve({ key: 'd' })).toBe('inbox.dismiss')
    expect(resolve({ key: 'D', shiftKey: true })).toBe('inbox.dismissSelected')
    // List mode's `d` is delete; the inbox's never leaks into it.
    expect(matchShortcut(ev({ key: 'd' }), inGroup('list-mode'))?.id).toBe('list.delete')
  })

  it('never fires under a Ctrl/Meta/Alt chord that belongs to the browser', () => {
    expect(resolve({ key: 'a', ctrlKey: true })).toBeUndefined()   // Ctrl+A: select all
    expect(resolve({ key: 'r', metaKey: true })).toBeUndefined()   // ⌘R: reload
  })

  it('is invisible to the shell dispatcher: no handler id, and no `when` to judge', () => {
    // `useShortcuts` can only fire a row whose id has a handler in AppShellInner,
    // and it registers none of these — which is deliberate: `a` and `r` are
    // irreversible past the undo window and must not outlive the Inbox scope.
    // No `when` either, because the real guards (`overlayIsOpen`, `isTypingTarget`)
    // are the scope's and this array cannot see them.
    for (const s of shortcutsByGroup('roadmap-inbox')) {
      expect(s.when, `${s.id} must not carry a when`).toBeUndefined()
      expect(s.action, `${s.id} is not a session-row action`).toBeUndefined()
    }
  })

  it('does not steal the list-mode letters that share its glyphs', () => {
    // `a` is archive in list mode and accept here; `r` is rename there. Each
    // scope resolves inside its own group, so neither leaks.
    expect(matchShortcut(ev({ key: 'a' }), inGroup('list-mode'))?.id).toBe('list.archive')
    expect(matchShortcut(ev({ key: 'r' }), inGroup('list-mode'))?.id).toBe('list.rename')
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
    // Two permissive contexts, idle and busy: the Esc rows split on `sessionBusy`
    // by design (stop vs detach), so no ONE context reaches both. A pending
    // deniable permission is part of "permissive": it is the positive fact
    // `insession.denyPermission` needs, and no row treats it as a blocker. So is
    // an on-screen transcript (`transcript.search`, D-11).
    const permissive = [
      ctx({ sessionOpen: true, hasDeniablePermission: true, transcriptOpen: true, transcriptSearchFocus: true }),
      ctx({ sessionOpen: true, sessionBusy: true, hasDeniablePermission: true, transcriptOpen: true, transcriptSearchFocus: true }),
    ]
    for (const s of SHORTCUTS) {
      expect(permissive.some((c) => shortcutAllowed(s, c)), `${s.id}`).toBe(true)
    }
  })
})

// ─── Platform display ────────────────────────────────────────────────────────

/** jsdom reports `navigator.platform` as ''. Redefine it rather than casting
 *  a stub navigator into place. */
const ORIGINAL_PLATFORM = window.navigator.platform
function setPlatform(value: string): void {
  Object.defineProperty(window.navigator, 'platform', { value, configurable: true })
}

const D10_ROWS = [
  'session.listPrev', 'session.listNext', 'session.jumpN', 'transcript.promptPrev', 'transcript.promptNext',
]

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
      // Alt+Shift+letter rows are ⌥⇧ on Apple, not ⌘ (Option, not Command).
      const altShift = /^Alt\+Shift\+([A-Z])$/.exec(s.keys)
      if (altShift) {
        expect(s.keysMac, `${s.id} keysMac`).toBe(`⌥⇧${altShift[1]}`)
        continue
      }
      // D-10's navigation rows are ⌘⌥ on Apple — a bare ⌥ types characters on
      // non-US Mac layouts (German ⌥5 is "[") and moves the caret (⌥↑) — and Alt
      // or Alt+Shift elsewhere, on the same terminal key.
      if (D10_ROWS.includes(s.id)) {
        expect(s.keysMac, `${s.id} keysMac`).toMatch(/^⌘⌥/)
        expect(s.keys, `${s.id} keys`).toMatch(/^Alt\+/)
        expect(s.keysMac?.slice(2), `${s.id}`).toBe(s.keys.replace(/^Alt\+(Shift\+)?/, ''))
        continue
      }
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
    expect(shellRow(ev({ key: 'Escape' }), open)).toBe('insession.detachEsc')
    expect(shellRow(ev({ key: 'ArrowLeft' }), open)).toBe('insession.detach')
    expect(shellRow(ev({ key: 'Escape' }), ctx())).toBeUndefined()
    expect(shellRow(ev({ key: 'Escape' }), ctx({ sessionOpen: true, hasOpenAction: true }))).toBeUndefined()
    expect(shellRow(ev({ key: 'ArrowLeft' }), ctx({ sessionOpen: true, hasOpenAction: true }))).toBeUndefined()
    expect(shellRow(ev({ key: 'Escape' }), ctx({ sessionOpen: true, yieldsToTextEntry: true }))).toBeUndefined()
    expect(shellRow(ev({ key: 'Escape' }), ctx({ sessionOpen: true, overlayOpen: true }))).toBeUndefined()
  })

  it('while a turn runs, Esc stops it — through a draft, but not through a card or an overlay; ← still detaches', () => {
    const busy = ctx({ sessionOpen: true, sessionBusy: true })
    expect(shellRow(ev({ key: 'Escape' }), busy)).toBe('insession.stop')
    expect(shellRow(ev({ key: 'ArrowLeft' }), busy)).toBe('insession.detach')
    // Desktop stops regardless of the session composer's draft (the draft is kept)…
    const composer = document.createElement('textarea')
    composer.setAttribute(SESSION_COMPOSER_ATTR, '')
    expect(shellRow(ev({ key: 'Escape' }),
      ctx({ sessionOpen: true, sessionBusy: true, yieldsToTextEntry: true, active: composer })))
      .toBe('insession.stop')
    // …and from the EMPTY session composer, or with focus on the page itself.
    expect(shellRow(ev({ key: 'Escape' }), ctx({ sessionOpen: true, sessionBusy: true, active: composer })))
      .toBe('insession.stop')
    expect(shellRow(ev({ key: 'Escape' }), ctx({ sessionOpen: true, sessionBusy: true, active: document.body })))
      .toBe('insession.stop')
    // Any OTHER text entry keeps Esc — with text or EMPTY. Each fixture's
    // `yieldsToTextEntry` is what the shell would compute for that field: an
    // empty field does not claim Esc, so the focused element itself must block.
    const search = document.createElement('input')
    search.type = 'search'
    expect(shellRow(ev({ key: 'Escape' }),
      ctx({ sessionOpen: true, sessionBusy: true, yieldsToTextEntry: textEntryClaimsKey(ev({ key: 'Escape' }), search), active: search })))
      .toBeUndefined()
    search.value = 'abc'
    expect(shellRow(ev({ key: 'Escape' }),
      ctx({ sessionOpen: true, sessionBusy: true, yieldsToTextEntry: textEntryClaimsKey(ev({ key: 'Escape' }), search), active: search })))
      .toBeUndefined()
    expect(shellRow(ev({ key: 'Escape' }), ctx({ sessionOpen: true, sessionBusy: true, hasOpenAction: true })))
      .toBeUndefined()
    expect(shellRow(ev({ key: 'Escape' }), ctx({ sessionOpen: true, sessionBusy: true, overlayOpen: true })))
      .toBeUndefined()
    expect(shellRow(ev({ key: 'Escape' }), ctx({ sessionBusy: true }))).toBeUndefined()
    // Kiosk is read-only: a wall display's keyboard must not stop a turn.
    expect(shellRow(ev({ key: 'Escape' }), ctx({ sessionOpen: true, sessionBusy: true, interactive: false })))
      .toBeUndefined()
  })

  it('the two Esc rows are mutually exclusive, so registry order never decides between them', () => {
    const stop = SHORTCUTS.find((s) => s.id === 'insession.stop')!
    const detachEsc = SHORTCUTS.find((s) => s.id === 'insession.detachEsc')!
    for (const sessionBusy of [true, false]) {
      const c = ctx({ sessionOpen: true, sessionBusy })
      expect(shortcutAllowed(stop, c) && shortcutAllowed(detachEsc, c)).toBe(false)
    }
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

  it('⌘. and Ctrl+. deny a pending permission — through a focused composer, never in kiosk or under an overlay', () => {
    const carded = ctx({ sessionOpen: true, hasOpenAction: true, hasDeniablePermission: true })
    expect(shellRow(ev({ key: '.', metaKey: true }), carded)).toBe('insession.denyPermission')
    expect(shellRow(ev({ key: '.', ctrlKey: true }), carded)).toBe('insession.denyPermission')
    // A chord is not a character: it fires while the composer holds a draft.
    expect(shellRow(ev({ key: '.', ctrlKey: true }), { ...carded, yieldsToTextEntry: true, active: input }))
      .toBe('insession.denyPermission')
    // No deniable card (none, or only a plan card — `hasOpenAction` without `hasDeniablePermission`).
    expect(shellRow(ev({ key: '.', ctrlKey: true }), ctx({ sessionOpen: true }))).toBeUndefined()
    expect(shellRow(ev({ key: '.', ctrlKey: true }), ctx({ sessionOpen: true, hasOpenAction: true }))).toBeUndefined()
    expect(shellRow(ev({ key: '.', ctrlKey: true }), { ...carded, interactive: false })).toBeUndefined()
    expect(shellRow(ev({ key: '.', ctrlKey: true }), { ...carded, overlayOpen: true })).toBeUndefined()
    expect(shellRow(ev({ key: '.', ctrlKey: true }), { ...carded, sessionOpen: false })).toBeUndefined()
    // Bare `.` is a character.
    expect(shellRow(ev({ key: '.' }), carded)).toBeUndefined()
  })

  it('⌘O and Ctrl+O cycle the view, but ⌘⇧O / Ctrl+Shift+O (Chrome Bookmark Manager) is left to the browser', () => {
    const cycle = (e: KeyboardLike) => matchShortcut(e, (s) => s.id === 'insession.cycleView', ctx({ sessionOpen: true }))?.id
    expect(cycle(ev({ key: 'o', ctrlKey: true }))).toBe('insession.cycleView')
    expect(cycle(ev({ key: 'o', metaKey: true }))).toBe('insession.cycleView')
    expect(cycle(ev({ key: 'O', ctrlKey: true, shiftKey: true }))).toBeUndefined()
    expect(cycle(ev({ key: 'O', metaKey: true, shiftKey: true }))).toBeUndefined()
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

  // ── the palette chord (D-10) ───────────────────────────────────────────────

  it('⌘K and Ctrl+K reach the palette row — on the physical K, so a non-Latin layout works', () => {
    expect(shellRow(ev({ key: 'k', code: 'KeyK', metaKey: true }), ctx())).toBe('app.palette')
    expect(shellRow(ev({ key: 'k', code: 'KeyK', ctrlKey: true }), ctx())).toBe('app.palette')
    // Russian layout: the K key reports "л".
    expect(shellRow(ev({ key: 'л', code: 'KeyK', ctrlKey: true }), ctx())).toBe('app.palette')
  })

  it('the palette chord fires from the composer mid-draft, and anywhere off the Sessions section', () => {
    const composer = document.createElement('textarea')
    composer.setAttribute(SESSION_COMPOSER_ATTR, '')
    expect(shellRow(ev({ key: 'k', code: 'KeyK', ctrlKey: true }),
      ctx({ active: composer, yieldsToTextEntry: true, sessionOpen: true }))).toBe('app.palette')
    expect(shellRow(ev({ key: 'k', code: 'KeyK', metaKey: true }), ctx({ inSessions: false }))).toBe('app.palette')
  })

  it('the palette chord is blocked in kiosk and under an overlay (the palette closes itself)', () => {
    expect(shellRow(ev({ key: 'k', code: 'KeyK', ctrlKey: true }), ctx({ interactive: false }))).toBeUndefined()
    expect(shellRow(ev({ key: 'k', code: 'KeyK', ctrlKey: true }), ctx({ overlayOpen: true }))).toBeUndefined()
  })

  it('the palette chord needs EXACTLY ⌘ or Ctrl — bare k types, and Ctrl+Shift+K is Firefox’s console', () => {
    expect(shellRow(ev({ key: 'k', code: 'KeyK' }), ctx())).toBeUndefined()
    expect(shellRow(ev({ key: 'K', code: 'KeyK', ctrlKey: true, shiftKey: true }), ctx())).toBeUndefined()
    expect(shellRow(ev({ key: 'K', code: 'KeyK', metaKey: true, shiftKey: true }), ctx())).toBeUndefined()
    expect(shellRow(ev({ key: 'k', code: 'KeyK', ctrlKey: true, altKey: true }), ctx())).toBeUndefined()
    expect(shellRow(ev({ key: 'k', code: 'KeyK', ctrlKey: true, metaKey: true }), ctx())).toBeUndefined()
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

// ─── D-10: session switching and prompt jumps (on `e.code`) ──────────────────
//
// Apple: ⌘⌥[ ] / ⌘⌥1–9 / ⌘⌥↑↓. A bare ⌥ chord is TYPING on a Mac — non-US
// layouts put brackets and braces on ⌥5/6/8/9 (German ⌥5 is "["), and ⌥↑/↓
// move the caret in a textarea — so the Mac form adds ⌘.
// Elsewhere: Alt+[ ] / Alt+↑↓, and Alt+SHIFT+1–9 — Chrome and Firefox on Linux
// select tabs with Alt+1–9, and taking it would break tab switching.

describe('matchShortcut — the D-10 navigation chords', () => {
  const composer = document.createElement('textarea')
  composer.setAttribute(SESSION_COMPOSER_ATTR, '')
  const dispatch = document.createElement('textarea')
  dispatch.id = DISPATCH_PROMPT_ID
  const search = document.createElement('input')
  search.type = 'search'
  const open = ctx({ sessionOpen: true })
  afterEach(() => setPlatform(ORIGINAL_PLATFORM))

  describe('on Linux / Windows', () => {
    beforeEach(() => setPlatform('Linux x86_64'))

    it('Alt+[ / Alt+] step the list — with or without a session open', () => {
      expect(shellRow(ev({ key: '[', code: 'BracketLeft', altKey: true }), ctx())).toBe('session.listPrev')
      expect(shellRow(ev({ key: ']', code: 'BracketRight', altKey: true }), ctx())).toBe('session.listNext')
      expect(shellRow(ev({ key: '[', code: 'BracketLeft', altKey: true }), open)).toBe('session.listPrev')
      expect(shellRow(ev({ key: ']', code: 'BracketRight', altKey: true }), open)).toBe('session.listNext')
    })

    it('Alt+Shift+1–9 reach the jump row; the digit is read from the physical key', () => {
      for (let n = 1; n <= 9; n++) {
        const e = ev({ key: '!@#$%^&*('[n - 1], code: `Digit${n}`, altKey: true, shiftKey: true })
        expect(shellRow(e, ctx())).toBe('session.jumpN')
        expect(altDigit(e)).toBe(n)
      }
      expect(shellRow(ev({ key: ')', code: 'Digit0', altKey: true, shiftKey: true }), ctx())).toBeUndefined()
      expect(altDigit(ev({ key: '0', code: 'Digit0', altKey: true }))).toBeNull()
    })

    it('bare Alt+1–9 is the browser\'s tab select — it reaches no row', () => {
      for (let n = 1; n <= 9; n++) {
        expect(shellRow(ev({ key: String(n), code: `Digit${n}`, altKey: true }), open)).toBeUndefined()
      }
    })

    it('Alt+↑ / Alt+↓ jump between own prompts, but only with a session open', () => {
      expect(shellRow(ev({ key: 'ArrowUp', code: 'ArrowUp', altKey: true }), open)).toBe('transcript.promptPrev')
      expect(shellRow(ev({ key: 'ArrowDown', code: 'ArrowDown', altKey: true }), open)).toBe('transcript.promptNext')
      expect(shellRow(ev({ key: 'ArrowUp', code: 'ArrowUp', altKey: true }), ctx())).toBeUndefined()
    })

    it('need their exact modifiers: Ctrl or Meta alongside is some other chord', () => {
      for (const extra of [{ ctrlKey: true }, { metaKey: true }]) {
        expect(shellRow(ev({ key: ']', code: 'BracketRight', altKey: true, ...extra }), open)).toBeUndefined()
        expect(shellRow(ev({ key: '#', code: 'Digit3', altKey: true, shiftKey: true, ...extra }), open)).toBeUndefined()
        expect(shellRow(ev({ key: 'ArrowUp', code: 'ArrowUp', altKey: true, ...extra }), open)).toBeUndefined()
      }
      expect(shellRow(ev({ key: '}', code: 'BracketRight', altKey: true, shiftKey: true }), open)).toBeUndefined()
      expect(shellRow(ev({ key: 'ArrowUp', code: 'ArrowUp', altKey: true, shiftKey: true }), open)).toBeUndefined()
    })

    it('plain keys are untouched — ↑/↓ stay the list highlight\'s', () => {
      expect(shellRow(ev({ key: 'ArrowUp', code: 'ArrowUp' }), open)).toBe('compose.nav')
      expect(shellRow(ev({ key: ']', code: 'BracketRight' }), ctx())).toBeUndefined()
      expect(shellRow(ev({ key: '3', code: 'Digit3' }), ctx())).toBeUndefined()
    })

    it('fire from the session composer and the new-session prompt, which yield these chords to the shell', () => {
      for (const active of [composer, dispatch]) {
        expect(shellRow(ev({ key: '#', code: 'Digit3', altKey: true, shiftKey: true }), ctx({ active }))).toBe('session.jumpN')
        expect(shellRow(ev({ key: ']', code: 'BracketRight', altKey: true }), ctx({ active }))).toBe('session.listNext')
      }
      expect(shellRow(ev({ key: 'ArrowUp', code: 'ArrowUp', altKey: true }), ctx({ sessionOpen: true, active: composer })))
        .toBe('transcript.promptPrev')
    })

    it('never fire from any OTHER text entry', () => {
      expect(shellRow(ev({ key: '!', code: 'Digit1', altKey: true, shiftKey: true }), ctx({ active: search }))).toBeUndefined()
      expect(shellRow(ev({ key: '[', code: 'BracketLeft', altKey: true }), ctx({ active: search }))).toBeUndefined()
      expect(shellRow(ev({ key: 'ArrowUp', code: 'ArrowUp', altKey: true }), ctx({ sessionOpen: true, active: search })))
        .toBeUndefined()
    })

    it('are blocked under a modal or an open menu', () => {
      expect(shellRow(ev({ key: ']', code: 'BracketRight', altKey: true }), ctx({ overlayOpen: true }))).toBeUndefined()
      expect(shellRow(ev({ key: '#', code: 'Digit3', altKey: true, shiftKey: true }), ctx({ overlayOpen: true }))).toBeUndefined()
      expect(shellRow(ev({ key: 'ArrowUp', code: 'ArrowUp', altKey: true }), ctx({ sessionOpen: true, overlayOpen: true })))
        .toBeUndefined()
    })

    it('session switching is off in kiosk — a wall display has no list to switch through', () => {
      expect(shellRow(ev({ key: ']', code: 'BracketRight', altKey: true }), ctx({ interactive: false }))).toBeUndefined()
      expect(shellRow(ev({ key: '!', code: 'Digit1', altKey: true, shiftKey: true }), ctx({ interactive: false }))).toBeUndefined()
    })
  })

  describe('on Apple', () => {
    beforeEach(() => setPlatform('MacIntel'))

    it('⌘⌥[ / ⌘⌥] / ⌘⌥1–9 / ⌘⌥↑↓ reach the rows', () => {
      const meta = { metaKey: true, altKey: true }
      expect(shellRow(ev({ key: '“', code: 'BracketLeft', ...meta }), ctx())).toBe('session.listPrev')
      expect(shellRow(ev({ key: '‘', code: 'BracketRight', ...meta }), ctx())).toBe('session.listNext')
      // ⌘⌥5 on a German layout reports "[" — the code is what is matched.
      const e5 = ev({ key: '[', code: 'Digit5', ...meta })
      expect(shellRow(e5, ctx())).toBe('session.jumpN')
      expect(altDigit(e5)).toBe(5)
      expect(shellRow(ev({ key: '¡', code: 'Digit1', ...meta }), ctx())).toBe('session.jumpN')
      expect(shellRow(ev({ key: 'ArrowUp', code: 'ArrowUp', ...meta }), open)).toBe('transcript.promptPrev')
      expect(shellRow(ev({ key: 'ArrowDown', code: 'ArrowDown', ...meta }), open)).toBe('transcript.promptNext')
    })

    it('a bare ⌥ chord is typing, never a binding — German ⌥5 is "[", ⌥↑ moves the caret', () => {
      expect(shellRow(ev({ key: '[', code: 'Digit5', altKey: true }), ctx({ active: composer }))).toBeUndefined()
      expect(shellRow(ev({ key: '{', code: 'Digit8', altKey: true }), ctx({ active: composer }))).toBeUndefined()
      expect(shellRow(ev({ key: '“', code: 'BracketLeft', altKey: true }), ctx({ active: composer }))).toBeUndefined()
      expect(shellRow(ev({ key: 'ArrowUp', code: 'ArrowUp', altKey: true }), ctx({ sessionOpen: true, active: composer })))
        .toBeUndefined()
    })

    it('the Linux digit form and Shift/Ctrl alongside ⌘⌥ reach nothing', () => {
      expect(shellRow(ev({ key: '!', code: 'Digit1', altKey: true, shiftKey: true }), ctx())).toBeUndefined()
      expect(shellRow(ev({ key: ']', code: 'BracketRight', metaKey: true, altKey: true, shiftKey: true }), ctx()))
        .toBeUndefined()
      expect(shellRow(ev({ key: ']', code: 'BracketRight', metaKey: true, altKey: true, ctrlKey: true }), ctx()))
        .toBeUndefined()
    })
  })

  it('the old display-only `compose.jump` is gone — the jump is one row, not two', () => {
    const ids = SHORTCUTS.map((s) => s.id)
    expect(ids).not.toContain('compose.jump')
    expect(SHORTCUTS.filter((s) => /1–9/.test(s.keys)).map((s) => s.id)).toEqual(['session.jumpN'])
  })
})

describe('alt()', () => {
  const altX = alt((e) => e.code === 'KeyX')

  it('REQUIRES Alt and bails on Shift, Ctrl and Meta', () => {
    expect(altX(ev({ key: 'x', code: 'KeyX', altKey: true }))).toBe(true)
    expect(altX(ev({ key: 'x', code: 'KeyX' }))).toBe(false)
    expect(altX(ev({ key: 'X', code: 'KeyX', altKey: true, shiftKey: true }))).toBe(false)
    expect(altX(ev({ key: 'x', code: 'KeyX', altKey: true, ctrlKey: true }))).toBe(false)
    expect(altX(ev({ key: 'x', code: 'KeyX', altKey: true, metaKey: true }))).toBe(false)
  })

  it('is disjoint from plain(), so no Alt chord steals an unmodified key', () => {
    const plainX = plain((e) => e.code === 'KeyX')
    for (const e of [ev({ code: 'KeyX' }), ev({ code: 'KeyX', altKey: true })]) {
      expect(plainX(e) && altX(e)).toBe(false)
    }
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
  // The reachability and reserved-chord cases switch platforms; restore it even
  // when an assertion throws mid-loop, so no later case runs on the wrong one.
  afterEach(() => setPlatform(ORIGINAL_PLATFORM))

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
    // Tab select by number. Alt+1–9 is Chrome's and Firefox's on Linux, which
    // is why the jump row is Alt+SHIFT+1–9 there; Ctrl+1–8 elsewhere, ⌘1–9 on Mac.
    ...Array.from({ length: 9 }, (_, i): [string, KeyboardLike] =>
      [`Alt+${i + 1} (Linux tab select)`, ev({ key: String(i + 1), code: `Digit${i + 1}`, altKey: true })]),
    ...Array.from({ length: 8 }, (_, i): [string, KeyboardLike] =>
      [`Ctrl+${i + 1} (tab select)`, ev({ key: String(i + 1), code: `Digit${i + 1}`, ctrlKey: true })]),
    ...Array.from({ length: 9 }, (_, i): [string, KeyboardLike] =>
      [`⌘${i + 1} (tab select)`, ev({ key: String(i + 1), code: `Digit${i + 1}`, metaKey: true })]),
    // ⌘⌥← / ⌘⌥→ — previous / next tab on macOS Chrome. The prompt jump is
    // ⌘⌥↑/↓ and must never widen to the horizontal arrows.
    ['⌘⌥← (previous tab)', ev({ key: 'ArrowLeft', code: 'ArrowLeft', metaKey: true, altKey: true })],
    ['⌘⌥→ (next tab)', ev({ key: 'ArrowRight', code: 'ArrowRight', metaKey: true, altKey: true })],
  ]

  it.each(RESERVED)('%s reaches no row in the whole registry, on any platform', (label, e) => {
    // Both platforms: the D-10 rows match differently on Apple (⌘⌥) and
    // elsewhere (Alt / Alt+Shift), and a reserved chord must miss both forms.
    for (const platform of ['MacIntel', 'Linux x86_64']) {
      setPlatform(platform)
      // `() => true` for `canAct`: not "the shell has no handler", but "no row
      // anywhere — list, header, compose, in-session — matches this event".
      expect(matchShortcut(e, () => true, anywhere), `${label} on ${platform}`).toBeUndefined()
      // …and it is not selectable by a focused row or group header either.
      expect(matchShortcut(e, () => true), `${label} on ${platform}`).toBeUndefined()
    }
  })

  it('the prompt-row menu chords (Alt+Shift+M / I / E) are not in the browser-reserved set', () => {
    // The set documented at the top of the registry: ⌘/Ctrl + N W T Q, Ctrl(+Shift)+Tab,
    // Ctrl+PageUp/Down, ⌘⇧[ ], plus Ctrl+Shift+I (DevTools) and ⌘⇧M (profile switcher),
    // which are why the menus use Alt+Shift.
    const reserved = /(⌘|Cmd|Ctrl)\s*\+?\s*(Shift\s*\+\s*|⇧\s*)?(N|W|T|Q|I|M|Tab|Page(Up|Down))\b|⌘\s*⇧\s*[[\]]/i
    const rows = SHORTCUTS.filter((s) => ['insession.modeMenu', 'insession.modelMenu', 'insession.effortMenu'].includes(s.id))
    expect(rows).toHaveLength(3)
    for (const s of rows) {
      expect(s.keys, `${s.id} keys`).not.toMatch(reserved)
      expect(s.keysMac ?? '', `${s.id} keysMac`).not.toMatch(reserved)
      expect(s.keys).toMatch(/^Alt\+Shift\+[MIE]$/)
    }
    // Ctrl+Shift+I / ⌘⇧M themselves reach none of them.
    expect(matchShortcut(ev({ key: 'I', code: 'KeyI', ctrlKey: true, shiftKey: true }), () => true, anywhere)).toBeUndefined()
    expect(matchShortcut(ev({ key: 'M', code: 'KeyM', metaKey: true, shiftKey: true }), () => true, anywhere)).toBeUndefined()
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

// ─── D-11: ⌘F / Ctrl+F — the transcript search ──────────────────────────────

describe('transcript.search', () => {
  const pane = document.createElement('div')
  pane.setAttribute(SESSION_TRANSCRIPT_ATTR, '')
  const inPane = document.createElement('div')
  pane.appendChild(inPane)
  const sessionComposer = document.createElement('textarea')
  sessionComposer.setAttribute(SESSION_COMPOSER_ATTR, '')
  const otherInput = document.createElement('input')
  const chromeButton = document.createElement('button')
  const open = (over: Partial<ShortcutContext> = {}) =>
    ctx({ sessionOpen: true, transcriptOpen: true, transcriptSearchFocus: true, ...over })
  const f = (over: Partial<KeyboardLike> = {}) => ev({ key: 'f', code: 'KeyF', ctrlKey: true, ...over })

  it('matches ⌘F and Ctrl+F on the key the layout types, with KeyF only for a non-Latin layout', () => {
    expect(shellRow(f(), open())).toBe('transcript.search')
    expect(shellRow(f({ ctrlKey: false, metaKey: true }), open())).toBe('transcript.search')
    expect(shellRow(f({ key: 'F' }), open())).toBe('transcript.search')
    // Russian layout: the F key reports "а".
    expect(shellRow(f({ key: 'а' }), open())).toBe('transcript.search')
    // Dvorak: the QWERTY F key types "u" — that is not F, whatever its code.
    expect(shellRow(f({ key: 'u' }), open())).toBeUndefined()
    // Dvorak's F lives on the QWERTY Y key.
    expect(shellRow(f({ key: 'f', code: 'KeyY' }), open())).toBe('transcript.search')
  })

  it('leaves ⌘⇧F / Ctrl+Shift+F, a bare F and a double chord alone', () => {
    expect(shellRow(f({ shiftKey: true, key: 'F' }), open())).toBeUndefined()
    expect(shellRow(ev({ key: 'f', code: 'KeyF' }), open())).toBeUndefined()
    expect(shellRow(f({ metaKey: true }), open())).toBeUndefined()
    expect(shellRow(f({ altKey: true }), open())).toBeUndefined()
  })

  it('the focus scope: the transcript, the session composer, or nowhere', () => {
    for (const el of [inPane, pane, sessionComposer, document.body, null]) {
      expect(focusAllowsTranscriptSearch(el), String(el)).toBe(true)
    }
    for (const el of [otherInput, chromeButton]) expect(focusAllowsTranscriptSearch(el)).toBe(false)
  })

  it('fires only when the KEY\'s focus is in scope — the per-event `transcriptSearchFocus`', () => {
    expect(shellRow(f(), open())).toBe('transcript.search')
    // Any other focus: neither matched nor prevented, so the browser's find works.
    expect(shellRow(f(), open({ transcriptSearchFocus: false }))).toBeUndefined()
  })

  it('needs an open session WITH its transcript on screen, and no overlay', () => {
    expect(shellRow(f(), open({ sessionOpen: false }))).toBeUndefined()
    // The Files / Changes tabs unmount the transcript.
    expect(shellRow(f(), open({ transcriptOpen: false }))).toBeUndefined()
    expect(shellRow(f(), open({ overlayOpen: true }))).toBeUndefined()
  })
})
