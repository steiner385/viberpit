import * as React from 'react'
import { Dialog } from './Dialog'
import { Button } from './Button'
import { Input } from './Input'
import { Field } from './Field'

export interface ConfirmDialogProps {
  open: boolean
  onClose: () => void
  /** May be async; the confirm button shows `busy` until it settles. Errors propagate to the caller. */
  onConfirm: () => void | Promise<void>
  title: string
  description: React.ReactNode
  confirmLabel: string
  tone?: 'danger' | 'warning'
  /** Typed tier: the exact string the user must type before confirm enables. */
  typed?: string
  busy?: boolean
}

/**
 * Spec §7.3. Two tiers on one component: Confirm (title + one sentence + two
 * buttons) and Typed (`typed` given: the action enables only when the input
 * equals it). Replaces every `window.confirm` — those cannot show the affected
 * items, are unstyled, and block automation.
 */
export function ConfirmDialog({
  open, onClose, onConfirm, title, description, confirmLabel, tone = 'danger', typed, busy = false,
}: ConfirmDialogProps) {
  const [text, setText] = React.useState('')
  const [pending, setPending] = React.useState(false)
  // Per-instance, not the old static "confirm-typed": two ConfirmDialogs mounted
  // at once (a section keeps its dialog rendered with `open=false`) would share
  // one id, and a duplicate id means the `<label htmlFor>` names whichever input
  // the document happens to hold first.
  const typedId = React.useId()
  const inputRef = React.useRef<HTMLInputElement>(null)
  const cancelRef = React.useRef<HTMLButtonElement>(null)

  // Reset the typed text once the dialog closes, so the next `open` starts
  // blank. Adjusted during render (React's documented pattern for state that
  // depends on a prop change) rather than in an effect, which would call
  // setState synchronously after commit and trigger an extra render pass.
  const [prevOpen, setPrevOpen] = React.useState(open)
  if (open !== prevOpen) {
    setPrevOpen(open)
    if (!open) setText('')
  }

  const ready = typed === undefined || text === typed
  const working = busy || pending

  async function confirm() {
    if (!ready || working) return
    try {
      setPending(true)
      await onConfirm()
    } finally {
      setPending(false)
    }
  }

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={title}
      description={description}
      size="sm"
      initialFocus={typed !== undefined ? inputRef : cancelRef}
    >
      {typed !== undefined && (
        <Field label={`Type ${typed} to confirm`} htmlFor={typedId}>
          <Input
            ref={inputRef}
            id={typedId}
            value={text}
            onChange={(e) => setText(e.target.value)}
            autoComplete="off"
            spellCheck={false}
            placeholder={typed}
          />
        </Field>
      )}
      <div className="flex justify-end gap-2">
        <Button ref={cancelRef} type="button" variant="ghost" onClick={onClose} disabled={working}>
          Cancel
        </Button>
        <Button
          type="button"
          variant={tone === 'danger' ? 'danger' : 'primary'}
          onClick={confirm}
          disabled={!ready || working}
          loading={working}
        >
          {confirmLabel}
        </Button>
      </div>
    </Dialog>
  )
}
