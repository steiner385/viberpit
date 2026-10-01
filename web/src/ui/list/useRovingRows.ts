/**
 * Roving tabindex for a flat list of `CardFrame` rows: ONE tab stop for the
 * whole list, `↑`/`↓`/Home/End to walk it, REAL DOM focus on the row's open
 * control so the ring lands somewhere visible.
 *
 * Extracted from `features/roadmap/useTreeKeys` — which now layers the tree's
 * `←`/`→` folding on top of this — when the Delivery list needed the same thing
 * and had none: its rows kept `CardFrame`'s default of "focusable only when
 * selected", and nothing is selected on arrival, so Tab skipped the whole list
 * (2026-10-01 Delivery critique, P1). One cursor, two lists; a third copy was
 * the anti-pattern.
 *
 * Why not `session/useListNavigation`: that hook is an index-only cursor over
 * `NavItem`s with no DOM focus. And not `sessions/useCardKeyboard`: that one
 * auto-focuses its first option on mount, which is right for a question card
 * and wrong for a list column — arriving on a page must never steal focus.
 *
 * Focus-scoped by construction: the handler hangs off the list container, so
 * it can only fire while focus is inside it. Nothing is registered with
 * `shell/shortcuts.ts`, which is where GLOBAL keys live.
 *
 * Contract: each row's wrapper carries `data-row-key={key}` and its `CardFrame`
 * takes `id={key}` with the same `idScope`, so `focusRow` can find the open
 * control through `rowElementId`.
 */
import { useCallback, useRef, useState } from 'react'
import { rowElementId } from './rowId'

export interface RovingRows {
  /** Put on the list container (`role="list"` / `role="tree"`). */
  containerRef: React.RefObject<HTMLDivElement | null>
  /** The one row that holds the tab stop. */
  rovingKey: string | null
  /** Move the tab stop and DOM focus to a row (no-op for a row not in the list). */
  focusRow: (key: string) => void
  /** Flat moves only — `↑`/`↓`/Home/End. Returns true when it consumed the key,
   *  so a caller layering more keys (the tree's `←`/`→`) knows to stop. */
  onKeyDown: (e: React.KeyboardEvent) => boolean
}

/** The row an event rose from, by the `data-row-key` its wrapper carries. */
export function rowKeyOf(e: React.KeyboardEvent): string | undefined {
  return (e.target as HTMLElement).closest<HTMLElement>('[data-row-key]')?.dataset.rowKey
}

export function useRovingRows(
  keys: readonly string[],
  selectedKey: string | null,
  idScope?: string,
): RovingRows {
  const containerRef = useRef<HTMLDivElement>(null)
  const [focusKey, setFocusKey] = useState<string | null>(null)

  // A NEW selection retires the keyboard's cursor. `focusKey` used to be write-
  // only: it was set by every arrow key and never cleared, so after arrowing down
  // three rows and then CLICKING a fourth, the tab stop stayed on the arrowed row
  // — Tab took the user back to a row they had left, not to the one highlighted
  // in front of them. Selection is the more recent intent, whichever way it was
  // expressed, so it wins. Render-phase adjustment (React's "resetting state when
  // a prop changes"), not an effect: the tab stop must be right in the same frame
  // the new selection paints.
  //
  // Arrowing itself does NOT re-enter here — `focusRow` only moves focus, it does
  // not open a row — so the cursor still survives a walk through the list.
  const [seenSelection, setSeenSelection] = useState(selectedKey)
  if (seenSelection !== selectedKey) {
    setSeenSelection(selectedKey)
    setFocusKey(null)
  }

  // The open row owns the tab stop until the keyboard moves off it. Either can
  // vanish (a fold, a search, a deleted row), and then the first row takes it —
  // a list whose only tab stop is a row that no longer exists is a list the Tab
  // key skips entirely. And a list with nothing selected yet — every arrival —
  // starts on its first row for the same reason.
  const preferred = focusKey ?? selectedKey
  const rovingKey = preferred !== null && keys.includes(preferred)
    ? preferred
    : (keys[0] ?? null)

  const focusRow = useCallback((key: string) => {
    setFocusKey(key)
    // `CardFrame` ids its open control through `rowElementId`; getElementById
    // avoids having to escape a key full of `:` for a CSS selector. The scope is
    // what makes the lookup unambiguous — a detail pane beside the list may
    // render the same keys — and the `contains` check stays as the belt to that
    // brace, so a second list in the same scope still cannot steal focus.
    const el = document.getElementById(rowElementId(key, idScope))
    if (el && containerRef.current?.contains(el)) el.focus()
  }, [idScope])

  const onKeyDown = useCallback((e: React.KeyboardEvent): boolean => {
    // Enter and Space are deliberately absent: `CardFrame` already opens the
    // focused row on both (its `listMode={false}` branch), and a second handler
    // here would open it twice.
    const key = rowKeyOf(e)
    if (key === undefined) return false
    const i = keys.indexOf(key)
    if (i < 0) return false

    const move = (j: number): boolean => {
      const target = keys[j]
      if (target === undefined) return false
      e.preventDefault()
      focusRow(target)
      return true
    }

    switch (e.key) {
      case 'ArrowDown': return move(i + 1)
      case 'ArrowUp': return move(i - 1)
      case 'Home': return move(0)
      case 'End': return move(keys.length - 1)
      default: return false
    }
  }, [keys, focusRow])

  return { containerRef, rovingKey, focusRow, onKeyDown }
}
