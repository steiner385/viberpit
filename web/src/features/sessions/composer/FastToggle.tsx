/**
 * ⚡ — the prompt row's fast-mode toggle (D-14), and its cost note: the first time
 * this browser sees fast mode ON — from ⚡, `/fast` or another tab alike — the
 * sentence shows inline under the row until dismissed or fast mode goes off.
 * Eager: a lazy split pulled the icon runtime into its own preloaded chunk.
 */
import { useEffect, useState } from 'react'
import { X, Zap } from 'lucide-react'
import { cn } from '@/lib/utils'
import { FAST_MODE_COPY, hasSeenFastNote, markFastNoteSeen } from './fastMode'

export default function FastToggle({ on, onToggle, pending = false }: {
  on: boolean
  onToggle: (enabled: boolean) => void
  /** The POST is in flight: no second toggle until it answers. */
  pending?: boolean
}) {
  // Adjusted while rendering on the off → on edge (no effect-driven setState); the
  // effect only records, in storage, that the note has been shown.
  const [prevOn, setPrevOn] = useState(on)
  const [noteShown, setNoteShown] = useState(false)
  if (on !== prevOn) {
    setPrevOn(on)
    setNoteShown(on && !hasSeenFastNote())
  }
  useEffect(() => { if (noteShown) markFastNoteSeen() }, [noteShown])

  return (
    <>
      <button
        type="button"
        aria-label="Fast mode"
        aria-pressed={on}
        title={FAST_MODE_COPY}
        disabled={pending}
        aria-busy={pending || undefined}
        onClick={() => onToggle(!on)}
        className={cn(
          'inline-flex items-center rounded-md px-2 py-0.5 min-h-7 text-xs',
          on ? 'text-warning' : 'text-ink-muted',
          'hover:bg-edge active:bg-edge-strong',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
          'disabled:opacity-50',
        )}
      >
        <Zap size={14} aria-hidden="true" />
      </button>
      {noteShown && on && (
        <p role="note" className="flex basis-full items-start gap-1 text-ink-muted">
          <span className="flex-1">{FAST_MODE_COPY}</span>
          <button
            type="button"
            aria-label="Dismiss fast mode note"
            onClick={() => setNoteShown(false)}
            className="shrink-0 rounded-sm hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            <X size={12} aria-hidden="true" />
          </button>
        </p>
      )}
    </>
  )
}
