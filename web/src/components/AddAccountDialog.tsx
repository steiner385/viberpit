/**
 * AddAccountDialog — collects a name for a new account, then drives the
 * interactive browser-OAuth login (the same global state machine as reauth) to
 * create the account's credential slot.
 *
 * Single source of truth for the add-account flow: imported by both the mobile
 * Dashboard accounts tab and the desktop AccountsManager pane. All add-account
 * hooks live here so they are only invoked while the dialog is open — keeping
 * them out of the always-mounted account panels.
 */
import * as React from 'react'
import { useState, useRef } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import { useAddAccount, useAddAccountStatus, useCancelAddAccount, QK } from '@/api/hooks'

interface AddAccountDialogProps {
  onClose: () => void
}

export function AddAccountDialog({ onClose }: AddAccountDialogProps) {
  const dialogRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const [name, setName] = useState('')
  const [started, setStarted] = useState(false)
  // Optimistic local idle override — set when the user cancels.
  const [optimisticIdle, setOptimisticIdle] = useState(false)

  const qc = useQueryClient()
  const addAccount = useAddAccount()
  const cancelAdd = useCancelAddAccount()

  const statusQuery = useAddAccountStatus(started && !optimisticIdle)
  const status = optimisticIdle ? undefined : statusQuery.data

  const isRunning = status?.state === 'running' || status?.state === 'cancelling'
  const isSuccess = status?.state === 'success'
  const isError = status?.state === 'error' || status?.state === 'timeout'

  React.useEffect(() => {
    inputRef.current?.focus()
  }, [])

  React.useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape' && !isRunning) { onClose(); return }
      if (e.key === 'Tab') {
        const focusable = dialogRef.current?.querySelectorAll<HTMLElement>(
          'button:not([disabled]), input, [tabindex]:not([tabindex="-1"])',
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
  }, [onClose, isRunning])

  // Refresh the accounts list once the add transitions into success — the new
  // account only appears in the list after its slot is written.
  const prevStatusRef = useRef<string | undefined>(undefined)
  React.useEffect(() => {
    if (status?.state === 'success' && prevStatusRef.current !== 'success') {
      qc.invalidateQueries({ queryKey: QK.accounts })
    }
    prevStatusRef.current = status?.state
  }, [status?.state, qc])

  const trimmed = name.trim()

  function handleSubmit() {
    if (!trimmed || isRunning) return
    setOptimisticIdle(false)
    setStarted(true)
    addAccount.mutate(trimmed, {
      onError: () => setStarted(false),
    })
  }

  function handleCancel() {
    setOptimisticIdle(true)
    setStarted(false)
    cancelAdd.mutate()
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-end sm:items-center justify-center p-4 bg-black/50"
      role="dialog"
      aria-modal="true"
      aria-labelledby="add-account-dialog-title"
      onClick={(e) => { if (e.target === e.currentTarget && !isRunning) onClose() }}
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
          id="add-account-dialog-title"
          className="text-lg font-semibold text-gray-900 dark:text-gray-100"
        >
          Add account
        </h2>

        <div className="space-y-1">
          <label
            htmlFor="add-account-name"
            className="block text-sm text-gray-700 dark:text-gray-300"
          >
            Account name
          </label>
          <input
            ref={inputRef}
            id="add-account-name"
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); handleSubmit() } }}
            disabled={isRunning || isSuccess}
            placeholder="e.g. work"
            aria-label="Account name"
            className={cn(
              'w-full rounded border border-gray-300 dark:border-gray-600',
              'bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100',
              'px-2 py-2 text-sm',
              'focus:outline-none focus:ring-2 focus:ring-blue-500',
              'disabled:opacity-60',
            )}
          />
          <p className="text-xs text-gray-500 dark:text-gray-400">
            A short identifier (letters, numbers, <code>. _ -</code>). You can add a
            friendly nickname afterwards.
          </p>
        </div>

        {isRunning && (
          <p className="text-xs text-amber-600 dark:text-amber-400">
            Opening a fresh Chrome on the host machine&rsquo;s screen — sign in there…
          </p>
        )}
        {isSuccess && (
          <p className="text-xs text-green-600 dark:text-green-400">
            ✓ Account added.
          </p>
        )}
        {isError && status?.message && (
          <p className="text-xs text-red-600 dark:text-red-400" role="alert">
            {status.message}
          </p>
        )}

        <div className="flex gap-3 justify-end pt-2">
          {isRunning ? (
            <Button
              type="button"
              variant="outline"
              onClick={handleCancel}
              aria-label="Cancel add"
            >
              Cancel
            </Button>
          ) : (
            <Button
              type="button"
              variant="outline"
              onClick={onClose}
            >
              {isSuccess ? 'Done' : 'Cancel'}
            </Button>
          )}
          {!isSuccess && (
            <Button
              type="button"
              onClick={handleSubmit}
              disabled={!trimmed || isRunning}
              className="min-h-[44px]"
            >
              {isRunning ? 'Adding…' : 'Add'}
            </Button>
          )}
        </div>
      </div>
    </div>
  )
}
