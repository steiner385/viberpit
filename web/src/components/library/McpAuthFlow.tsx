import { useState } from 'react'
import { cn } from '@/lib/utils'
import {
  useStartMcpAuth,
  useMcpAuthStatus,
  useSubmitMcpRedirect,
  useCancelMcpAuth,
} from '@/api/hooks'

interface McpAuthFlowProps {
  server: string
  accounts: string[]
}

export function McpAuthFlow({ server, accounts }: McpAuthFlowProps) {
  // started gates the status poll — the hook's own refetchInterval stops polling on terminal states.
  const [started, setStarted] = useState(false)
  const [redirectUrl, setRedirectUrl] = useState('')

  const startMcpAuth = useStartMcpAuth()
  const cancelMcpAuth = useCancelMcpAuth()
  const submitMcpRedirect = useSubmitMcpRedirect()

  const statusQuery = useMcpAuthStatus(server, started)
  const status = statusQuery.data
  // Derive display state: use polled data when available; fall back to synthetic states.
  const state = status?.state ?? (started ? 'starting' : 'idle')

  function handleStart() {
    setStarted(true)
    startMcpAuth.mutate(server)
  }

  function handleCancel() {
    setStarted(false)
    cancelMcpAuth.mutate(server)
  }

  function handleSubmit() {
    submitMcpRedirect.mutate(
      { name: server, redirectUrl },
      { onSuccess: () => setRedirectUrl('') },
    )
  }

  // --- Render by state ---

  // Idle / not started
  if (state === 'idle') {
    return (
      <div className="mt-1 px-3 pb-2">
        <button
          type="button"
          onClick={handleStart}
          className={cn(
            'rounded px-2 py-1 text-xs font-medium',
            'border border-amber-400 dark:border-amber-600',
            'text-amber-700 dark:text-amber-300',
            'hover:bg-amber-50 dark:hover:bg-amber-900/30',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-500',
          )}
        >
          Fix auth
        </button>
      </div>
    )
  }

  // Cancelled — subtle text + allow re-start
  if (state === 'cancelled') {
    return (
      <div className="mt-1 px-3 pb-2">
        <p className="text-xs text-gray-500 dark:text-gray-400 mb-1">Cancelled.</p>
        <button
          type="button"
          onClick={handleStart}
          className={cn(
            'rounded px-2 py-1 text-xs font-medium',
            'border border-amber-400 dark:border-amber-600',
            'text-amber-700 dark:text-amber-300',
            'hover:bg-amber-50 dark:hover:bg-amber-900/30',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-500',
          )}
        >
          Fix auth
        </button>
      </div>
    )
  }

  // Starting — progress text + Cancel
  if (state === 'starting') {
    return (
      <div className="mt-1 px-3 pb-2 flex items-center gap-2">
        <p className="text-xs text-amber-600 dark:text-amber-400">Getting authorization link…</p>
        <button
          type="button"
          onClick={handleCancel}
          className={cn(
            'rounded px-2 py-1 text-xs',
            'border border-gray-300 dark:border-gray-600',
            'text-gray-700 dark:text-gray-300',
            'hover:bg-gray-100 dark:hover:bg-gray-700',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
          )}
        >
          Cancel
        </button>
      </div>
    )
  }

  // Awaiting redirect — show URL + paste box + Cancel
  if (state === 'awaiting_redirect') {
    return (
      <div className="mt-1 px-3 pb-2 flex flex-col gap-2">
        <p className="text-xs text-amber-600 dark:text-amber-400">
          Open the link below and approve access.
        </p>
        {status?.authUrl ? (
          <div className="flex items-center gap-2 min-w-0">
            <a
              href={status.authUrl}
              target="_blank"
              rel="noopener noreferrer"
              className={cn(
                'text-xs text-blue-600 dark:text-blue-400 underline truncate min-w-0 flex-1',
              )}
            >
              {status.authUrl}
            </a>
            <button
              type="button"
              aria-label="Copy authorization URL"
              className={cn(
                'rounded px-2 py-1 text-xs flex-shrink-0',
                'border border-gray-300 dark:border-gray-600',
                'text-gray-700 dark:text-gray-300',
                'hover:bg-gray-100 dark:hover:bg-gray-700',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
              )}
              onClick={() => {
                void navigator.clipboard.writeText(status.authUrl!)
              }}
            >
              Copy
            </button>
          </div>
        ) : (
          <p className="text-xs text-red-500">Authorization link unavailable — cancel and retry.</p>
        )}
        <p className="text-xs text-gray-500 dark:text-gray-400">
          Open the link and approve. Your browser will redirect to a localhost address that may fail
          to load — copy that full address and paste it here.
        </p>
        <div className="flex items-center gap-2">
          <input
            type="text"
            value={redirectUrl}
            onChange={(e) => setRedirectUrl(e.target.value)}
            aria-label={`Redirect URL for ${server}`}
            placeholder="Paste the redirect URL…"
            className={cn(
              'flex-1 min-w-0 rounded border border-gray-300 dark:border-gray-600',
              'bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100',
              'px-2 py-1 text-xs',
              'focus:outline-none focus:ring-2 focus:ring-blue-500',
            )}
          />
          <button
            type="button"
            className={cn(
              'rounded px-2 py-1 text-xs font-medium flex-shrink-0',
              'bg-blue-600 text-white hover:bg-blue-700',
              'dark:bg-blue-700 dark:hover:bg-blue-600',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
              'disabled:opacity-40',
            )}
            disabled={!redirectUrl.trim() || submitMcpRedirect.isPending}
            onClick={handleSubmit}
          >
            Submit
          </button>
          <button
            type="button"
            onClick={handleCancel}
            className={cn(
              'rounded px-2 py-1 text-xs flex-shrink-0',
              'border border-gray-300 dark:border-gray-600',
              'text-gray-700 dark:text-gray-300',
              'hover:bg-gray-100 dark:hover:bg-gray-700',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
            )}
          >
            Cancel
          </button>
        </div>
      </div>
    )
  }

  // Exchanging — progress text + Cancel
  if (state === 'exchanging') {
    return (
      <div className="mt-1 px-3 pb-2 flex items-center gap-2">
        <p className="text-xs text-amber-600 dark:text-amber-400">Completing authentication…</p>
        <button
          type="button"
          onClick={handleCancel}
          className={cn(
            'rounded px-2 py-1 text-xs',
            'border border-gray-300 dark:border-gray-600',
            'text-gray-700 dark:text-gray-300',
            'hover:bg-gray-100 dark:hover:bg-gray-700',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
          )}
        >
          Cancel
        </button>
      </div>
    )
  }

  // Success
  if (state === 'success') {
    const count = status?.accounts.length ?? accounts.length
    return (
      <div className="mt-1 px-3 pb-2">
        <p className="text-xs text-green-600 dark:text-green-400">
          Authorized — cleared for {count} account(s).
        </p>
      </div>
    )
  }

  // Error
  if (state === 'error') {
    return (
      <div className="mt-1 px-3 pb-2 flex flex-col gap-1">
        <p className="text-xs text-red-600 dark:text-red-400" role="alert">
          {status?.message ?? 'Authentication failed.'}
        </p>
        <button
          type="button"
          onClick={handleStart}
          className={cn(
            'rounded px-2 py-1 text-xs font-medium self-start',
            'border border-amber-400 dark:border-amber-600',
            'text-amber-700 dark:text-amber-300',
            'hover:bg-amber-50 dark:hover:bg-amber-900/30',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-500',
          )}
        >
          Retry
        </button>
      </div>
    )
  }

  return null
}
