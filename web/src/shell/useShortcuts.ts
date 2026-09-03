/**
 * The app's ONE keydown listener.
 *
 * ## What it replaced
 *
 * Five window/document listeners, each with its own idea of when a key may fire:
 *
 *   • `session/useInSessionKeys` (window) — Ctrl+Z / Esc / ← detach,
 *   • `components/useGlobalHelpKey` (document) — `?`, blocked by
 *     `[role="dialog"][aria-modal="true"]`,
 *   • `components/useGlobalListNav` (document) — ↑/↓, blocked by the LOOSER
 *     `[role="dialog"]`, so a docked push-mode drawer killed the arrows,
 *   • `SessionCard`'s and `SessionActions`' overflow-menu Escape (document,
 *     one each, `stopPropagation`).
 *
 * They disagreed in ways nothing could see: none of them looked at `ui/Menu`, two
 * of them queried different selectors for "is an overlay open", and only one
 * checked `defaultPrevented` — which is why Escape on the theme menu used to
 * close the menu AND walk out of the open session.
 *
 * Now: one listener, and `shell/shortcuts.ts` — the same array the help sheet
 * renders — decides. A key's guard lives beside the row that declares it, so
 * "where may this fire?" has one answer per shortcut instead of one answer per
 * listener.
 *
 * `ui/useFocusTrap` keeps its own `document` listener (DEC-11): a modal's Escape
 * is the primitive's own behaviour, it is stack-aware so the INNERMOST dialog
 * closes, and it calls `stopImmediatePropagation()` — which is exactly why this
 * listener never has to know that dialogs exist.
 *
 * ## Precedence, and where it lives
 *
 * Not in this file. The order of `SHORTCUTS` plus each row's `when` IS the
 * precedence:
 *
 *   1. an event another handler already claimed (`defaultPrevented`) is left
 *      alone — a focused row's `list.up`, `ui/Menu`'s Escape, `DispatchBar`'s
 *      arrows all `preventDefault`, so they win by handling the key first;
 *   2. `overlayOpen` — every global row refuses to fire under a modal or an open
 *      menu, so dismissing one is all that happens;
 *   3. `hasOpenAction` — an unanswered card holds Esc and ←, but not Ctrl+Z;
 *   4. `yieldsToTextEntry` — the focused text entry's claim on this key
 *      (`lib/detachKeyGuard`), the composer's left-edge ← rule included;
 *   5. the session-level gestures.
 *
 * Read through `shortcutAllowed`, never a hand-spelled `when` test: the
 * "omitted ⇒ always allowed" default has one owner, because spelling it
 * `!s.when(c)` once would make all 22 unconditional rows silently unreachable.
 */
import * as React from 'react'
import { matchShortcut, type ShortcutContext } from './shortcuts'

/** A shortcut's effect, keyed by row id. A row with no entry here cannot fire. */
export type ShortcutHandlers = Record<string, (e: KeyboardEvent) => void>

/**
 * Install the dispatcher. `ctx` is called once per keydown, with the event, so
 * the per-event half of the context (`active`, `yieldsToTextEntry`) is honest.
 *
 * Refs and an empty dep array: the shell re-renders on every route change, list
 * cursor move and session frame, and re-registering the listener on each of them
 * would churn it — and a listener that is briefly absent is a keypress that is
 * silently dropped.
 */
export function useShortcuts(
  ctx: (e: KeyboardEvent) => ShortcutContext,
  handlers: ShortcutHandlers,
): void {
  const ctxRef = React.useRef(ctx)
  const handlersRef = React.useRef(handlers)
  // Assigned in an effect, not during render: a render that React throws away
  // must not leave the live listener pointing at its context.
  React.useEffect(() => {
    ctxRef.current = ctx
    handlersRef.current = handlers
  })

  React.useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.defaultPrevented) return
      const row = matchShortcut(e, (s) => handlersRef.current[s.id] !== undefined, ctxRef.current(e))
      if (!row) return
      e.preventDefault()
      handlersRef.current[row.id](e)
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])
}
