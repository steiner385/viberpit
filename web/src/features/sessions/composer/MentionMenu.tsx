// web/src/features/sessions/composer/MentionMenu.tsx
import { forwardRef, useImperativeHandle, useMemo, useState, useEffect } from 'react'
import { cn } from '@/lib/utils'

export interface MentionSource {
  id: string
  title: string
}

export interface MentionMenuHandle {
  /** Returns true if the key was consumed by the menu. */
  handleKeyDown: (e: React.KeyboardEvent) => boolean
}

interface Props {
  sessions: MentionSource[]
  query: string
  onSelect: (session: MentionSource) => void
  onDismiss: () => void
}

export const MentionMenu = forwardRef<MentionMenuHandle, Props>(function MentionMenu(
  { sessions, query, onSelect, onDismiss }, ref,
) {
  const matches = useMemo(() => {
    const q = query.toLowerCase()
    return sessions.filter((s) => s.title.toLowerCase().includes(q)).slice(0, 8)
  }, [sessions, query])

  const [sel, setSel] = useState(0)
  useEffect(() => { setSel(0) }, [query])

  useImperativeHandle(ref, () => ({
    handleKeyDown(e: React.KeyboardEvent): boolean {
      if (e.key === 'Escape') { e.preventDefault(); onDismiss(); return true }
      if (matches.length === 0) return false
      if (e.key === 'ArrowDown') { e.preventDefault(); setSel((s) => (s + 1) % matches.length); return true }
      if (e.key === 'ArrowUp')   { e.preventDefault(); setSel((s) => (s - 1 + matches.length) % matches.length); return true }
      if (e.key === 'Enter')     { e.preventDefault(); onSelect(matches[sel]); return true }
      return false
    },
  }), [matches, sel, onSelect, onDismiss])

  if (matches.length === 0) return null

  return (
    <div
      role="listbox"
      aria-label="Session mentions"
      className={cn(
        'max-sm:fixed max-sm:inset-x-0 max-sm:bottom-0 max-sm:rounded-b-none max-sm:rounded-t-xl',
        'sm:absolute sm:bottom-full sm:left-0 sm:right-0 sm:mb-1',
        'max-h-64 overflow-y-auto z-popover',
        'border border-edge bg-surface shadow-lg',
        'rounded-md',
      )}
    >
      {matches.map((s, i) => (
        <button
          key={s.id}
          type="button"
          role="option"
          aria-selected={i === sel}
          onMouseEnter={() => setSel(i)}
          onClick={() => onSelect(s)}
          className={cn(
            'block w-full truncate px-3 py-2 text-left text-sm min-h-target',
            i === sel ? 'bg-accent-wash' : 'hover:bg-surface-raised',
          )}
        >
          {s.title}
        </button>
      ))}
    </div>
  )
})
