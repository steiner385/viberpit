/**
 * ModelPicker — the prompt row's model menu (sub-spec D §3). It replaced the model
 * `<select>` in the session status strip, and owns the busy-session confirmation:
 * switching model mid-turn either stops and restarts the turn or waits for it, so a
 * pick on a busy session opens `ModelSwitchDialog` instead of firing at once.
 *
 * The trigger is bound to the CHOSEN alias (the session record), never to the
 * SDK-reported id — that id is the launch model until a turn runs on the new one,
 * and binding to it made the picker snap back on every switch. The live id is a
 * separate read-only readout beside it.
 */

import { useRef, useState } from 'react'
import { ChevronDown } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Menu } from '@/ui/Menu'
import { modelName } from '@/session/modelName'
import { ModelSwitchDialog, type ModelSwitchWhen } from '@/features/sessions/panels/ModelSwitchDialog'
import type { AgentEngine } from '@/api/types'

export interface ModelPickerProps {
  /** The chosen alias (session record); null = the account default. */
  chosen: string | null
  /** The model actually producing (SDK id); lags `chosen` by a turn after a switch. */
  live: string | null
  /** True while a "switch after this turn" change waits for the turn to end. */
  pending: boolean
  models: readonly { id: string; label: string }[]
  onSelect: (id: string, when: ModelSwitchWhen) => void
  /** A turn is running: a pick asks stop-now vs after-this-turn first. */
  busy?: boolean
  /** The engine's `steer` capability (relabels the non-interrupting choice). */
  canSteer?: boolean
  engine?: AgentEngine | null
  /** Controlled open state (the keyboard shortcut opens it from outside). */
  open?: boolean
  onOpenChange?: (open: boolean) => void
}

export function ModelPicker({
  chosen, live, pending, models, onSelect, busy = false, canSteer = false, engine = null, open, onOpenChange,
}: ModelPickerProps) {
  const anchorRef = useRef<HTMLButtonElement>(null)
  const [localOpen, setLocalOpen] = useState(false)
  const isOpen = open ?? localOpen
  const setOpen = (next: boolean) => {
    if (open === undefined) setLocalOpen(next)
    onOpenChange?.(next)
  }
  // Model id awaiting the stop-now / after-this-turn choice (busy sessions only).
  const [confirmId, setConfirmId] = useState<string | null>(null)

  const current = chosen ?? 'default'
  const labelFor = (id: string) =>
    models.find((m) => m.id === id)?.label ?? (id === 'default' ? 'Default' : id)
  const label = labelFor(current)
  const liveLabel = live ? modelName(live, engine) : ''
  // A choice outside the catalog (a retired alias, or the default) still shows as checked.
  const items = models.some((m) => m.id === current) ? models : [{ id: current, label }, ...models]

  const pick = (id: string) => {
    if (id === current) return
    if (busy) setConfirmId(id)
    else onSelect(id, 'now')
  }

  return (
    <span className="inline-flex items-center gap-1.5 min-w-0">
      <button
        ref={anchorRef}
        type="button"
        aria-label={`Model: ${label}`}
        aria-haspopup="menu"
        aria-expanded={isOpen}
        onClick={() => setOpen(!isOpen)}
        className={cn(
          'inline-flex items-center gap-1 rounded-md px-2 py-0.5 min-h-7 text-xs text-ink-muted',
          'hover:bg-edge active:bg-edge-strong',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
        )}
      >
        <span className="truncate max-w-[12rem]">{label}</span>
        <ChevronDown size={12} aria-hidden="true" />
      </button>
      {liveLabel && liveLabel !== label && (
        <span
          aria-label="Model in use"
          title="Model currently answering (a switch takes effect on the next turn)"
          className="text-xs text-ink-faint truncate max-w-[8rem]"
        >
          {liveLabel}
        </span>
      )}
      {pending && (
        <span aria-label="Model switch pending" title="Switching when the current turn finishes" className="text-xs text-warning">
          · after this turn
        </span>
      )}
      <Menu
        open={isOpen}
        onClose={() => setOpen(false)}
        anchorRef={anchorRef}
        label="Model"
        items={items.map((m) => ({ id: m.id, label: m.label, selected: m.id === current }))}
        onSelect={pick}
      />
      {confirmId !== null && (
        <ModelSwitchDialog
          modelLabel={labelFor(confirmId)}
          canSteer={canSteer}
          onClose={() => setConfirmId(null)}
          onConfirm={(when) => {
            onSelect(confirmId, when)
            setConfirmId(null)
          }}
        />
      )}
    </span>
  )
}
