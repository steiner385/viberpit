import { useCallback, useEffect, useRef, useState } from 'react'

export interface CardKeyboardOptions {
  count: number
  multiSelect?: boolean
  onActivate: (index: number) => void
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
 */
export function useCardKeyboard({
  count, multiSelect = false, onActivate, onToggle, onSubmit,
}: CardKeyboardOptions): CardKeyboardApi {
  const [activeIndex, setActiveIndex] = useState(0)
  const itemRefs = useRef<(HTMLElement | null)[]>([])

  useEffect(() => {
    itemRefs.current[0]?.focus()
  }, [])

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
        else onActivate(n)
      }
      return
    }
    if (e.key === 'Enter') {
      e.preventDefault(); e.stopPropagation()
      if (multiSelect) onSubmit?.()
      else onActivate(activeIndex)
      return
    }
    if (e.key === ' ' || e.key === 'Spacebar') {
      e.preventDefault(); e.stopPropagation()
      if (multiSelect) onToggle?.(activeIndex)
      else onActivate(activeIndex)
    }
  }, [count, multiSelect, onActivate, onToggle, onSubmit, activeIndex])

  const itemProps = useCallback((index: number) => ({
    ref: (el: HTMLElement | null) => { itemRefs.current[index] = el },
    tabIndex: index === activeIndex ? 0 : -1,
  }), [activeIndex])

  return { activeIndex, containerProps: { onKeyDown }, itemProps }
}
