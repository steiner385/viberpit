/**
 * ModelSwitchDialog — choose HOW to switch model on a BUSY session.
 *
 * A model change can land two ways on a session mid-turn: stop the in-flight
 * turn and restart it on the new model ("now"), or hand the model to the driver
 * without interrupting ("after_turn"). Cancel keeps the current model.
 * Focus starts on Cancel; Esc/backdrop close; Dialog traps focus.
 *
 * What the non-interrupting choice actually DOES depends on the engine's
 * `steer` capability, so the label does too:
 *
 * * no `steer` (Claude) — the SDK keeps the running turn on the old model and
 *   applies the new one to the next turn ("switch after this turn").
 * * `steer` (Codex) — the driver retargets the turn already running
 *   (`server/codex/driver.py::set_model` → `turn/settings/update`), so nothing
 *   waits for the turn to end ("switch without stopping").
 *
 * The command sent is identical either way; only the promise made to the user
 * changes, and it is gated on the capability the server advertises — never on
 * an engine-name comparison.
 */
import { useRef } from 'react'
import { Dialog } from '@/ui/Dialog'
import { Button } from '@/ui/Button'

export type ModelSwitchWhen = 'now' | 'after_turn'

interface ModelSwitchDialogProps {
  /** Human label of the target model (falls back to the raw id at the call site). */
  modelLabel: string
  /** The engine's server-advertised `steer` capability: it can retarget a turn
   *  that is already running. Changes what the non-interrupting choice promises
   *  (see the module docstring), never what it sends. */
  canSteer?: boolean
  onClose: () => void
  onConfirm: (when: ModelSwitchWhen) => void
}

export function ModelSwitchDialog({ modelLabel, canSteer = false, onClose, onConfirm }: ModelSwitchDialogProps) {
  const cancelRef = useRef<HTMLButtonElement>(null)

  return (
    <Dialog open onClose={onClose} title="Switch model?" size="md" initialFocus={cancelRef}>
      <p className="text-sm text-ink-muted">
        This session is working. Switch to <span className="font-medium text-ink">{modelLabel}</span>{' '}
        now — stopping the current activity and restarting it on the new model — or{' '}
        {canSteer
          ? 'switch without stopping, which moves the activity already running onto the new model.'
          : 'let the current turn finish first and switch for the next one.'}
      </p>

      <div className="flex flex-col sm:flex-row gap-3 sm:justify-end pt-2">
        {/* size="lg" on all three: it is the only Button size carrying
            min-h-target (44px), which docs/ACCESSIBILITY.md requires, and a row
            of three buttons at mismatched heights is worse than either state. */}
        <Button ref={cancelRef} type="button" variant="secondary" size="lg" onClick={onClose}>
          Cancel
        </Button>
        <Button type="button" variant="secondary" size="lg" onClick={() => onConfirm('after_turn')}>
          {canSteer ? 'Switch without stopping' : 'Switch after this turn'}
        </Button>
        <Button type="button" variant="primary" size="lg" onClick={() => onConfirm('now')}>
          Stop &amp; switch now
        </Button>
      </div>
    </Dialog>
  )
}
