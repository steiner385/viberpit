import {
  focusAllowsTranscriptSearch, focusInNonPromptTextEntry, focusInOtherTextEntry, isFormControl,
} from '@/lib/detachKeyGuard'

/** An action performed against the focused session row in list mode. */
export type ListRowAction =
  | { type: 'open' }
  | { type: 'continue' }
  | { type: 'rename' }
  | { type: 'archive' }
  | { type: 'permanent-delete' }
  | { type: 'navigate'; dir: 'up' | 'down' }
  | { type: 'exit' } // back to compose (focus the prompt)
  | { type: 'expand' } // group header: open the group
  | { type: 'collapse' } // group header: close the group
  | { type: 'toggle' } // group header: toggle open/closed

export type KeyboardLike = Pick<
  KeyboardEvent,
  'key' | 'code' | 'altKey' | 'ctrlKey' | 'metaKey' | 'shiftKey'
>

export type ShortcutGroup =
  | 'app' | 'compose' | 'list-mode' | 'list-mode-header' | 'in-session' | 'roadmap-inbox'

/**
 * Where a shortcut is allowed to fire. Evaluated against live context on each
 * keydown; the FIRST matching row in `SHORTCUTS` order wins.
 *
 * This is the app's state as a *value*, so the "may this key fire here?" rule
 * lives beside the row that declares the key instead of being re-derived by each
 * listener. The five window/document listeners this registry replaces each
 * answered it their own way — `useGlobalHelpKey` queried
 * `[role="dialog"][aria-modal="true"]`, `useGlobalListNav` queried the looser
 * `[role="dialog"]`, and the two disagreed about whether a docked push-mode
 * `Sheet` blocked a key. One shape, filled once per keydown, is how that stops
 * being possible.
 */
export interface ShortcutContext {
  /** A modal Dialog/Sheet or an open Menu is up. */
  overlayOpen: boolean
  /** A permission or question card is waiting for an answer. */
  hasOpenAction: boolean
  /** A NON-plan permission card is waiting — one ⌘. / Ctrl+. can deny. A plan
   *  step is not a permission to deny, so a plan card alone leaves this false. */
  hasDeniablePermission: boolean
  /** A session is open (`/sessions/:id`). */
  sessionOpen: boolean
  /** The open session has a turn running — the composer's Stop button is showing. */
  sessionBusy: boolean
  /** The Sessions section is current. */
  inSessions: boolean
  /** Kiosk is read-only: nothing that mutates may fire. */
  interactive: boolean
  /** What currently has focus, for text-entry yielding. */
  active: Element | null
  /**
   * The focused text entry claims THIS keydown (`lib/detachKeyGuard`) — the
   * composer's left-edge ← rule included.
   *
   * Key-specific, so it is computed per event rather than being a standing
   * "something is focused" fact: the composer holds ← mid-prompt and releases it
   * at the left edge, with the same caret and the same draft. A `when` predicate
   * only sees the context, so the context is what carries the answer.
   */
  yieldsToTextEntry: boolean
  /** The open session's transcript is on screen — its jump/search handle is
   *  registered (`TranscriptJump`). False on the Files / Changes / Browser / Details
   *  tabs, which unmount it. */
  transcriptOpen: boolean
}

export interface Shortcut {
  id: string
  /** Display string for the help panel. Use `keysMac` when the platform binding
   *  differs; `displayKeys()` picks between them. */
  keys: string
  keysMac?: string
  description: string
  group: ShortcutGroup
  /** Present ⇒ wired into the SessionCard list-mode handler. */
  action?: ListRowAction
  match?: (e: KeyboardLike) => boolean
  /** Omitted ⇒ always allowed. Read through `shortcutAllowed()`. */
  when?: (c: ShortcutContext) => boolean
}

/** Unmodified key predicate — bails if Ctrl/Alt/Meta is held (Shift allowed). */
export function plain(pred: (e: KeyboardLike) => boolean) {
  return (e: KeyboardLike): boolean => !e.ctrlKey && !e.altKey && !e.metaKey && pred(e)
}

/**
 * Cmd/Win-chord predicate — REQUIRES Meta, bails on Ctrl/Alt (Shift allowed).
 *
 * Deliberately a sibling of `plain()` rather than a loosening of it. `plain()`
 * bails on Meta, which is the only reason `Cmd+A` still selects text in the
 * prompt instead of archiving the focused session, and `Cmd+C` copies instead of
 * continuing it. A row that wants a Cmd chord opts in here; the list-mode rows
 * keep bailing.
 */
export function meta(pred: (e: KeyboardLike) => boolean) {
  return (e: KeyboardLike): boolean => e.metaKey && !e.ctrlKey && !e.altKey && pred(e)
}

/** Ctrl-chord predicate — REQUIRES Ctrl, bails on Alt/Meta (Shift allowed). */
export function ctrl(pred: (e: KeyboardLike) => boolean) {
  return (e: KeyboardLike): boolean => e.ctrlKey && !e.metaKey && !e.altKey && pred(e)
}

/**
 * The platform's primary chord modifier: ⌘ on Apple, Ctrl everywhere else.
 *
 * Composed from `meta()` and `ctrl()` rather than re-spelling the modifier test
 * a third time. That matters more than it looks: a hand-rolled
 * `(e.metaKey || e.ctrlKey) && !e.altKey` would accept ⌘+Ctrl+/ as well, and
 * would be a fourth private answer to "which modifiers disqualify a chord"
 * alongside `plain()`, `meta()` and `insession.forceDetach`'s old inline test
 * (now routed through `ctrl()`).
 *
 * Deliberately NOT platform-sniffed. `isApplePlatform()` decides what the help
 * sheet PRINTS; it must not decide what fires, because a Mac user on an external
 * PC keyboard, a Linux box with ⌘ mapped to Super, and a remote-desktop session
 * all report platforms that disagree with the keyboard in front of the user. A
 * binding gated on the sniff would be dead for them with no way to tell why.
 * Accepting either modifier costs nothing: no row here also wants the other one.
 */
export function cmdOrCtrl(pred: (e: KeyboardLike) => boolean) {
  const onMeta = meta(pred)
  const onCtrl = ctrl(pred)
  return (e: KeyboardLike): boolean => onMeta(e) || onCtrl(e)
}

/**
 * Bare-Alt chord predicate — REQUIRES Alt, bails on Shift/Ctrl/Meta.
 *
 * Unlike its siblings it bails on Shift too: Alt+Shift+letter is the prompt-row
 * menus' family, a different chord. Rows built on it match on `e.code`, never
 * `e.key` — macOS Option rewrites the key (⌥1 reports "¡", ⌥[ reports "“"), so
 * a key match would leave the binding dead on every Mac.
 */
export function alt(pred: (e: KeyboardLike) => boolean) {
  return (e: KeyboardLike): boolean => e.altKey && !e.shiftKey && !e.ctrlKey && !e.metaKey && pred(e)
}

/**
 * The D-10 navigation chord: ⌘⌥ on Apple, Alt (or Alt+Shift) elsewhere —
 * Meta+Alt REQUIRED on Apple, bailing on Shift/Ctrl; off Apple `alt()`, or
 * Alt+Shift with no Meta/Ctrl when `shiftElsewhere`.
 *
 * Platform-sniffed, unlike `cmdOrCtrl`, because neither form works on the
 * other platform. On a Mac a bare ⌥ chord is TYPING: non-US layouts put
 * brackets and braces on ⌥5/6/8/9 (German ⌥5 is "["), US ⌥[ is "“", and ⌥↑/↓
 * move the caret in a textarea — a binding there would eat text. ⌘⌥ types
 * nothing. Off Apple, ⌘ is the Super key, which window managers own.
 *
 * `shiftElsewhere` is the digits': Chrome and Firefox on Linux select tabs
 * with bare Alt+1–9, so the jump takes Alt+Shift+1–9 there rather than break
 * tab switching (the same rule that keeps ⌘⇧[ ] unbound).
 */
export function navChord(pred: (e: KeyboardLike) => boolean, { shiftElsewhere = false } = {}) {
  const elsewhere = shiftElsewhere
    ? (e: KeyboardLike): boolean => e.altKey && e.shiftKey && !e.ctrlKey && !e.metaKey && pred(e)
    : alt(pred)
  return (e: KeyboardLike): boolean =>
    isApplePlatform()
      ? e.metaKey && e.altKey && !e.shiftKey && !e.ctrlKey && pred(e)
      : elsewhere(e)
}

/** The 1–9 of a digit chord, read from the physical key (`Digit1`…`Digit9`); else null. */
export function altDigit(e: KeyboardLike): number | null {
  const m = /^Digit([1-9])$/.exec(e.code)
  return m ? Number(m[1]) : null
}

/**
 * The navigation chords fire from the app's prompts — the session composer and
 * the new-session prompt both leave Alt chords unclaimed — and from the chrome,
 * but never from any other text entry, which owns its own keys.
 */
const altChordAllowed = (c: ShortcutContext): boolean =>
  !c.overlayOpen && !focusInNonPromptTextEntry(c.active)

/** True on Apple platforms, where the help sheet shows `keysMac`. */
export function isApplePlatform(): boolean {
  if (typeof navigator === 'undefined') return false
  return /Mac|iPhone|iPad|iPod/.test(navigator.platform ?? '')
}

/** The display string for `s` on the platform this browser is running on. */
export function displayKeys(s: Shortcut): string {
  return isApplePlatform() && s.keysMac ? s.keysMac : s.keys
}

/**
 * Whether `s` may fire in `c`. A row with no `when` is always allowed.
 *
 * The default lives here rather than at each call site so a dispatcher cannot
 * spell it `!s.when(c)` and silently make every unconditional row unreachable.
 */
export function shortcutAllowed(s: Shortcut, c: ShortcutContext): boolean {
  return s.when?.(c) ?? true
}

export const SHORTCUTS: Shortcut[] = [
  // ── App-global chords (⌘ on Apple, Ctrl elsewhere) — WIRED ───────────
  //
  // Unlike every other row here, these fire with a text entry focused: a
  // ⌘/Ctrl chord is not a character, so there is nothing for the composer to
  // claim. `compose.help` below stays a SEPARATE row for exactly that reason —
  // `?` IS a character and must yield to a focused field, and one row cannot
  // carry two `when`s. Both open the same drawer; the guard is what differs.
  //
  // Both are blocked in kiosk: `REGION_VISIBILITY['app.drawer']` is false there,
  // so a wall display's keyboard would otherwise fire a handler with no region
  // to open into.
  { id: 'app.help', keys: 'Ctrl+/', keysMac: '⌘/',
    description: 'Open this help panel (works while typing)', group: 'app',
    match: cmdOrCtrl((e) => e.key === '/'),
    when: (c) => c.interactive && !c.overlayOpen },
  { id: 'app.assistant', keys: 'Ctrl+;', keysMac: '⌘;',
    description: 'Open the assistant drawer', group: 'app',
    match: cmdOrCtrl((e) => e.key === ';'),
    when: (c) => c.interactive && !c.overlayOpen },
  // The ⌘K palette (D-10): sessions, actions, sections, settings, repos and
  // branches. On `e.key` — the K the layout types (Dvorak's sits on the QWERTY
  // V key) — falling back to the physical `KeyK` only when the layout reports
  // no Latin letter (Cyrillic, Greek …). Shift is excluded because
  // Ctrl+Shift+K is Firefox's web console. Ctrl+K is Chrome's
  // "search the web" on Linux/Windows, but it is not browser-reserved — the
  // keydown reaches the page and the dispatcher's preventDefault keeps it.
  // Blocked under an overlay like its siblings: the open palette IS one, and
  // closes itself on this same row's `match` (`features/palette/AppPalette`).
  // Kiosk is excluded — `ui/Dialog` refuses modals there.
  { id: 'app.palette', keys: 'Ctrl+K', keysMac: '⌘K',
    description: 'Search sessions, actions and settings (works while typing)', group: 'app',
    match: cmdOrCtrl((e) => !e.shiftKey
      && (e.key === 'k' || e.key === 'K' || (!/^[a-z]$/i.test(e.key) && e.code === 'KeyK'))),
    when: (c) => c.interactive && !c.overlayOpen },
  //
  // ── Session switching (D-10) — WIRED ─────────────────────────────────
  //
  // Previous / next / Nth session in the LIST's order — the shell's
  // `useSessionListState().ordering.orderedSessions`, filters and search
  // applied, the order the session header's ‹ › chevrons step through too
  // (`shell/sessionListNav`). They wrap; with no session open, next opens the
  // first row and previous the last. ⌘⌥ on Apple, Alt / Alt+Shift elsewhere
  // (`navChord`): the ⌘/Ctrl next-tab family below is the browser's, a bare ⌥
  // is typing on a Mac, and bare Alt+1–9 is Linux's tab select. Kiosk has no
  // list to switch through.
  { id: 'session.listPrev', keys: 'Alt+[', keysMac: '⌘⌥[',
    description: 'Open the previous session in the list', group: 'app',
    match: navChord((e) => e.code === 'BracketLeft'),
    when: (c) => c.interactive && altChordAllowed(c) },
  { id: 'session.listNext', keys: 'Alt+]', keysMac: '⌘⌥]',
    description: 'Open the next session in the list', group: 'app',
    match: navChord((e) => e.code === 'BracketRight'),
    when: (c) => c.interactive && altChordAllowed(c) },
  // One row for all nine digits: the help sheet shows one line, and the handler
  // reads the digit back through `altDigit`.
  { id: 'session.jumpN', keys: 'Alt+Shift+1–9', keysMac: '⌘⌥1–9',
    description: 'Open the Nth session in the list', group: 'app',
    match: navChord((e) => altDigit(e) !== null, { shiftElsewhere: true }),
    when: (c) => c.interactive && altChordAllowed(c) },
  //
  // ── NOT BOUND, and why — the Desktop-parity rows this app cannot have ─
  //
  // Claude Code *Desktop* is Electron and owns its window, so it can bind ⌘N,
  // ⌘W and Ctrl+Tab. A web page cannot. Chrome and Safari do not DISPATCH the
  // keydown for a shortcut their own UI reserves — there is no event to call
  // `preventDefault()` on — and the deliberate reason is that a page which
  // could swallow them would be able to trap a keyboard-only user inside
  // itself. The enumerated reserved sets (w3c/uievents#7, Masayuki Nakano,
  // 2016-01: lists.w3.org/Archives/Public/public-webapps-github/2016Jan/0255):
  //
  //   Chrome + Safari, macOS : ⌘N ⌘W ⌘Q ⌘T Ctrl+Tab Ctrl+Shift+Tab
  //   Chrome, Windows        : Ctrl+N Ctrl+W Ctrl+T Ctrl+Tab Ctrl+Shift+Tab
  //                            Ctrl+PageUp Ctrl+PageDown
  //
  // So these four rows are dropped rather than shipped:
  //
  //   · `app.newSession`  ⌘N / Ctrl+N       — reserved: NEW WINDOW.
  //   · `session.close`   ⌘W / Ctrl+W       — reserved: CLOSE TAB. On macOS it
  //     is also the ⌘W menu item, handled above the web content entirely.
  //   · `session.next`    Ctrl+Tab          — reserved: NEXT TAB.
  //   · `session.prev`    Ctrl+Shift+Tab    — reserved: PREVIOUS TAB.
  //
  // The prompt-row menu chords stay clear of the browser too: Ctrl+Shift+I is
  // DevTools and ⌘⇧M the Chrome profile switcher, so the menus use Alt+Shift+I /
  // Alt+Shift+M, which neither Chrome nor Safari reserves.
  //
  // The help sheet renders from this array, so a row here is a PROMISE that the
  // key does something. Shipping a promise the browser silently eats is worse
  // than the missing shortcut: the user presses it, a tab closes, and the sheet
  // still says it detaches a session.
  //
  // The spec's fallback for next/previous — ⌘⇧] / ⌘⇧[ — is no better. Those are
  // Safari's and Chrome's own next/previous-tab bindings on macOS (Apple's
  // Safari shortcut reference, support.apple.com/guide/safari/cpsh003), so even
  // where the page CAN preventDefault them, binding them would break tab
  // switching for anyone with the app open. No ⌘/Ctrl binding stands in for
  // them: next/previous session is Alt+] / Alt+[ (⌘⌥] / ⌘⌥[ on Apple —
  // `session.listNext` / `session.listPrev` above, D-10), which no browser
  // reserves — under ids of their own, so `session.next` / `session.prev` stay
  // absent. Tab-select-by-number (Alt+1–9 on Linux, Ctrl+1–8, ⌘1–9) is left
  // alone the same way: the jump is Alt+Shift+1–9 / ⌘⌥1–9.
  //
  // Consequences that outlive this comment:
  //   · The cycling order is `useSessionListState().ordering.orderedSessions`,
  //     the same order the list renders, so cycling never navigates to a
  //     filtered-out session.
  //   · Starting a session is still reachable without a chord: the New Session
  //     button at the foot of the list column clears the open session, which
  //     puts the dispatch bar in `app.dock`. Detach is still ← (and Esc while idle).
  // Pinned by "the un-bindable Desktop chords bind nothing" in shortcuts.test.ts.
  //
  // ── List mode — WIRED ────────────────────────────────────────────────
  { id: 'list.open', keys: 'Enter / → / Space', description: 'Open the focused session',
    group: 'list-mode', action: { type: 'open' },
    match: plain((e) => e.key === 'Enter' || e.key === ' ' || e.key === 'ArrowRight') },
  { id: 'list.continue', keys: 'c', description: 'Continue (resume) the focused session',
    group: 'list-mode', action: { type: 'continue' }, match: plain((e) => e.key === 'c' || e.key === 'C') },
  { id: 'list.rename', keys: 'r', description: 'Rename the focused session',
    group: 'list-mode', action: { type: 'rename' }, match: plain((e) => e.key === 'r' || e.key === 'R') },
  { id: 'list.archive', keys: 'a / Backspace', description: 'Archive the focused session',
    group: 'list-mode', action: { type: 'archive' },
    match: plain((e) => e.key === 'a' || e.key === 'A' || e.key === 'Backspace') },
  { id: 'list.delete', keys: 'd / Delete', description: 'Delete the focused session (confirm)',
    group: 'list-mode', action: { type: 'permanent-delete' },
    match: plain((e) => e.key === 'd' || e.key === 'D' || e.key === 'Delete') },
  { id: 'list.up', keys: '↑', description: 'Move to the previous row',
    group: 'list-mode', action: { type: 'navigate', dir: 'up' }, match: plain((e) => e.key === 'ArrowUp') },
  { id: 'list.down', keys: '↓', description: 'Move to the next row',
    group: 'list-mode', action: { type: 'navigate', dir: 'down' }, match: plain((e) => e.key === 'ArrowDown') },
  { id: 'list.exit', keys: 'Esc / ← / Tab', description: 'Return to the prompt (compose)',
    group: 'list-mode', action: { type: 'exit' },
    match: plain((e) => e.key === 'Escape' || e.key === 'ArrowLeft' || e.key === 'Tab') },
  // ── List mode, group header focused — WIRED ──────────────────────────
  { id: 'header.expand', keys: '→', description: 'Expand the focused group',
    group: 'list-mode-header', action: { type: 'expand' }, match: plain((e) => e.key === 'ArrowRight') },
  { id: 'header.collapse', keys: '←', description: 'Collapse the focused group',
    group: 'list-mode-header', action: { type: 'collapse' }, match: plain((e) => e.key === 'ArrowLeft') },
  { id: 'header.toggle', keys: 'Enter / Space', description: 'Toggle the focused group',
    group: 'list-mode-header', action: { type: 'toggle' },
    match: plain((e) => e.key === 'Enter' || e.key === ' ') },
  { id: 'header.up', keys: '↑', description: 'Move to the previous row',
    group: 'list-mode-header', action: { type: 'navigate', dir: 'up' }, match: plain((e) => e.key === 'ArrowUp') },
  { id: 'header.down', keys: '↓', description: 'Move to the next row',
    group: 'list-mode-header', action: { type: 'navigate', dir: 'down' }, match: plain((e) => e.key === 'ArrowDown') },
  { id: 'header.exit', keys: 'Esc / Tab', description: 'Return to the prompt (compose)',
    group: 'list-mode-header', action: { type: 'exit' },
    match: plain((e) => e.key === 'Escape' || e.key === 'Tab') },
  // ── Compose mode — DISPLAY-ONLY (handled in DispatchBar) ──────────────
  { id: 'compose.enterList', keys: 'Tab', description: 'Enter list mode (empty prompt)', group: 'compose' },
  // ↑/↓ move the list highlight when focus is parked in the chrome rather than in
  // the prompt (`DispatchBar` owns the arrows while the prompt has focus, and a
  // focused row owns them through `list.up`/`list.down` above — both call
  // `preventDefault`, which the dispatcher yields to).
  { id: 'compose.nav', keys: '↑ / ↓', description: 'Move the highlight', group: 'compose',
    match: plain((e) => e.key === 'ArrowUp' || e.key === 'ArrowDown'),
    when: (c) => c.interactive && c.inSessions && !c.overlayOpen && !isFormControl(c.active) },
  { id: 'compose.create', keys: 'Enter', description: 'Create a session from the prompt', group: 'compose' },
  { id: 'compose.open', keys: '→ / Enter', description: 'Open highlighted session (empty prompt)', group: 'compose' },
  { id: 'compose.newline', keys: 'Shift+Enter', description: 'Insert a newline', group: 'compose' },
  { id: 'compose.deselect', keys: 'Esc', description: 'Clear the prompt and deselect', group: 'compose' },
  // `?` works from anywhere in the chrome, not just the prompt (`DispatchBar`
  // keeps its own empty-prompt branch, so `?` still types a character mid-draft).
  // `Shift+/` as well as `?`: a browser reports one or the other depending on the
  // layout, and only matching `?` makes the key dead on the layouts that report
  // `/`. Blocked in kiosk, which has no drawer region to open into.
  { id: 'compose.help', keys: '?', description: 'Open this help panel', group: 'compose',
    match: plain((e) => e.key === '?' || (e.key === '/' && e.shiftKey)),
    when: (c) => c.interactive && !c.overlayOpen && !isFormControl(c.active) },
  // ── Roadmap Inbox scope — WIRED, but by the scope, not by the shell ──
  //
  // These fire from `features/roadmap/inbox/InboxScopeBody`'s own window
  // listener, which resolves them through `matchShortcut(e, inGroup(
  // 'roadmap-inbox'))`. They are registered HERE anyway, for two reasons that
  // pull in the same direction:
  //
  //   · the help sheet (`?`) renders from this array and nothing else, so a key
  //     map that is not here is a key map no user can discover — the gap #1494
  //     reports. The Kbd legend in the Inbox's controls strip is a reminder for
  //     somebody already looking at the queue, not documentation;
  //   · one owner for "which key does what". Before this, the switch statement
  //     in the scope was the only place `x` meant select, and the legend beside
  //     it was a second, hand-kept copy free to disagree with it.
  //
  // They stay OUT of the shell's dispatcher regardless: it can only fire a row
  // with a handler registered under its id (`useShortcuts`), and `AppShellInner`
  // registers none of these. That is deliberate and not an oversight — `a` and
  // `r` are irreversible past the undo window, and must not survive the scope
  // that owns the queue they act on. The scope keeps its own guards for the same
  // reason (`overlayIsOpen`, `isTypingTarget`), which is why no row here carries
  // a `when`: a context this array cannot see is not a context it should judge.
  //
  // `a` and `A` are separate rows matching the exact `key`, not one row with a
  // case-insensitive test the way `list.archive` has: Shift+a is a DIFFERENT
  // action here (accept the whole selection), and folding them together would
  // make one keystroke do the other's work.
  { id: 'inbox.next', keys: 'j', description: 'Move to the next proposal',
    group: 'roadmap-inbox', match: plain((e) => e.key === 'j') },
  { id: 'inbox.prev', keys: 'k', description: 'Move to the previous proposal',
    group: 'roadmap-inbox', match: plain((e) => e.key === 'k') },
  { id: 'inbox.accept', keys: 'a', description: 'Accept the focused proposal (a decision opens instead)',
    group: 'roadmap-inbox', match: plain((e) => e.key === 'a') },
  { id: 'inbox.reject', keys: 'r', description: 'Reject the focused proposal — park it for its owner (a decision opens instead)',
    group: 'roadmap-inbox', match: plain((e) => e.key === 'r') },
  { id: 'inbox.dismiss', keys: 'd', description: 'Dismiss the focused proposal — not roadmap work, no hold',
    group: 'roadmap-inbox', match: plain((e) => e.key === 'd') },
  { id: 'inbox.edit', keys: 'e', description: 'Open the focused proposal with its scores focused',
    group: 'roadmap-inbox', match: plain((e) => e.key === 'e') },
  { id: 'inbox.select', keys: 'x', description: 'Tick the focused proposal for a bulk accept or reject',
    group: 'roadmap-inbox', match: plain((e) => e.key === 'x') },
  { id: 'inbox.acceptSelected', keys: 'Shift+A', description: 'Accept every ticked proposal',
    group: 'roadmap-inbox', match: plain((e) => e.key === 'A') },
  // Same shape as `a`/`A`: Shift+r sweeps the selection, `r` decides one row.
  { id: 'inbox.rejectSelected', keys: 'Shift+R', description: 'Reject every ticked proposal',
    group: 'roadmap-inbox', match: plain((e) => e.key === 'R') },
  { id: 'inbox.dismissSelected', keys: 'Shift+D', description: 'Dismiss every ticked proposal',
    group: 'roadmap-inbox', match: plain((e) => e.key === 'D') },
  // ── In-session ───────────────────────────────────────────────────────
  //
  // `hasOpenAction` holds ← and Esc — an unanswered permission or question card
  // is answered first — but deliberately NOT Ctrl+Z, which is the force-detach
  // escape hatch that keeps the user from being trapped by a card.
  //
  // Esc is split by `sessionBusy` (Desktop parity, sub-spec D §3): while a turn
  // runs Esc STOPS it; while idle it detaches. ← always detaches. The two Esc rows
  // carry mutually exclusive guards, so which one wins never depends on order.
  //
  // `insession.stop` deliberately does NOT yield to the session composer's
  // draft: Desktop stops regardless of what is typed, and the draft stays put.
  // It never fires from any OTHER text entry (the list search, a rename field,
  // the assistant composer …), empty or not — keyed on the focused element, not
  // `yieldsToTextEntry`, which an empty field leaves false — because stopping a
  // turn from there would be a destructive surprise. It also yields to anything
  // that consumed Esc first: the slash palette and mention menu `preventDefault`
  // it, and the dispatcher skips a prevented event.
  { id: 'insession.stop', keys: 'Esc', description: 'Stop the running turn',
    group: 'in-session',
    match: plain((e) => e.key === 'Escape'),
    // `interactive`: stopping a turn mutates, and kiosk is read-only.
    when: (c) => c.interactive && c.sessionOpen && c.sessionBusy && !c.overlayOpen && !c.hasOpenAction
      && !focusInOtherTextEntry(c.active) },
  { id: 'insession.detach', keys: '←', description: 'Detach back to the session list',
    group: 'in-session',
    match: plain((e) => e.key === 'ArrowLeft'),
    when: (c) => c.sessionOpen && !c.overlayOpen && !c.hasOpenAction && !c.yieldsToTextEntry },
  { id: 'insession.detachEsc', keys: 'Esc', description: 'Detach back to the session list (no turn running)',
    group: 'in-session',
    match: plain((e) => e.key === 'Escape'),
    when: (c) => c.sessionOpen && !c.sessionBusy && !c.overlayOpen && !c.hasOpenAction && !c.yieldsToTextEntry },
  { id: 'insession.forceDetach', keys: 'Ctrl+Z', description: 'Force-detach to the list',
    group: 'in-session',
    // `ctrl()`, not an inline modifier test: this row spelled its own
    // `e.ctrlKey && !e.altKey && !e.metaKey` until `cmdOrCtrl` needed the same
    // rule, at which point the inline copy became the second answer to a
    // question the helpers own.
    match: ctrl((e) => e.key === 'z' || e.key === 'Z'),
    when: (c) => c.sessionOpen && !c.overlayOpen && !c.yieldsToTextEntry },
  // Prompt-row menus (Desktop parity, sub-spec D §3.1). Alt+Shift+letter, matched
  // on `e.code` because macOS Option rewrites `e.key` (⌥⇧M is "Â", not "M"). They
  // fire while typing — a chord is not a character — and `Alt+Shift` leaves
  // Ctrl+Shift+I (DevTools) and ⌘⇧M (Chrome profile switcher) alone.
  { id: 'insession.modeMenu', keys: 'Alt+Shift+M', keysMac: '⌥⇧M', description: 'Open the permission-mode menu',
    group: 'in-session',
    match: (e) => e.altKey && e.shiftKey && !e.ctrlKey && !e.metaKey && e.code === 'KeyM',
    when: (c) => c.sessionOpen && c.interactive && !c.overlayOpen },
  { id: 'insession.modelMenu', keys: 'Alt+Shift+I', keysMac: '⌥⇧I', description: 'Open the model menu',
    group: 'in-session',
    match: (e) => e.altKey && e.shiftKey && !e.ctrlKey && !e.metaKey && e.code === 'KeyI',
    when: (c) => c.sessionOpen && c.interactive && !c.overlayOpen },
  { id: 'insession.effortMenu', keys: 'Alt+Shift+E', keysMac: '⌥⇧E', description: 'Open the reasoning-effort menu (when the model has one)',
    group: 'in-session',
    match: (e) => e.altKey && e.shiftKey && !e.ctrlKey && !e.metaKey && e.code === 'KeyE',
    when: (c) => c.sessionOpen && c.interactive && !c.overlayOpen },
  // Desktop's ⌘. — deny the OLDEST pending permission card. A chord, not a
  // character, so it fires with the composer focused (no `yieldsToTextEntry`).
  // Plan cards are skipped (`hasDeniablePermission` excludes them); with nothing to
  // deny the row does not match, so the key is left unclaimed.
  { id: 'insession.denyPermission', keys: 'Ctrl+.', keysMac: '⌘.',
    description: 'Deny the oldest pending permission request', group: 'in-session',
    match: cmdOrCtrl((e) => e.key === '.'),
    when: (c) => c.sessionOpen && c.interactive && !c.overlayOpen && c.hasDeniablePermission },
  // Transcript view mode (sub-spec D §4): Normal → Thinking → Verbose → Normal.
  // Fires while typing in the session composer — a chord is not a character —
  // but never from any OTHER text entry (the list search, the docked assistant
  // composer …), the same focused-element rule as `insession.stop`. Ctrl+O / ⌘O
  // (the browser's "open file") is not browser-reserved, so the dispatcher's
  // preventDefault keeps the file picker shut. Shift is excluded: Ctrl/⌘+Shift+O
  // is Chrome's Bookmark Manager and must reach the browser untouched.
  { id: 'insession.cycleView', keys: 'Ctrl+O', keysMac: '⌘O', description: 'Cycle the transcript view (Normal, Thinking, Verbose)',
    group: 'in-session',
    match: cmdOrCtrl((e) => !e.shiftKey && (e.key === 'o' || e.key === 'O')),
    when: (c) => c.sessionOpen && c.interactive && !c.overlayOpen && !focusInOtherTextEntry(c.active) },
  // Prompt jumps (D-10): scroll the transcript to the previous / next prompt the
  // user typed (`role === 'user' && !origin` — nudges are skipped), relative to
  // the topmost visible row. They unpin stick-to-bottom and move no focus, so
  // they work mid-draft: the composer leaves Alt chords unclaimed for exactly
  // this. Read-only, so kiosk is not excluded. ⌘⌥↑/↓ on Apple — a bare ⌥↑/↓ moves
  // the caret there; ⌘⌥↑/↓ shadows Chrome Mac's pane focus, which the page may
  // take. NEVER ⌘⌥← / ⌘⌥→: those are macOS Chrome's previous / next tab.
  { id: 'transcript.promptPrev', keys: 'Alt+↑', keysMac: '⌘⌥↑', description: 'Scroll to your previous prompt',
    group: 'in-session',
    match: navChord((e) => e.code === 'ArrowUp'),
    when: (c) => c.sessionOpen && altChordAllowed(c) },
  { id: 'transcript.promptNext', keys: 'Alt+↓', keysMac: '⌘⌥↓', description: 'Scroll to your next prompt',
    group: 'in-session',
    match: navChord((e) => e.code === 'ArrowDown'),
    when: (c) => c.sessionOpen && altChordAllowed(c) },
  // ⌘F / Ctrl+F (D-11): search this transcript. On `e.key` like ⌘K, falling
  // back to the physical `KeyF` only when the layout reports no Latin letter.
  // It takes the browser's find ONLY from the transcript, the session composer or
  // nowhere (`focusAllowsTranscriptSearch`) — from any other field, the list or
  // the chrome it neither matches nor prevents, so the browser's find still
  // works there. Shift is excluded: ⌘⇧F is the browser's / OS's own. Read-only,
  // so kiosk is not excluded. While the bar is open, the same row refocuses and
  // selects its query.
  { id: 'transcript.search', keys: 'Ctrl+F', keysMac: '⌘F', description: 'Search this conversation',
    group: 'in-session',
    match: cmdOrCtrl((e) => !e.shiftKey
      && (e.key === 'f' || e.key === 'F' || (!/^[a-z]$/i.test(e.key) && e.code === 'KeyF'))),
    when: (c) => c.sessionOpen && c.transcriptOpen && !c.overlayOpen && focusAllowsTranscriptSearch(c.active) },
  // NOT here: ⌘W / Ctrl+W (detach), Ctrl+Tab / Ctrl+Shift+Tab (next/previous
  // session — that is Alt+[ / Alt+] / ⌘⌥[ ] above). All three are reserved by the
  // browser's own UI — see the "NOT BOUND, and why" block at the top of this
  // array for the evidence.
  // `Shift+Tab` (cycle permission mode) is deliberately NOT listed: sub-spec B D8
  // removes the binding, and the handler that carried it (`useInSessionKeys`, now
  // deleted) is gone — the AppShell cutover made a window-level
  // `preventDefault()` on Shift+Tab
  // untenable (it killed reverse-tab app-wide and escalated a live session's
  // permission mode), so D8's removal was brought forward into this PR. The
  // registry is the only thing the help sheets render from, so dropping the row
  // here is how the documentation goes away — a `HelpSheet`-local filter would be
  // a second, invisible owner of "which shortcuts are documented".
]

/**
 * THE selection rule, and the only one: the FIRST row in registry order whose
 * `match` accepts the event, which the caller can act on (`canAct`), and whose
 * `when` allows the context.
 *
 * ## Why one function and not three
 *
 * There used to be `matchListShortcut` (rows `!== 'list-mode-header'`) and
 * `matchHeaderShortcut` (rows `=== 'list-mode-header'`), each with its own
 * `find` and neither aware of `when`. The shell's dispatcher would have been a
 * third rule — "first matching row, subject to `when`" — over the same array, so
 * the registry would have had three different answers to "which row wins".
 * `canAct` is the only difference that was ever real: a focused group header must
 * not run a session-row action, and the shell must not fire a row it has no
 * handler for. Everything else — the ordering, and now `when` — is shared.
 *
 * That collapse also fixed a latent bug: `!== 'list-mode-header'` made every
 * compose and in-session row a candidate for a focused list row, which was
 * harmless only while none of them had a `match`. Four of them do now.
 *
 * ## Why `c` is optional
 *
 * A focused row's own `onKeyDown` is the one caller with no honest context: it
 * cannot answer `hasOpenAction` or `sessionOpen`, and fabricating them is how a
 * `when` comes to be evaluated against a lie. Omitting `c` means "no context to
 * test", and a row carrying a `when` is then simply not selectable — so the
 * failure mode of adding a `when` to a list-mode row is that the row stops
 * firing there and its test fails, not that it fires under a guess.
 */
export function matchShortcut(
  e: KeyboardLike,
  canAct: (s: Shortcut) => boolean,
  c?: ShortcutContext,
): Shortcut | undefined {
  return SHORTCUTS.find(
    (s) =>
      canAct(s) &&
      s.match?.(e) === true &&
      (c !== undefined ? shortcutAllowed(s, c) : s.when === undefined),
  )
}

/** `canAct` for a focused list row / group header (`SessionCard`, `GroupHeader`). */
export function inGroup(group: ShortcutGroup): (s: Shortcut) => boolean {
  return (s) => s.group === group
}

export function shortcutsByGroup(group: ShortcutGroup): Shortcut[] {
  return SHORTCUTS.filter((s) => s.group === group)
}
