/**
 * PlusMenu — the `+` button and its menu beside a prompt (D-14, spec §3/§8): the
 * dispatch bar's Attach image · Presets › · Templates ›, and the session composer's
 * Attach file · Slash commands. Built on the one `ui/Menu`; a `›` entry swaps the
 * menu for a second `Menu` on the same anchor rather than nesting a flyout, which
 * the primitive does not have.
 *
 * Eager, not lazy: the dock is in the entry chunk, and a lazy split here moved
 * `Menu` and the icon runtime into extra preloaded chunks — measured, it RAISED
 * `initialJs`.
 *
 * An action runs AFTER the menu has closed. `Menu` hands focus back to its anchor
 * on close, so an action that focuses the prompt (Slash commands, a template) would
 * otherwise be undone by that restore; this component's effect runs after the
 * menus', in the same commit.
 */
import { useEffect, useRef, useState } from 'react'
import { Plus } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Menu, type MenuItem } from '@/ui/Menu'

export interface PlusMenuEntry {
  id: string
  label: string
  /** Runs once the menu has closed. */
  run?: () => void
  /** Or opens a second menu of these items in place of the first. */
  submenu?: { label: string; items: MenuItem[]; onSelect: (id: string) => void }
}

export function PlusMenu({ entries, label = 'Add' }: { entries: readonly PlusMenuEntry[]; label?: string }) {
  const anchorRef = useRef<HTMLButtonElement>(null)
  // null = closed; 'root' = the first menu; an entry id = that entry's submenu.
  const [open, setOpen] = useState<string | null>(null)
  const pending = useRef<(() => void) | null>(null)

  useEffect(() => {
    if (open !== null || !pending.current) return
    const run = pending.current
    pending.current = null
    run()
  }, [open])

  const sub = entries.find((e) => e.id === open)?.submenu

  return (
    <>
      <button
        ref={anchorRef}
        type="button"
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={open !== null}
        onClick={() => setOpen((cur) => (cur === null ? 'root' : null))}
        className={cn(
          'flex-shrink-0 rounded-md px-3 py-2 text-sm min-h-target',
          'border border-edge-strong',
          'text-ink-muted',
          'hover:bg-surface-raised',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
        )}
      >
        <Plus size={16} aria-hidden="true" />
      </button>
      <Menu
        open={open === 'root'}
        // Functional: choosing a `›` entry has already moved `open` to its id, and
        // the close that follows every pick must not undo that.
        onClose={() => setOpen((cur) => (cur === 'root' ? null : cur))}
        anchorRef={anchorRef}
        label={label}
        align="end"
        items={entries.map((e) => ({ id: e.id, label: e.submenu ? `${e.label} ›` : e.label }))}
        onSelect={(id) => {
          const entry = entries.find((e) => e.id === id)
          if (entry?.submenu) setOpen(id)
          else pending.current = entry?.run ?? null
        }}
      />
      {/* Always mounted, like the first: `Menu` restores focus to the anchor on its
          open → closed transition, which an unmount would skip. */}
      <Menu
        open={sub !== undefined}
        onClose={() => setOpen(null)}
        anchorRef={anchorRef}
        label={sub?.label ?? label}
        align="end"
        items={sub?.items ?? []}
        onSelect={(id) => { if (sub) pending.current = () => sub.onSelect(id) }}
      />
    </>
  )
}
