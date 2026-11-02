/**
 * ModePicker — the prompt row's permission-mode menu (sub-spec D §3.1). It
 * replaces the old in-session Plan toggle: Plan is one entry among the Desktop
 * modes, ViberPit's own modes sit under their own heading. Labels and grouping
 * come from `permissionModes.ts` (the single list); this component only renders.
 */

import { useRef, useState } from 'react'
import { ChevronDown } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Menu, type MenuGroup } from '@/ui/Menu'
import {
  PERMISSION_GROUP_HEADINGS,
  canonicalMode,
  modeLabel,
  type PermissionModeGroup,
  type SessionMode,
} from '@/session/permissionModes'

const GROUPS: readonly MenuGroup[] = (Object.keys(PERMISSION_GROUP_HEADINGS) as PermissionModeGroup[]).map((id) => ({
  id,
  heading: PERMISSION_GROUP_HEADINGS[id],
}))

export interface ModePickerProps {
  /** The session's effective mode as the runtime reports it (may be the SDK `default` sentinel). */
  mode: string
  /** A requested mode still awaiting the engine's acknowledgement. */
  pendingMode?: string | null
  options: readonly { value: SessionMode; label: string; group: PermissionModeGroup }[]
  onSelect: (mode: SessionMode) => void
  /** Controlled open state (the Alt+Shift+M shortcut opens it from outside). */
  open?: boolean
  onOpenChange?: (open: boolean) => void
}

export function ModePicker({ mode, pendingMode = null, options, onSelect, open, onOpenChange }: ModePickerProps) {
  const anchorRef = useRef<HTMLButtonElement>(null)
  const [localOpen, setLocalOpen] = useState(false)
  const isOpen = open ?? localOpen
  const setOpen = (next: boolean) => {
    if (open === undefined) setLocalOpen(next)
    onOpenChange?.(next)
  }
  // The checkmark follows the latest request so a pending change reads as chosen.
  const current = canonicalMode(pendingMode ?? mode)
  const label = modeLabel(mode)

  return (
    <span className="inline-flex items-center gap-1.5">
      <button
        ref={anchorRef}
        type="button"
        aria-label={`Mode: ${label}`}
        aria-haspopup="menu"
        aria-expanded={isOpen}
        onClick={() => setOpen(!isOpen)}
        className={cn(
          'inline-flex items-center gap-1 rounded-md px-2 py-0.5 min-h-7 text-xs text-ink-muted',
          'hover:bg-edge active:bg-edge-strong',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
          canonicalMode(mode) === 'plan' && 'text-accent',
        )}
      >
        {label}
        <ChevronDown size={12} aria-hidden="true" />
      </button>
      {pendingMode && (
        <span role="status" className="text-xs text-ink-faint">Mode change pending</span>
      )}
      <Menu
        open={isOpen}
        onClose={() => setOpen(false)}
        anchorRef={anchorRef}
        label="Permission mode"
        items={options.map((o) => ({ id: o.value, label: o.label, group: o.group, selected: o.value === current }))}
        groups={GROUPS}
        onSelect={(id) => onSelect(id as SessionMode)}
      />
    </span>
  )
}
