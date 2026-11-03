import type { Session } from '@/api/types'
import type { ShortcutContext } from '@/shell/shortcuts'

/**
 * What the shell hands the ⌘K palette (D-10). Types only, so `shell/` can name
 * it without pulling the palette's code out of its lazy chunk.
 *
 * Every effect is the shell's OWN function, the same one the list, the nav and
 * the keyboard already run — the palette keeps no copy of any of them.
 */
export interface PaletteHost {
  /** The shell's shortcut context at the ⌘K keydown, before the palette itself
   *  counted as an overlay. Actions are offered only where their row's `when`
   *  allows this context; `interactive: false` is kiosk. */
  context: ShortcutContext
  /** `useShortcuts`' handlers that run without a key event, by `SHORTCUTS` id —
   *  the identical functions, not wrappers. */
  actions: Readonly<Record<string, () => void>>
  /** The shell list state's live sessions, each with the scope its list opens it in. */
  live: readonly { session: Session; scope: 'live' | 'background' }[]
  /** The list's open path (pending-id guard, then `sessionPath`). */
  openSession: (id: string, scope: 'live' | 'background') => void
  /** The archived list's open path (`archivedSessionPath`). */
  openArchived: (id: string) => void
  /** The list's New session button. */
  newSession: () => void
  navigate: (path: string) => void
  /** Narrow the Sessions list to a repo / branch (see `AppShell`). */
  filterRepo: (nameWithOwner: string) => void
  filterBranch: (branch: string) => void
  /** Close the palette. `then` runs after it has unmounted and focus is back
   *  where ⌘K was pressed, so an action that opens a menu or moves focus is not
   *  undone by the palette's focus restore. */
  close: (then?: () => void) => void
}
