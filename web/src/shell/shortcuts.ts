import { isFormControl } from '@/lib/detachKeyGuard'

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

export type ShortcutGroup = 'app' | 'compose' | 'list-mode' | 'list-mode-header' | 'in-session'

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
  /** A session is open (`/sessions/:id`). */
  sessionOpen: boolean
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
  // The help sheet renders from this array, so a row here is a PROMISE that the
  // key does something. Shipping a promise the browser silently eats is worse
  // than the missing shortcut: the user presses it, a tab closes, and the sheet
  // still says it detaches a session.
  //
  // The spec's fallback for next/previous — ⌘⇧] / ⌘⇧[ — is no better. Those are
  // Safari's and Chrome's own next/previous-tab bindings on macOS (Apple's
  // Safari shortcut reference, support.apple.com/guide/safari/cpsh003), so even
  // where the page CAN preventDefault them, binding them would break tab
  // switching for anyone with the app open. **No replacement binding is
  // invented here.** Next/previous-session has no keyboard binding in this PR,
  // and picking one is a human decision, not this task's.
  //
  // Consequences that outlive this comment:
  //   · Ruling R2 (Task 8 must re-check the session-cycling handler once list
  //     filters land) has NOTHING to re-check — no such handler exists. If a
  //     future PR adds one, its order must come from
  //     `useSessionListState().ordering.orderedSessions`, the same order the
  //     list renders, or cycling will navigate to a filtered-out session.
  //   · `?dialog=new` is still reachable without a chord: the rail's New button
  //     is a `<Link>` to `/sessions?dialog=new`, and detach is still Esc / ←.
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
  { id: 'compose.jump', keys: 'Alt+1–9', description: 'Open session by number', group: 'compose' },
  { id: 'compose.deselect', keys: 'Esc', description: 'Clear the prompt and deselect', group: 'compose' },
  // `?` works from anywhere in the chrome, not just the prompt (`DispatchBar`
  // keeps its own empty-prompt branch, so `?` still types a character mid-draft).
  // `Shift+/` as well as `?`: a browser reports one or the other depending on the
  // layout, and only matching `?` makes the key dead on the layouts that report
  // `/`. Blocked in kiosk, which has no drawer region to open into.
  { id: 'compose.help', keys: '?', description: 'Open this help panel', group: 'compose',
    match: plain((e) => e.key === '?' || (e.key === '/' && e.shiftKey)),
    when: (c) => c.interactive && !c.overlayOpen && !isFormControl(c.active) },
  // ── In-session ───────────────────────────────────────────────────────
  //
  // `hasOpenAction` holds ← and Esc — an unanswered permission or question card
  // is answered first — but deliberately NOT Ctrl+Z, which is the force-detach
  // escape hatch that keeps the user from being trapped by a card.
  { id: 'insession.detach', keys: '← / Esc', description: 'Detach back to the session list',
    group: 'in-session',
    match: plain((e) => e.key === 'Escape' || e.key === 'ArrowLeft'),
    when: (c) => c.sessionOpen && !c.overlayOpen && !c.hasOpenAction && !c.yieldsToTextEntry },
  { id: 'insession.forceDetach', keys: 'Ctrl+Z', description: 'Force-detach to the list',
    group: 'in-session',
    // `ctrl()`, not an inline modifier test: this row spelled its own
    // `e.ctrlKey && !e.altKey && !e.metaKey` until `cmdOrCtrl` needed the same
    // rule, at which point the inline copy became the second answer to a
    // question the helpers own.
    match: ctrl((e) => e.key === 'z' || e.key === 'Z'),
    when: (c) => c.sessionOpen && !c.overlayOpen && !c.yieldsToTextEntry },
  // NOT here: ⌘W / Ctrl+W (detach), Ctrl+Tab / Ctrl+Shift+Tab (next/previous
  // session). All three are reserved by the browser's own UI — see the
  // "NOT BOUND, and why" block at the top of this array for the evidence.
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
