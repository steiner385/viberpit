/**
 * The automation detail's header controls: the mode select and the enable
 * switch. Moved (behaviour) from `AutomationCard.tsx`'s main card — the mode
 * `<select>` and the enable `<button role="switch">` — onto `ui/Select` and
 * `ui/Switch`.
 */
import type { Automation, AutomationPatch } from '@/api/automations'
import { usePatchAutomation } from '@/api/hooks'
import { Select } from '@/ui/Select'
import { Switch } from '@/ui/Switch'

export function AutomationControls({ automation: a }: { automation: Automation }) {
  const patch = usePatchAutomation()
  // No control loop → no toggle and no schedule editor. Both would be controls
  // the server refuses (422) and, worse, an operator reading a grey switch as
  // "PM classification is off" when it is simply not a thing you turn on.
  const operatorTriggered = a.operatorTriggered ?? a.modes.length === 0

  return (
    <div className="flex items-center gap-3">
      {a.modes.length > 2 && (
        <Select
          aria-label="Configured mode"
          size="sm"
          value={a.mode ?? ''}
          title={a.mode ? a.oversight?.modeDescriptions[a.mode] : undefined}
          onChange={(e) => patch.mutate({ id: a.id, body: { mode: e.target.value } })}
        >
          {a.modes.map((m) => <option key={m} value={m}>{m}</option>)}
        </Select>
      )}
      {!operatorTriggered && (
        <Switch
          aria-label={`${a.name} enabled`}
          checked={a.enabled}
          onChange={(next) => {
            const body: AutomationPatch = { enabled: next }
            // A 2-mode automation (off/propose) hides the mode <select>, so the
            // toggle is its only control: enabling with no active mode must also
            // set one, else the row is enabled but stuck 'off'.
            const active = a.modes.filter((m) => m !== 'off')
            if (next && active.length > 0 && !active.includes(a.mode ?? '')) {
              body.mode = active[0]
            }
            patch.mutate({ id: a.id, body })
          }}
        />
      )}
    </div>
  )
}
