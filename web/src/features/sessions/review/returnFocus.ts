import { useCallback, useEffect, useRef } from 'react'
import { lineKey, type Side } from './diffAnchors'

/** Finds where focus goes, inside the flow's root, once the change has rendered. */
export type FocusFinder<T extends HTMLElement> = (root: T) => HTMLElement | null | undefined

/**
 * Return focus when a comment flow (D-5) closes the control that held it: the
 * editor, a card's Delete, the review bar. `PermissionCard.cancelComment`'s
 * precedent, except the target often exists only after the next render (a
 * re-mounted gutter, the card after a deleted one), so the caller names it with
 * `returnFocus(find)` and `find(root)` is focused after the owner re-renders.
 * The handler that calls `returnFocus` must also change the owner's state or props.
 */
export function useReturnFocus<T extends HTMLElement>() {
  const root = useRef<T>(null)
  const pending = useRef<FocusFinder<T> | null>(null)
  useEffect(() => {
    const find = pending.current
    if (!find || !root.current) return
    pending.current = null
    find(root.current)?.focus()
  })
  const returnFocus = useCallback((find: FocusFinder<T>) => { pending.current = find }, [])
  return { root, returnFocus }
}

/** The gutter button for `side`'s line `n` (`LineCommentGutter` carries the key). */
export function gutterButton(root: Element, side: Side, n: number): HTMLElement | null {
  return root.querySelector<HTMLElement>(`[data-line="${lineKey(side, n)}"]`)
}

/**
 * The last resort for a flow inside one file's section of the Changes tab: that
 * file's header (`[data-file]` / `[data-file-header]`), else the tab's own root
 * (`[data-review-root]`, focusable). Null outside the tab (a bare diff).
 */
export function fileOrTabFallback(el: Element): HTMLElement | null {
  return el.closest('[data-file]')?.querySelector<HTMLElement>('[data-file-header]')
    ?? el.closest<HTMLElement>('[data-review-root]')
}

/**
 * Where focus goes after deleting comment `id`: the next card's Edit button, in
 * reading order, else `fallback`. Call it BEFORE the delete renders — the next
 * card is found now; it is keyed, so it is the same node after. A card mid-edit
 * has no Edit button and falls through to `fallback`.
 */
export function afterDelete<T extends HTMLElement>(root: T | null, id: string, fallback: FocusFinder<T>): FocusFinder<T> {
  const cards = Array.from(root?.querySelectorAll<HTMLElement>('[data-comment-id]') ?? [])
  const at = cards.findIndex((c) => c.dataset.commentId === id)
  const next = at < 0 ? undefined : cards[at + 1]
  return (r) => (next?.isConnected ? next.querySelector<HTMLElement>('[data-comment-edit]') : null) ?? fallback(r)
}
