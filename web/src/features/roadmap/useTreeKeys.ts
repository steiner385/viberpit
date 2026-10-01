/**
 * Tree keyboard semantics for the Map scope (WAI-ARIA tree pattern): one tab
 * stop for the whole tree, arrows to walk it, `←`/`→` to fold and unfold.
 *
 * The flat half — the roving tab stop, `↑`/`↓`/Home/End, real DOM focus through
 * `rowElementId` — is `ui/list/useRovingRows`, shared with the Delivery list
 * column. This hook adds only what a tree has and a list does not: parent/child
 * moves from `depth`, and fold/unfold on `←`/`→`.
 *
 * Focus-scoped by construction: the handler hangs off the tree container, so it
 * can only fire while focus is inside the tree. Nothing is registered with
 * `shell/shortcuts.ts`, which is where GLOBAL keys live — an arrow key that
 * moved a tree cursor from the prompt dock would be a bug, not a feature.
 */
import { useCallback, useMemo } from 'react'
import { rowKeyOf, useRovingRows } from '@/ui/list/useRovingRows'
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
  const keys = useMemo(() => rows.map((r) => r.key), [rows])
  const { containerRef, rovingKey, focusRow, onKeyDown: flatKeys } =
    useRovingRows(keys, selectedKey, TREE_ID_SCOPE)

  const onKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (flatKeys(e)) return
    const key = rowKeyOf(e)
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
  }, [rows, setCollapsed, focusRow, flatKeys])

  return { containerRef, rovingKey, onKeyDown }
}
