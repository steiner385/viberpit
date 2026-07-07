/**
 * ModelSwitchDialog — confirm switching model on a BUSY session.
 *
 * The backend stops the in-flight turn, switches model, and restarts the turn,
 * so the picker must confirm before firing. Patterned on PermanentDeleteDialog
 * (overlay, focus trap, Esc/backdrop close) but non-destructive: the confirm
 * action is a primary blue "Stop & switch". Focus starts on Cancel; Esc closes;
 * Tab is focus-trapped.
 */
import { useEffect, useRef } from 'react'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'

interface ModelSwitchDialogProps {
  /** Human label of the target model (falls back to the raw id at the call site). */
  modelLabel: string
  onClose: () => void
  onConfirm: () => void
}

export function ModelSwitchDialog({ modelLabel, onClose, onConfirm }: ModelSwitchDialogProps) {
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
      aria-labelledby="model-switch-dialog-title"
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
          id="model-switch-dialog-title"
          className="text-lg font-semibold text-gray-900 dark:text-gray-100"
        >
          Switch model?
        </h2>

        <p className="text-sm text-gray-700 dark:text-gray-300">
          This session is working. Switching to <span className="font-medium">{modelLabel}</span>{' '}
          will stop the current activity and restart it on the new model.
        </p>

        <div className="flex gap-3 justify-end pt-2">
          <Button
            ref={cancelRef}
            type="button"
            variant="outline"
            onClick={onClose}
          >
            Cancel
          </Button>
          <Button
            type="button"
            onClick={onConfirm}
            className={cn(
              'min-h-[44px]',
              'bg-blue-600 hover:bg-blue-700 text-white',
              'dark:bg-blue-700 dark:hover:bg-blue-600',
              'focus-visible:ring-blue-500',
            )}
          >
            Stop &amp; switch
          </Button>
        </div>
      </div>
    </div>
  )
}
