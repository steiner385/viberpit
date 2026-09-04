/**
 * PermanentDeleteDialog — single-session destructive confirm.
 *
 * Shared source of truth for the "permanently delete this session" modal, used by
 * Dashboard (session card + live session), DesktopShell, and the in-session
 * SessionActions menu. Focus starts on Cancel; Esc/backdrop close; Dialog traps focus.
 */
import { useRef } from 'react'
import { Dialog } from '@/ui/Dialog'
import { Button } from '@/ui/Button'

interface PermanentDeleteDialogProps {
  sessionName: string
  onClose: () => void
  onConfirm: () => void
  isPending: boolean
}

export function PermanentDeleteDialog({ sessionName, onClose, onConfirm, isPending }: PermanentDeleteDialogProps) {
  const cancelRef = useRef<HTMLButtonElement>(null)
  return (
    <Dialog open onClose={onClose} title="Permanently delete session" size="sm" initialFocus={cancelRef}>
      <p className="text-sm text-ink-muted">
        Permanently delete <span className="font-medium text-ink">'{sessionName}'</span>? This removes the
        session folder and all its data. This can't be undone.
      </p>
      <div className="flex gap-3 justify-end pt-2">
        <Button ref={cancelRef} type="button" variant="secondary" onClick={onClose} disabled={isPending}>Cancel</Button>
        <Button type="button" variant="danger" size="lg" onClick={onConfirm} loading={isPending}>
          {isPending ? 'Deleting…' : 'Delete permanently'}
        </Button>
      </div>
    </Dialog>
  )
}
