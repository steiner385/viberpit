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
import { rowElementId } from '@/ui/list/rowId'
import type { TreeRow } from './treeRows'

/**
 * `CardFrame` id namespace for every row of the Map scope's tree.
 *
 * Exported because two parties must agree on it: `RoadmapListColumn`, which
 * passes it down to the rows, and `focusRow` below, which looks one up. Without
 * a namespace both the tree and the detail pane's `detail/InitiativeList` mint
 * `row-initiative:<slug>` for the same initiative, and `getElementById` returns
 * whichever is earlier in the DOM — so arrow-key focus was correct only by the
 * accident of pane order (#1475).
 */
export const TREE_ID_SCOPE = 'roadmap-tree'

/** Rows that can be folded carry `expanded`/`hasChildren`; leaves do not. A
 *  program initiative row structurally qualifies the same way a product or
 *  capability row does — it just names a third `kind`. */
type ExpandableRow = Extract<TreeRow, { expanded: boolean }>

const expandable = (row: TreeRow): row is ExpandableRow =>
  (row.kind === 'product' || row.kind === 'capability' || row.kind === 'initiative') && row.hasChildren

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
  // not open a row — so the cursor still survives a walk through the tree.
  const [seenSelection, setSeenSelection] = useState(selectedKey)
  if (seenSelection !== selectedKey) {
    setSeenSelection(selectedKey)
    setFocusKey(null)
  }

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
    // `CardFrame` ids its open control through `rowElementId`; getElementById
    // avoids having to escape a key full of `:` for a CSS selector. The scope is
    // what makes the lookup unambiguous — the detail pane beside this tree
    // renders the same keys — and the `contains` check stays as the belt to that
    // brace, so a future second tree in the same scope still cannot steal focus.
    const el = document.getElementById(rowElementId(key, TREE_ID_SCOPE))
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
