/**
 * Focus guard for the detach gesture (← / Esc / Ctrl+Z → back to the session
 * list).
 *
 * It used to be shared by `DesktopShell` and `SessionView`, then by
 * `useInSessionKeys`; all three are deleted. Its one caller is now the shell's
 * single keydown dispatcher (`shell/useShortcuts.ts`), which folds the answer
 * into `ShortcutContext.yieldsToTextEntry` so the registry rows can state the
 * rule declaratively instead of each listener re-deriving it.
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

/** Which detach gesture `e` is, or null for every other key. */
export function detachKeyOf(e: {
  key: string
  ctrlKey: boolean
  altKey: boolean
  metaKey: boolean
}): DetachKey | null {
  if (e.ctrlKey && !e.altKey && !e.metaKey && (e.key === 'z' || e.key === 'Z')) return 'CtrlZ'
  if (e.ctrlKey || e.altKey || e.metaKey) return null
  if (e.key === 'Escape') return 'Escape'
  if (e.key === 'ArrowLeft') return 'ArrowLeft'
  return null
}

/**
 * True ⇒ the focused text entry claims THIS event; no detach gesture may fire.
 *
 * Key-specific by construction — the composer holds ← only when the caret is
 * away from the left edge — which is why the shell computes it per keydown
 * rather than storing "a text entry has focus" once.
 */
export function textEntryClaimsKey(
  e: { key: string; ctrlKey: boolean; altKey: boolean; metaKey: boolean },
  active: Element | null,
): boolean {
  const key = detachKeyOf(e)
  return key !== null && detachKeyYieldsToTextEntry(key, active)
}

/**
 * True ⇒ focus is inside a form control that owns its own keys.
 *
 * The guard `useGlobalHelpKey` and `useGlobalListNav` each carried inline (and
 * each spelled the same way): `?` must type a character into a field, and ↑/↓
 * must drive a `<select>` or move a caret, never the session list underneath.
 * It is deliberately BROADER than `asTextEntry` above — a `<select>` holds no
 * text but owns its arrows — and deliberately narrower than "anything
 * focusable": a focused button or link is chrome, and the global keys work from
 * there.
 */
export function isFormControl(el: Element | null): boolean {
  if (el === null) return false
  if (el instanceof HTMLElement && el.isContentEditable) return true
  return el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT'
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
