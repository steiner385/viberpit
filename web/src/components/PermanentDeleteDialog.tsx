/**
 * PermanentDeleteDialog — single-session destructive confirm.
 *
 * Shared source of truth for the "permanently delete this session" modal, used by
 * Dashboard (session card + live session), DesktopShell, and the in-session
 * SessionActions menu. Focus starts on Cancel; Esc closes; Tab is focus-trapped.
 *
 * NOTE: ArchivedManager.tsx still carries its own bulk-capable copy; consolidating
 * that one is tracked separately.
 */
import { useEffect, useRef } from 'react'
import { cn } from '@/lib/utils'
import { Button } from '@/ui/Button'

interface PermanentDeleteDialogProps {
  sessionName: string
  onClose: () => void
  onConfirm: () => void
  isPending: boolean
}

export function PermanentDeleteDialog({ sessionName, onClose, onConfirm, isPending }: PermanentDeleteDialogProps) {
  const dialogRef = useRef<HTMLDivElement>(null)
  const cancelRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    cancelRef.current?.focus()
  }, [])

  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') { e.stopPropagation(); onClose(); return }
      if (e.key === 'Tab') {
        const focusable = dialogRef.current?.querySelectorAll<HTMLElement>(
          'button, input, select, textarea, [tabindex]:not([tabindex="-1"])',
        )
        if (!focusable || focusable.length === 0) return
        const first = focusable[0]
        const last = focusable[focusable.length - 1]
        if (e.shiftKey) {
          if (document.activeElement === first) { e.preventDefault(); last.focus() }
        } else {
          if (document.activeElement === last) { e.preventDefault(); first.focus() }
        }
      }
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [onClose])

  return (
    <div
      className="fixed inset-0 z-50 flex items-end sm:items-center justify-center p-4 bg-black/50"
      role="dialog"
      aria-modal="true"
      aria-labelledby="perm-delete-dialog-title"
      onClick={(e) => { if (e.target === e.currentTarget) onClose() }}
    >
      <div
        ref={dialogRef}
        className={cn(
          'w-full max-w-sm rounded-xl',
          'bg-white dark:bg-gray-800',
          'border border-gray-200 dark:border-gray-600',
          'shadow-xl p-6 space-y-4',
        )}
      >
        <h2
          id="perm-delete-dialog-title"
          className="text-lg font-semibold text-gray-900 dark:text-gray-100"
        >
          Permanently delete session
        </h2>

        <p className="text-sm text-gray-700 dark:text-gray-300">
          Permanently delete <span className="font-medium">'{sessionName}'</span>? This removes the
          session folder and all its data. This can't be undone.
        </p>

        <div className="flex gap-3 justify-end pt-2">
          <Button
            ref={cancelRef}
            type="button"
            variant="outline"
            onClick={onClose}
            disabled={isPending}
          >
            Cancel
          </Button>
          <Button
            type="button"
            onClick={onConfirm}
            disabled={isPending}
            className={cn(
              'min-h-[44px]',
              'bg-red-600 hover:bg-red-700 text-white',
              'dark:bg-red-700 dark:hover:bg-red-600',
              'focus-visible:ring-red-500',
            )}
          >
            {isPending ? 'Deleting…' : 'Delete permanently'}
          </Button>
        </div>
      </div>
    </div>
  )
}
