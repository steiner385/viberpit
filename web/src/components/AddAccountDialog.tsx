/**
 * AddAccountDialog — collects a name for a new account, then drives either:
 *   - Anthropic OAuth: the interactive browser-OAuth login (the same global
 *     state machine as reauth) to create the account's credential slot.
 *   - GitHub Copilot: the device-flow OAuth to create a Copilot account slot.
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
import {
  useAddAccount, useAddAccountStatus, useCancelAddAccount,
  useAddCopilotAccount, useCopilotAddStatus, useCancelCopilotAdd,
  QK,
} from '@/api/hooks'
import { accountNameError } from '@/lib/accountName'
import { ReauthBrowserPanel } from './ReauthBrowserPanel'

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
  const [provider, setProvider] = useState<'anthropic' | 'copilot'>('anthropic')
  const [copilotStarted, setCopilotStarted] = useState(false)

  const qc = useQueryClient()

  // Anthropic hooks
  const addAccount = useAddAccount()
  const cancelAdd = useCancelAddAccount()
  const statusQuery = useAddAccountStatus(started && !optimisticIdle)
  const status = optimisticIdle ? undefined : statusQuery.data

  // Copilot hooks
  const addCopilotAccount = useAddCopilotAccount()
  const cancelCopilotAdd = useCancelCopilotAdd()
  const copilotStatusQuery = useCopilotAddStatus(name.trim(), copilotStarted)
  const copilotStatus = copilotStarted ? copilotStatusQuery.data : undefined

  const isRunning = status?.state === 'running' || status?.state === 'cancelling'
  const isSuccess = status?.state === 'success'
  const isError = status?.state === 'error' || status?.state === 'timeout'
  // While the in-page reauth browser is showing, widen the dialog so the 1280×800
  // virtual display isn't crushed into the narrow add-account form width.
  const showVnc = isRunning && !!status?.vncSession

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

  // Refresh the accounts list once the Anthropic add transitions into success — the new
  // account only appears in the list after its slot is written.
  const prevStatusRef = useRef<string | undefined>(undefined)
  React.useEffect(() => {
    if (status?.state === 'success' && prevStatusRef.current !== 'success') {
      qc.invalidateQueries({ queryKey: QK.accounts })
    }
    prevStatusRef.current = status?.state
  }, [status?.state, qc])

  // Close when the Copilot device-flow completes with authorization.
  const prevCopilotStatusRef = useRef<string | undefined>(undefined)
  React.useEffect(() => {
    if (copilotStatus?.state === 'authorized' && prevCopilotStatusRef.current !== 'authorized') {
      qc.invalidateQueries({ queryKey: QK.accounts })
      onClose()
    }
    prevCopilotStatusRef.current = copilotStatus?.state
  }, [copilotStatus?.state, qc, onClose])

  const trimmed = name.trim()
  // Mirror the backend rule client-side so an invalid name never round-trips to
  // a 400 that surfaces as a generic toast. null = valid (or empty).
  const nameError = accountNameError(trimmed)

  // Anthropic canSubmit: block while running
  const canSubmit = !!trimmed && !nameError && !isRunning
  // Copilot canSubmit: block once the flow has started
  const copilotCanSubmit = !!trimmed && !nameError && !copilotStarted

  function handleSubmit() {
    if (!canSubmit) return
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

  function handleCopilotSubmit() {
    if (!copilotCanSubmit) return
    setCopilotStarted(true)
    addCopilotAccount.mutate(trimmed, {
      onError: () => setCopilotStarted(false),
    })
  }

  function handleCopilotCancel() {
    cancelCopilotAdd.mutate(trimmed)
    setCopilotStarted(false)
  }

  const copilotFailed = copilotStatus?.state === 'failed'

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
          'w-full rounded-xl transition-[max-width] motion-reduce:transition-none',
          showVnc ? 'max-w-5xl' : 'max-w-sm',
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

        {/* Provider toggle */}
        <div className="flex rounded-lg border border-gray-200 dark:border-gray-600 overflow-hidden text-sm">
          <button
            type="button"
            onClick={() => { setProvider('anthropic'); setCopilotStarted(false) }}
            className={cn(
              'flex-1 py-2 px-3 text-center transition-colors',
              provider === 'anthropic'
                ? 'bg-blue-600 text-white'
                : 'bg-white dark:bg-gray-700 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-600',
            )}
            aria-pressed={provider === 'anthropic'}
          >
            Anthropic (OAuth)
          </button>
          <button
            type="button"
            onClick={() => { setProvider('copilot'); setStarted(false); setOptimisticIdle(false) }}
            className={cn(
              'flex-1 py-2 px-3 text-center transition-colors',
              provider === 'copilot'
                ? 'bg-blue-600 text-white'
                : 'bg-white dark:bg-gray-700 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-600',
            )}
            aria-pressed={provider === 'copilot'}
          >
            GitHub Copilot
          </button>
        </div>

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
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault()
                if (provider === 'anthropic') handleSubmit()
                else handleCopilotSubmit()
              }
            }}
            disabled={isRunning || isSuccess || copilotStarted}
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
          {nameError ? (
            <p className="text-xs text-red-600 dark:text-red-400" role="alert">
              {nameError}
            </p>
          ) : (
            <p className="text-xs text-gray-500 dark:text-gray-400">
              A short identifier (letters, numbers, <code>. _ -</code>). You can add a
              friendly nickname afterwards.
            </p>
          )}
        </div>

        {/* Anthropic OAuth flow status */}
        {provider === 'anthropic' && isRunning && (
          status?.vncSession ? (
            <>
              <p className="text-xs text-amber-600 dark:text-amber-400">
                Sign in to Claude in the window below…
              </p>
              <ReauthBrowserPanel vncSession={status.vncSession} />
            </>
          ) : (
            <p className="text-xs text-amber-600 dark:text-amber-400">
              Opening Chrome on the host screen — sign in there…
            </p>
          )
        )}
        {provider === 'anthropic' && isSuccess && (
          <p className="text-xs text-green-600 dark:text-green-400">
            ✓ Account added.
          </p>
        )}
        {provider === 'anthropic' && isError && status?.message && (
          <p className="text-xs text-red-600 dark:text-red-400" role="alert">
            {status.message}
          </p>
        )}

        {/* Copilot device-flow status */}
        {provider === 'copilot' && copilotStatus?.state === 'awaiting_authorization' && copilotStatus.userCode && (
          <div className="space-y-2 rounded-lg border border-amber-200 dark:border-amber-700 bg-amber-50 dark:bg-amber-900/20 p-3">
            <p className="text-xs text-amber-700 dark:text-amber-300">
              Enter this code at the link below to authorize:
            </p>
            <p className="text-lg font-mono font-bold tracking-widest text-amber-900 dark:text-amber-100 text-center">
              {copilotStatus.userCode}
            </p>
            {copilotStatus.verificationUri && (
              <a
                href={copilotStatus.verificationUri}
                target="_blank"
                rel="noopener noreferrer"
                className="block text-xs text-blue-600 dark:text-blue-400 hover:underline text-center"
              >
                {copilotStatus.verificationUri}
              </a>
            )}
          </div>
        )}
        {provider === 'copilot' && copilotStatus?.state === 'pending' && copilotStarted && (
          <p className="text-xs text-amber-600 dark:text-amber-400">
            Starting device authorization…
          </p>
        )}
        {provider === 'copilot' && copilotFailed && (
          <p className="text-xs text-red-600 dark:text-red-400" role="alert">
            Authorization failed. Please try again.
          </p>
        )}

        <div className="flex gap-3 justify-end pt-2">
          {provider === 'anthropic' ? (
            <>
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
                  disabled={!canSubmit}
                  className="min-h-[44px]"
                >
                  {isRunning ? 'Adding…' : 'Add'}
                </Button>
              )}
            </>
          ) : (
            <>
              {copilotStarted ? (
                <Button
                  type="button"
                  variant="outline"
                  onClick={handleCopilotCancel}
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
                  Cancel
                </Button>
              )}
              <Button
                type="button"
                onClick={handleCopilotSubmit}
                disabled={!copilotCanSubmit}
                className="min-h-[44px]"
              >
                {copilotStarted ? 'Authorizing…' : 'Add'}
              </Button>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
