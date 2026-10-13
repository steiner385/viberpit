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
    startMcpAuth.mutate({ name: server, account: accounts[0] })
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
            'border border-warning',
            'text-warning',
            'hover:bg-warning-wash',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-warning',
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
        <p className="text-xs text-ink-muted mb-1">Cancelled.</p>
        <button
          type="button"
          onClick={handleStart}
          className={cn(
            'rounded px-2 py-1 text-xs font-medium',
            'border border-warning',
            'text-warning',
            'hover:bg-warning-wash',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-warning',
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
        <p className="text-xs text-warning">Getting authorization link…</p>
        <button
          type="button"
          onClick={handleCancel}
          className={cn(
            'rounded px-2 py-1 text-xs',
            'border border-edge-strong',
            'text-ink',
            'hover:bg-surface-raised',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
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
        <p className="text-xs text-warning">
          Waiting for you to approve access.
        </p>
        {status?.authUrl ? (
          <div className="flex items-center gap-2 min-w-0">
            <a
              href={status.authUrl}
              target="_blank"
              rel="noopener noreferrer"
              className={cn(
                'text-xs text-accent underline truncate min-w-0 flex-1',
              )}
            >
              {status.authUrl}
            </a>
            <button
              type="button"
              aria-label="Copy authorization URL"
              className={cn(
                'rounded px-2 py-1 text-xs flex-shrink-0',
                'border border-edge-strong',
                'text-ink',
                'hover:bg-surface-raised',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
              )}
              onClick={() => {
                void navigator.clipboard.writeText(status.authUrl!)
              }}
            >
              Copy
            </button>
          </div>
        ) : (
          <p className="text-xs text-danger">Authorization link unavailable — cancel and retry.</p>
        )}
        <p className="text-xs text-ink-muted">
          Open the link on any device and approve access. The browser then tries to load a localhost
          address — that page failing to load is expected. Copy the full address from the address bar
          and paste it here.
        </p>
        <div className="flex items-center gap-2">
          <input
            type="text"
            value={redirectUrl}
            onChange={(e) => setRedirectUrl(e.target.value)}
            aria-label={`Redirect URL for ${server}`}
            placeholder="Paste the redirect URL…"
            className={cn(
              'flex-1 min-w-0 rounded border border-edge-strong',
              'bg-surface text-ink',
              'px-2 py-1 text-xs',
              'focus:outline-none focus:ring-2 focus:ring-accent',
            )}
          />
          <button
            type="button"
            className={cn(
              'rounded px-2 py-1 text-xs font-medium flex-shrink-0',
              'bg-accent text-accent-ink hover:bg-accent/90',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
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
              'border border-edge-strong',
              'text-ink',
              'hover:bg-surface-raised',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
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
        <p className="text-xs text-warning">Completing authentication…</p>
        <button
          type="button"
          onClick={handleCancel}
          className={cn(
            'rounded px-2 py-1 text-xs',
            'border border-edge-strong',
            'text-ink',
            'hover:bg-surface-raised',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
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
        <p className="text-xs text-success">
          Authorized — cleared for {count} account(s).
        </p>
      </div>
    )
  }

  // Error
  if (state === 'error') {
    return (
      <div className="mt-1 px-3 pb-2 flex flex-col gap-1">
        <p className="text-xs text-danger" role="alert">
          {status?.message ?? 'Authentication failed.'}
        </p>
        <button
          type="button"
          onClick={handleStart}
          className={cn(
            'rounded px-2 py-1 text-xs font-medium self-start',
            'border border-warning',
            'text-warning',
            'hover:bg-warning-wash',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-warning',
          )}
        >
          Retry
        </button>
      </div>
    )
  }

  return null
}
