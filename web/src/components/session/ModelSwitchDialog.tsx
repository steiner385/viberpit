/**
 * ModelSwitchDialog — choose HOW to switch model on a BUSY session.
 *
 * A model change can land two ways on a session mid-turn: stop the in-flight
 * turn and restart it on the new model ("now"), or hand the model to the SDK
 * without interrupting so the current turn finishes first and the next turn
 * runs on the new model ("after this turn"). Cancel keeps the current model.
 * Patterned on PermanentDeleteDialog (overlay, focus trap, Esc/backdrop close)
 * but non-destructive. Focus starts on Cancel; Esc closes; Tab is focus-trapped.
 */
import { useEffect, useRef } from 'react'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'

export type ModelSwitchWhen = 'now' | 'after_turn'

interface ModelSwitchDialogProps {
  /** Human label of the target model (falls back to the raw id at the call site). */
  modelLabel: string
  onClose: () => void
  onConfirm: (when: ModelSwitchWhen) => void
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
          'w-full max-w-md rounded-xl',
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
          This session is working. Switch to <span className="font-medium">{modelLabel}</span>{' '}
          now — stopping the current activity and restarting it on the new model — or let
          the current turn finish first and switch for the next one.
        </p>

        <div className="flex flex-col sm:flex-row gap-3 sm:justify-end pt-2">
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
            variant="outline"
            onClick={() => onConfirm('after_turn')}
            className="min-h-[44px]"
          >
            Switch after this turn
          </Button>
          <Button
            type="button"
            onClick={() => onConfirm('now')}
            className={cn(
              'min-h-[44px]',
              'bg-blue-600 hover:bg-blue-700 text-white',
              'dark:bg-blue-700 dark:hover:bg-blue-600',
              'focus-visible:ring-blue-500',
            )}
          >
            Stop &amp; switch now
          </Button>
        </div>
      </div>
    </div>
  )
}
