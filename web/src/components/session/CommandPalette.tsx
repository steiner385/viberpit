// web/src/components/session/CommandPalette.tsx
import { forwardRef, useImperativeHandle, useMemo, useState, useEffect } from 'react'
import { cn } from '@/lib/utils'
import type { Command } from '@/session/events'

export interface CommandPaletteHandle {
  /** Returns true if the key was consumed by the palette. */
  handleKeyDown: (e: React.KeyboardEvent) => boolean
}

interface Props {
  commands: Command[]
  query: string
  onSelect: (cmd: Command) => void
  onComplete: (cmd: Command) => void
  onDismiss: () => void
}

// Built-ins first, then whatever the engine itself publishes. `skill` joins
// `passthrough` because both mean "the engine runs this"; they only differ in
// how the server hands it over (see server/sessions/commands/dispatcher.py).
function rank(c: Command): number {
  return c.kind === 'passthrough' || c.kind === 'skill' ? 1 : 0
}

// One engine publishes one kind of catalog, so the heading follows the first
// entry in the group rather than trying to describe a mix that cannot occur.
function groupLabel(c: Command): string {
  return c.kind === 'skill' ? 'Skills' : 'Custom'
}

export const CommandPalette = forwardRef<CommandPaletteHandle, Props>(function CommandPalette(
  { commands, query, onSelect, onComplete, onDismiss }, ref,
) {
  const matches = useMemo(() => {
    const q = query.toLowerCase()
    return commands
      .filter((c) => c.name.toLowerCase().includes(q))
      .sort((a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name))
  }, [commands, query])

  const [sel, setSel] = useState(0)
  useEffect(() => { setSel(0) }, [query])

  useImperativeHandle(ref, () => ({
    handleKeyDown(e: React.KeyboardEvent): boolean {
      if (matches.length === 0) return false
      if (e.key === 'ArrowDown') { e.preventDefault(); setSel((s) => (s + 1) % matches.length); return true }
      if (e.key === 'ArrowUp')   { e.preventDefault(); setSel((s) => (s - 1 + matches.length) % matches.length); return true }
      if (e.key === 'Enter')     { e.preventDefault(); onSelect(matches[sel]); return true }
      if (e.key === 'Tab')       { e.preventDefault(); onComplete(matches[sel]); return true }
      if (e.key === 'Escape')    { e.preventDefault(); onDismiss(); return true }
      return false
    },
  }), [matches, sel, onSelect, onComplete, onDismiss])

  if (matches.length === 0) return null

  let lastRank = -1
  return (
    <div
      role="listbox"
      aria-label="Slash commands"
      className={cn(
        // Mobile (<sm): fixed bottom sheet anchored to viewport bottom
        'max-sm:fixed max-sm:inset-x-0 max-sm:bottom-0 max-sm:rounded-b-none max-sm:rounded-t-xl',
        // sm+: dropdown above the composer
        'sm:absolute sm:bottom-full sm:left-0 sm:right-0 sm:mb-1',
        'max-h-64 overflow-y-auto z-20',
        'border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 shadow-lg',
        'rounded-md',
      )}
    >
      {matches.map((c, i) => {
        const showDivider = rank(c) !== lastRank && rank(c) === 1
        lastRank = rank(c)
        return (
          <div key={c.name}>
            {showDivider && (
              <div className="px-3 py-1 text-[10px] uppercase tracking-wide text-gray-400 dark:text-gray-500">{groupLabel(c)}</div>
            )}
            <button
              type="button"
              role="option"
              aria-selected={i === sel}
              onMouseEnter={() => setSel(i)}
              onClick={() => onSelect(c)}
              className={cn(
                'flex w-full items-center gap-2 px-3 py-2 text-left text-sm min-h-[44px]',
                i === sel ? 'bg-blue-50 dark:bg-blue-900/30' : 'hover:bg-gray-50 dark:hover:bg-gray-800',
              )}
            >
              <span className="font-mono text-gray-900 dark:text-gray-100">{c.name}</span>
              <span className="flex-1 truncate text-gray-500 dark:text-gray-400">{c.description}</span>
              {c.argumentHint && <span className="font-mono text-xs text-gray-400">{c.argumentHint}</span>}
            </button>
          </div>
        )
      })}
    </div>
  )
})
