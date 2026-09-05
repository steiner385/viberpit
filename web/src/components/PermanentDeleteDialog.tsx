/**
 * PermanentDeleteDialog — the destructive "this is gone for good" confirm, for
 * one session or many.
 *
 * There were two of these: this one (single session; AppShell's card menu and
 * the in-session SessionActions) and a private copy inside ArchivedManager that
 * had grown the bulk copy, a progress bar and a cancel. One dialog now, because
 * the difference between them was never the dialog — it was how many names the
 * caller had.
 *
 * Focus starts on Cancel. While a bulk run is in flight the dialog refuses to
 * dismiss and Cancel becomes the abort: Esc-closing mid-run abandons a delete
 * the user can no longer watch.
 */
import { useRef } from 'react'
import { cn } from '@/lib/utils'
import { Dialog } from '@/ui/Dialog'
import { Button } from '@/ui/Button'
import { prefersReducedMotion } from './session/brailleSpinner'

export interface BulkDeleteProgressShape {
  done: number
  total: number
}

interface PermanentDeleteDialogProps {
  /** One name = the single-session copy; more = the counted bulk copy. */
  names: string[]
  onClose: () => void
  onConfirm: () => void
  isPending: boolean
  /** Bulk only: drives the progress bar. */
  progress?: BulkDeleteProgressShape | null
  /** Bulk only: abort at the next chunk boundary. */
  onCancel?: () => void
}

export function PermanentDeleteDialog({
  names, onClose, onConfirm, isPending, progress, onCancel,
}: PermanentDeleteDialogProps) {
  const cancelRef = useRef<HTMLButtonElement>(null)
  const count = names.length
  const title = count === 1 ? 'Permanently delete session' : `Permanently delete ${count} sessions`

  return (
    <Dialog open onClose={onClose} title={title} size="sm" initialFocus={cancelRef} dismissable={!isPending}>
      {isPending && progress ? (
        <div className="space-y-2">
          <div className="flex items-center justify-between text-sm text-ink-muted">
            <span>Deleting…</span>
            <span className="tabular-nums">{progress.done} of {progress.total}</span>
          </div>
          <div
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={progress.total}
            aria-valuenow={progress.done}
            aria-valuetext={`Deleting ${progress.done} of ${progress.total}`}
            className="h-2 w-full overflow-hidden rounded bg-surface-raised"
          >
            <div
              className={cn('h-full rounded bg-danger-strong', !prefersReducedMotion() && 'transition-[width] duration-200 ease-linear')}
              style={{ width: `${progress.total > 0 ? Math.round((progress.done / progress.total) * 100) : 0}%` }}
            />
          </div>
        </div>
      ) : (
        <p className="text-sm text-ink-muted">
          {count === 1 ? (
            <>
              Permanently delete <span className="font-medium text-ink">&lsquo;{names[0]}&rsquo;</span>? This removes the
              session folder and all its data. This can&rsquo;t be undone.
            </>
          ) : (
            <>
              Permanently delete <span className="font-medium text-ink">{count} sessions</span>? This removes their
              session folders and all their data. This can&rsquo;t be undone.
            </>
          )}
        </p>
      )}

      <div className="flex gap-3 justify-end pt-2">
        <Button
          ref={cancelRef}
          type="button"
          variant="secondary"
          onClick={isPending ? onCancel : onClose}
          disabled={isPending && !onCancel}
        >
          Cancel
        </Button>
        <Button type="button" variant="danger" size="lg" onClick={onConfirm} loading={isPending}>
          {isPending ? 'Deleting…' : 'Delete permanently'}
        </Button>
      </div>
    </Dialog>
  )
}
