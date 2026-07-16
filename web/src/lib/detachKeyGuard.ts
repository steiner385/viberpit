/**
 * Focus guard for the window-level detach gesture (← / Esc / Ctrl+Z → back to
 * the session list), shared by DesktopShell and SessionView so the two
 * surfaces can't drift apart.
 *
 * The composer (aria-label "Message") keeps its historical contract:
 *   ←          detaches from the left edge (caret at 0, no selection), even
 *              mid-draft — the composer draft survives a detach.
 *   Esc/Ctrl+Z yield whenever it holds text (Esc keeps the draft, Ctrl+Z stays
 *              native undo).
 *
 * Every OTHER text entry (question-card "Write a response", search, rename …)
 * is stricter: all three keys yield whenever the field has text at all. Those
 * fields live in components that unmount on detach, so navigating away
 * destroys the draft — ← may only detach from an empty field.
 */

export type DetachKey = 'ArrowLeft' | 'Escape' | 'CtrlZ'

/** Input types that hold free text; ← must move the caret, not navigate. */
const TEXT_INPUT_TYPES = new Set(['text', 'search', 'url', 'tel', 'email', 'password', 'number'])

function asTextEntry(el: Element | null): HTMLInputElement | HTMLTextAreaElement | null {
  if (el instanceof HTMLTextAreaElement) return el
  if (el instanceof HTMLInputElement && TEXT_INPUT_TYPES.has(el.type)) return el
  return null
}

/** True ⇒ the key belongs to the focused text entry; do NOT detach. */
export function detachKeyYieldsToTextEntry(key: DetachKey, active: Element | null): boolean {
  const el = asTextEntry(active)
  if (!el) return false
  const hasText = el.value !== ''
  const isComposer = el.getAttribute('aria-label') === 'Message'
  if (isComposer && key === 'ArrowLeft') {
    // Left edge (caret at the very start, nothing selected) → detach.
    return !(el.selectionStart === 0 && el.selectionEnd === 0)
  }
  return hasText
}
