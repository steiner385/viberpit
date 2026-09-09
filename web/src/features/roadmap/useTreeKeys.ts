/**
 * Tree keyboard semantics for the Map scope (WAI-ARIA tree pattern): one tab
 * stop for the whole tree, arrows to walk it, `←`/`→` to fold and unfold.
 *
 * Why a local hook and not `session/useListNavigation`: that hook is a flat
 * cursor over `NavItem`s (Session-typed, index-only, no DOM focus and no notion
 * of depth). A tree needs the two things it does not have — parent/child moves
 * from `depth`, and REAL focus on the row so `→` lands somewhere visible — so
 * generalising it would have meant rewriting it, not parameterising it. It stays
 * the sessions list's cursor; this is the tree's.
 *
 * Focus-scoped by construction: the handler hangs off the tree container, so it
 * can only fire while focus is inside the tree. Nothing is registered with
 * `shell/shortcuts.ts`, which is where GLOBAL keys live — an arrow key that
 * moved a tree cursor from the prompt dock would be a bug, not a feature.
 */
import { useCallback, useRef, useState } from 'react'
import type { TreeRow } from './treeRows'

/** Rows that can be folded carry `expanded`/`hasChildren`; leaves do not. */
type ExpandableRow = Extract<TreeRow, { expanded: boolean }>

const expandable = (row: TreeRow): row is ExpandableRow =>
  (row.kind === 'product' || row.kind === 'capability') && row.hasChildren

export interface TreeKeys {
  /** Put on the `role="tree"` element. */
  containerRef: React.RefObject<HTMLDivElement | null>
  /** The one row that holds the tab stop (roving tabindex). */
  rovingKey: string | null
  onKeyDown: (e: React.KeyboardEvent) => void
}

export function useTreeKeys(
  rows: readonly TreeRow[],
  selectedKey: string | null,
  setCollapsed: (key: string, collapsed: boolean) => void,
): TreeKeys {
  const containerRef = useRef<HTMLDivElement>(null)
  const [focusKey, setFocusKey] = useState<string | null>(null)

  // The open row owns the tab stop until the keyboard moves off it. Either can
  // vanish (a fold, a search, a deleted product), and then the first row takes
  // it — a tree whose only tab stop is a row that no longer exists is a tree the
  // Tab key skips entirely.
  const preferred = focusKey ?? selectedKey
  const rovingKey = rows.some((r) => r.key === preferred)
    ? preferred
    : (rows[0]?.key ?? null)

  const focusRow = useCallback((key: string) => {
    setFocusKey(key)
    // `CardFrame` ids its open control `row-<key>`; getElementById avoids having
    // to escape a key full of `:` for a CSS selector. Scoped to the container so
    // a same-named row in another list can never steal the focus.
    const el = document.getElementById(`row-${key}`)
    if (el && containerRef.current?.contains(el)) el.focus()
  }, [])

  const onKeyDown = useCallback((e: React.KeyboardEvent) => {
    // Enter and Space are deliberately absent: `CardFrame` already opens the
    // focused row on both (its `listMode={false}` branch), and a second handler
    // here would open it twice.
    const wrapper = (e.target as HTMLElement).closest<HTMLElement>('[data-row-key]')
    const key = wrapper?.dataset.rowKey
    if (key === undefined) return
    const i = rows.findIndex((r) => r.key === key)
    if (i < 0) return
    const row = rows[i]!

    const move = (j: number) => {
      const target = rows[j]
      if (!target) return
      e.preventDefault()
      focusRow(target.key)
    }

    switch (e.key) {
      case 'ArrowDown': move(i + 1); break
      case 'ArrowUp': move(i - 1); break
      case 'Home': move(0); break
      case 'End': move(rows.length - 1); break
      case 'ArrowRight':
        if (!expandable(row)) break
        e.preventDefault()
        // Closed: open it. Already open: step onto the first child, which is
        // the next row exactly when it is deeper than this one.
        if (!row.expanded) setCollapsed(row.key, false)
        else if ((rows[i + 1]?.depth ?? -1) > row.depth) focusRow(rows[i + 1]!.key)
        break
      case 'ArrowLeft':
        if (expandable(row) && row.expanded) {
          e.preventDefault()
          setCollapsed(row.key, true)
          break
        }
        // A closed row (or a leaf) hands focus up to its parent: the nearest
        // row above it that sits shallower.
        for (let j = i - 1; j >= 0; j -= 1) {
          if (rows[j]!.depth < row.depth) { move(j); break }
        }
        break
      default: break
    }
  }, [rows, setCollapsed, focusRow])

  return { containerRef, rovingKey, onKeyDown }
}
