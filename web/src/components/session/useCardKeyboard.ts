import { useCallback, useEffect, useRef, useState } from 'react'

export interface CardKeyboardOptions {
  count: number
  multiSelect?: boolean
  /**
   * Focus the first option on mount so keyboard users land on it. Set false for
   * off-screen cards in a carousel: focusing an option in a non-visible card
   * scrolls the shared track to it, jumping the view to the wrong question.
   */
  autoFocus?: boolean
  onActivate?: (index: number) => void
  onToggle?: (index: number) => void
  onSubmit?: () => void
}

export interface CardKeyboardApi {
  activeIndex: number
  containerProps: { onKeyDown: (e: React.KeyboardEvent) => void }
  itemProps: (index: number) => { ref: (el: HTMLElement | null) => void; tabIndex: number }
}

/**
 * Roving-focus keyboard handling for a card's option list (permission/question
 * cards). Numbers pick an option, arrows move a wrapping highlight, Enter/Space
 * activate (single-select) or toggle/submit (multi-select). Bails on INPUT/TEXTAREA
 * targets so freeform typing is never hijacked.
 *
 * Propagation/default notes:
 * - `preventDefault()` stops native browser defaults (Enter submitting a form,
 *   Space scrolling the page).
 * - `stopPropagation()` stops React synthetic-event bubbling up the React tree;
 *   it does NOT reach native `window.addEventListener` handlers (e.g.
 *   `useInSessionKeys`' detach listener), which sit outside React's event system
 *   entirely.
 * - Isolation from `useInSessionKeys`' window-level Esc/←/Ctrl+Z detach handler
 *   is provided by that hook's own `if (hasOpenAction) return` guard, which fires
 *   whenever a permission or question block is pending — not by stopPropagation here.
 */
export function useCardKeyboard({
  count, multiSelect = false, autoFocus = true, onActivate, onToggle, onSubmit,
}: CardKeyboardOptions): CardKeyboardApi {
  const [activeIndex, setActiveIndex] = useState(0)
  const itemRefs = useRef<(HTMLElement | null)[]>([])

  // Depend on `autoFocus` (not `[]`): a carousel card that later becomes the
  // active one flips false→true and focuses its first option then, so keyboard
  // focus follows the visible card instead of firing once for every card at mount.
  //
  // preventScroll is load-bearing: without it, focusing scrolls the carousel's
  // shared snap track to reveal the option. Mid-navigation the track's onScroll
  // still rounds to the *old* card for the first few frames, flipping its
  // autoFocus back on; a scrolling refocus then drags the track back where it
  // came from — a feedback loop that pins the carousel to question 1.
  useEffect(() => {
    if (autoFocus) itemRefs.current[0]?.focus({ preventScroll: true })
  }, [autoFocus])

  const onKeyDown = useCallback((e: React.KeyboardEvent) => {
    const tag = (e.target as HTMLElement).tagName
    if (tag === 'INPUT' || tag === 'TEXTAREA') return
    if (count === 0) return

    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault(); e.stopPropagation()
      const next = e.key === 'ArrowDown'
        ? (activeIndex + 1) % count
        : (activeIndex - 1 + count) % count
      setActiveIndex(next)
      itemRefs.current[next]?.focus()
      return
    }
    if (/^[1-9]$/.test(e.key)) {
      const n = Number(e.key) - 1
      if (n < count) {
        e.preventDefault(); e.stopPropagation()
        if (multiSelect) onToggle?.(n)
        else onActivate?.(n)
      }
      return
    }
    if (e.key === 'Enter') {
      e.preventDefault(); e.stopPropagation()
      if (multiSelect) onSubmit?.()
      else onActivate?.(activeIndex)
      return
    }
    if (e.key === ' ' || e.key === 'Spacebar') {
      e.preventDefault(); e.stopPropagation()
      if (multiSelect) onToggle?.(activeIndex)
      else onActivate?.(activeIndex)
    }
  }, [count, multiSelect, onActivate, onToggle, onSubmit, activeIndex])

  const itemProps = useCallback((index: number) => ({
    ref: (el: HTMLElement | null) => { itemRefs.current[index] = el },
    tabIndex: index === activeIndex ? 0 : -1,
  }), [activeIndex])

  return { activeIndex, containerProps: { onKeyDown }, itemProps }
}
