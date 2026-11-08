/**
 * THE sandbox toggle confirm (D-23), shared by the Environment card's row and the hold
 * banner: every direction restarts the session, and turning off a held session is
 * "Continue without the sandbox?" from either place. The copy is `sandboxConfirmCopy`.
 */
import { ConfirmDialog } from '@/ui/ConfirmDialog'
import { sandboxConfirmCopy } from './sandboxCopy'

export interface SandboxConfirmDialogProps {
  open: boolean
  /** The direction asked for: true = turn on (or Retry), false = turn off. */
  enabled: boolean
  /** The session is held on a failed sandbox. */
  held: boolean
  busy?: boolean
  onClose: () => void
  onConfirm: () => void | Promise<void>
}

export function SandboxConfirmDialog({ open, enabled, held, busy, onClose, onConfirm }: SandboxConfirmDialogProps) {
  const copy = sandboxConfirmCopy(enabled, held)
  return (
    <ConfirmDialog
      open={open}
      onClose={onClose}
      onConfirm={onConfirm}
      title={copy.title}
      description={copy.description}
      confirmLabel={copy.confirmLabel}
      tone="warning"
      busy={busy}
    />
  )
}
