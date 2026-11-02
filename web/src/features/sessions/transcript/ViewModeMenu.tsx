/**
 * ViewModeMenu — the transcript view-mode control in the session meta strip
 * (Desktop parity, sub-spec D §4): Normal / Thinking / Verbose, also cycled by
 * Ctrl/⌘+O. The list and labels come from `session/viewMode.ts`; this only renders.
 */
import { useRef, useState } from 'react'
import { ChevronDown } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Menu } from '@/ui/Menu'
import { TRANSCRIPT_VIEW_MODES, viewModeLabel, type TranscriptViewMode } from '@/session/viewMode'

export interface ViewModeMenuProps {
  mode: TranscriptViewMode
  onSelect: (mode: TranscriptViewMode) => void
}

export function ViewModeMenu({ mode, onSelect }: ViewModeMenuProps) {
  const anchorRef = useRef<HTMLButtonElement>(null)
  const [open, setOpen] = useState(false)
  const modeLabel = viewModeLabel(mode)
  // The accessible name is "View: <Mode>" at every width; below `md` the visible
  // "View: " prefix drops so the trigger fits the phone meta strip on one line.
  const label = `View: ${modeLabel}`

  return (
    <>
      <button
        ref={anchorRef}
        type="button"
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className={cn(
          'inline-flex items-center gap-1 whitespace-nowrap rounded-md px-2 py-0.5 min-h-7 text-xs text-ink-muted',
          'hover:bg-edge active:bg-edge-strong',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
        )}
      >
        <span><span className="hidden md:inline">View: </span>{modeLabel}</span>
        <ChevronDown size={12} aria-hidden="true" />
      </button>
      <Menu
        open={open}
        onClose={() => setOpen(false)}
        anchorRef={anchorRef}
        label="Transcript view"
        align="end"
        items={TRANSCRIPT_VIEW_MODES.map((m) => ({ id: m.value, label: m.label, selected: m.value === mode }))}
        onSelect={(id) => onSelect(id as TranscriptViewMode)}
      />
    </>
  )
}
