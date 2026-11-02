/**
 * EffortPicker — the prompt row's reasoning-effort menu (sub-spec D §3). It replaced
 * the read-only effort readout in the session status strip. The caller gates it:
 * it renders only for an engine with the `reasoningEffort` capability whose chosen
 * model lists `reasoningEfforts` — never disabled, just absent.
 */

import { useRef, useState } from 'react'
import { ChevronDown } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Menu } from '@/ui/Menu'

export interface EffortPickerProps {
  /** The session's effort, else the model's default; null = unknown. */
  effort: string | null
  /** Efforts the chosen model offers, in catalog order. */
  efforts: readonly string[]
  onSelect: (effort: string) => void
  /** Controlled open state (the keyboard shortcut opens it from outside). */
  open?: boolean
  onOpenChange?: (open: boolean) => void
}

export function EffortPicker({ effort, efforts, onSelect, open, onOpenChange }: EffortPickerProps) {
  const anchorRef = useRef<HTMLButtonElement>(null)
  const [localOpen, setLocalOpen] = useState(false)
  const isOpen = open ?? localOpen
  const setOpen = (next: boolean) => {
    if (open === undefined) setLocalOpen(next)
    onOpenChange?.(next)
  }
  const label = effort ?? 'Default'

  return (
    <>
      <button
        ref={anchorRef}
        type="button"
        aria-label={`Effort: ${label}`}
        aria-haspopup="menu"
        aria-expanded={isOpen}
        onClick={() => setOpen(!isOpen)}
        className={cn(
          'inline-flex items-center gap-1 rounded-md px-2 py-0.5 min-h-7 text-xs text-ink-muted',
          'hover:bg-edge active:bg-edge-strong',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
        )}
      >
        {label}
        <ChevronDown size={12} aria-hidden="true" />
      </button>
      <Menu
        open={isOpen}
        onClose={() => setOpen(false)}
        anchorRef={anchorRef}
        label="Reasoning effort"
        items={efforts.map((e) => ({ id: e, label: e, selected: e === effort }))}
        onSelect={(id) => { if (id !== effort) onSelect(id) }}
      />
    </>
  )
}
