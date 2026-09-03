/**
 * ModelSwitchDialog — choose HOW to switch model on a BUSY session.
 *
 * A model change can land two ways on a session mid-turn: stop the in-flight
 * turn and restart it on the new model ("now"), or hand the model to the SDK
 * without interrupting so the current turn finishes first and the next turn
 * runs on the new model ("after this turn"). Cancel keeps the current model.
 * Focus starts on Cancel; Esc/backdrop close; Dialog traps focus.
 */
import { useRef } from 'react'
import { Dialog } from '@/ui/Dialog'
import { Button } from '@/ui/Button'

export type ModelSwitchWhen = 'now' | 'after_turn'

interface ModelSwitchDialogProps {
  /** Human label of the target model (falls back to the raw id at the call site). */
  modelLabel: string
  onClose: () => void
  onConfirm: (when: ModelSwitchWhen) => void
}

export function ModelSwitchDialog({ modelLabel, onClose, onConfirm }: ModelSwitchDialogProps) {
  const cancelRef = useRef<HTMLButtonElement>(null)

  return (
    <Dialog open onClose={onClose} title="Switch model?" size="md" initialFocus={cancelRef}>
      <p className="text-sm text-ink-muted">
        This session is working. Switch to <span className="font-medium text-ink">{modelLabel}</span>{' '}
        now — stopping the current activity and restarting it on the new model — or let
        the current turn finish first and switch for the next one.
      </p>

      <div className="flex flex-col sm:flex-row gap-3 sm:justify-end pt-2">
        {/* size="lg" on all three: it is the only Button size carrying
            min-h-target (44px), which docs/ACCESSIBILITY.md requires, and a row
            of three buttons at mismatched heights is worse than either state. */}
        <Button ref={cancelRef} type="button" variant="secondary" size="lg" onClick={onClose}>
          Cancel
        </Button>
        <Button type="button" variant="secondary" size="lg" onClick={() => onConfirm('after_turn')}>
          Switch after this turn
        </Button>
        <Button type="button" variant="primary" size="lg" onClick={() => onConfirm('now')}>
          Stop &amp; switch now
        </Button>
      </div>
    </Dialog>
  )
}
