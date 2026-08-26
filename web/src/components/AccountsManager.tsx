/**
 * AccountsManager — self-contained accounts panel: account rows (status, usage,
 * reauth, rename, delete, refresh) plus the delete-confirmation dialog. Owns its
 * own data hooks so it can render inside the Dashboard tab panel (mobile) or the
 * desktop main pane unchanged.
 */

import * as React from 'react'
import { useState, useRef } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { RefreshCw, Pencil, Check, X, Trash2, Plus, KeyRound, Power } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import { UsageBadge, StatusPill } from './UsageBadge'
import {
  useAccounts,
  useSessions,
  useRefreshAccount,
  useUpdateAccount,
  useDeleteAccount,
  useSetAccountEnabled,
  useReauthAccount,
  useReauthStatus,
  useCancelReauth,
  useSubmitReauthCode,
  QK,
} from '@/api/hooks'
import { AddAccountDialog } from './AddAccountDialog'
import { ReauthBrowserPanel } from './ReauthBrowserPanel'
import type { Account, LiveSessionCounts, Session } from '@/api/types'

// ── Sessions + Rank Badges ───────────────────────────────────────────────────

function SessionsBadge({ counts }: { counts?: LiveSessionCounts | null }) {
  const { active, idle } = counts ?? { active: 0, idle: 0 }
  let text: string
  if (active === 0 && idle === 0) text = '—'
  else if (active > 0 && idle > 0) text = `${active} active · ${idle} idle`
  else if (active > 0) text = `${active} active`
  else text = `${idle} idle`
  return (
    <span
      className={cn(
        'text-xs whitespace-nowrap',
        active === 0 && idle === 0 ? 'text-gray-400 dark:text-gray-600' : 'text-gray-600 dark:text-gray-300',
      )}
      aria-label="Live sessions using this account"
    >
      {text}
    </span>
  )
}

function RankBadge({ rank, reason }: { rank?: number | null; reason?: string | null }) {
  if (rank == null) {
    return <span className="text-xs text-gray-400 dark:text-gray-600">unavailable</span>
  }
  const label = rank === 1 ? '#1 next' : `#${rank}`
  return (
    <span
      title={reason ?? undefined}
      className={cn(
        'text-xs font-medium rounded px-1.5 py-0.5 whitespace-nowrap',
        rank === 1
          ? 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300'
          : 'text-gray-500 dark:text-gray-400',
      )}
    >
      {label}
    </span>
  )
}

// ── Delete Account Dialog ────────────────────────────────────────────────────

interface DeleteAccountDialogProps {
  account: Account
  activeSessions: Session[]
  onClose: () => void
  onConfirm: () => void
  isPending: boolean
}

function DeleteAccountDialog({ account, activeSessions, onClose, onConfirm, isPending }: DeleteAccountDialogProps) {
  const dialogRef = useRef<HTMLDivElement>(null)
  const cancelRef = useRef<HTMLButtonElement>(null)
  const displayLabel = account.nickname || account.name

  React.useEffect(() => {
    cancelRef.current?.focus()
  }, [])

  React.useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') { onClose(); return }
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
      aria-labelledby="delete-account-dialog-title"
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
          id="delete-account-dialog-title"
          className="text-lg font-semibold text-gray-900 dark:text-gray-100"
        >
          Delete account
        </h2>

        <p className="text-sm text-gray-700 dark:text-gray-300">
          Delete account <span className="font-medium">&lsquo;{displayLabel}&rsquo;</span>? This permanently
          removes its stored credentials and config directory. This can&rsquo;t be undone.
        </p>

        {activeSessions.length > 0 && (
          <p className="text-sm text-amber-700 dark:text-amber-400" role="alert">
            {activeSessions.length} active session{activeSessions.length === 1 ? '' : 's'} use this
            account and will lose access.
          </p>
        )}

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
            {isPending ? 'Deleting…' : 'Delete'}
          </Button>
        </div>
      </div>
    </div>
  )
}

// ── Provider Chip ────────────────────────────────────────────────────────────

function CopilotChip() {
  return (
    <span
      className={cn(
        'text-xs font-medium rounded px-1.5 py-0.5 whitespace-nowrap',
        'bg-purple-100 text-purple-700 dark:bg-purple-900/40 dark:text-purple-300',
      )}
      aria-label="GitHub Copilot account"
    >
      Copilot
    </span>
  )
}

function OverReserveChip() {
  return (
    <span
      className={cn(
        'text-xs font-medium rounded px-1.5 py-0.5 whitespace-nowrap',
        'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300',
      )}
      title="Weekly usage is at/over this account's reserve — only picked as a last resort"
    >
      over reserve
    </span>
  )
}

// ── Account Row ──────────────────────────────────────────────────────────────

interface AccountRowProps {
  account: Account
  onRefresh: (name: string) => void
  refreshing: boolean
  onSave: (update: { name: string; nickname?: string | null; weeklyReservePct?: number | null }) => void
  onDelete: (account: Account) => void
  /** Called when a Copilot account's reauth button is clicked — opens the device-flow dialog. */
  onCopilotReauth?: (account: Account) => void
}

function AccountRow({ account, onRefresh, refreshing, onSave, onDelete, onCopilotReauth }: AccountRowProps) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [reserveDraft, setReserveDraft] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)

  // Reauth state: track whether we've started a reauth (controls polling)
  const [reauthStarted, setReauthStarted] = useState(false)
  // Optimistic local idle override — set when user cancels
  const [reauthOptimisticIdle, setReauthOptimisticIdle] = useState(false)
  // Code input for awaiting_code state
  const [code, setCode] = useState('')

  const qc = useQueryClient()
  const reauthAccount = useReauthAccount()
  const cancelReauth = useCancelReauth()
  const submitCode = useSubmitReauthCode()
  const setEnabled = useSetAccountEnabled()

  // Absent `enabled` (older payloads) means enabled — only an explicit false disables.
  const isDisabled = account.enabled === false

  // Poll status while reauth is running (enabled when started and not optimistically idled)
  const reauthStatusQuery = useReauthStatus(account.name, reauthStarted && !reauthOptimisticIdle)
  const reauthStatus = reauthOptimisticIdle ? undefined : reauthStatusQuery.data

  const needsReauth = account.status === 'needs-reauth' || account.status === 'needs-relogin'

  function handleReauth() {
    setReauthOptimisticIdle(false)
    setReauthStarted(true)
    reauthAccount.mutate(account.name, {
      onError: () => {
        // If start itself fails, stop polling
        setReauthStarted(false)
      },
    })
  }

  function handleCancel() {
    // Optimistically return to idle immediately, don't wait for poll
    setReauthOptimisticIdle(true)
    setReauthStarted(false)
    cancelReauth.mutate(account.name, {
      onError: () => {
        // Even on cancel error, keep optimistic idle
      },
    })
  }

  // Refresh the accounts list when reauth transitions into success. Polling stops
  // on its own once the status is terminal (see useReauthStatus.refetchInterval),
  // so this effect only reacts to the external query state — no setState here.
  const prevStatusRef = useRef<string | undefined>(undefined)
  React.useEffect(() => {
    const state = reauthStatus?.state
    if (state === 'success' && prevStatusRef.current !== 'success') {
      qc.invalidateQueries({ queryKey: QK.accounts })
    }
    prevStatusRef.current = state
  }, [reauthStatus?.state, qc])

  function startEdit() {
    setDraft(account.nickname ?? '')
    setReserveDraft(account.weeklyReservePct != null ? String(account.weeklyReservePct) : '')
    setEditing(true)
    setTimeout(() => inputRef.current?.focus(), 0)
  }

  function cancelEdit() {
    setEditing(false)
    setDraft('')
    setReserveDraft('')
  }

  function saveEdit() {
    const trimmed = reserveDraft.trim()
    let reserve: number | null = null
    if (trimmed !== '') {
      const parsed = Number(trimmed)
      if (Number.isFinite(parsed) && parsed > 0) reserve = Math.min(100, Math.max(1, parsed))
    }
    onSave({ name: account.name, nickname: draft.trim() || null, weeklyReservePct: reserve })
    setEditing(false)
    setDraft('')
    setReserveDraft('')
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'Enter') { e.preventDefault(); saveEdit() }
    if (e.key === 'Escape') { e.preventDefault(); cancelEdit() }
  }

  const displayLabel = account.nickname || account.name

  const isReauthRunning = reauthStatus?.state === 'running' || reauthStatus?.state === 'cancelling'
  const isAwaitingCode = reauthStatus?.state === 'awaiting_code'
  const isReauthSuccess = reauthStatus?.state === 'success'
  const isReauthError = reauthStatus?.state === 'error' || reauthStatus?.state === 'timeout'

  return (
    <div
      className={cn(
        'flex flex-col gap-1 rounded-lg border border-gray-200 dark:border-gray-700',
        'bg-white dark:bg-gray-800 px-3 py-2',
      )}
    >
      {/* Top row: account name / edit inputs + action icon buttons */}
      <div className="flex items-center gap-2">
        {editing ? (
          <div className="flex-1 flex items-center gap-2 min-w-0">
            <input
              ref={inputRef}
              type="text"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={handleKeyDown}
              aria-label={`Nickname for ${account.name}`}
              placeholder={account.name}
              className={cn(
                'flex-1 min-w-0 rounded border border-gray-300 dark:border-gray-600',
                'bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100',
                'px-2 py-1 text-sm',
                'focus:outline-none focus:ring-2 focus:ring-blue-500',
              )}
            />
            <input
              type="number"
              min={1}
              max={100}
              value={reserveDraft}
              onChange={(e) => setReserveDraft(e.target.value)}
              onKeyDown={handleKeyDown}
              aria-label={`Weekly reserve % for ${account.name}`}
              placeholder="reserve %"
              title="Stop auto-selecting this account once weekly usage reaches this % (blank = no reserve)"
              className={cn(
                'w-24 rounded border border-gray-300 dark:border-gray-600',
                'bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100',
                'px-2 py-1 text-sm',
                'focus:outline-none focus:ring-2 focus:ring-blue-500',
              )}
            />
            <button
              type="button"
              onClick={saveEdit}
              aria-label="Save account settings"
              className={cn(
                'min-h-[44px] min-w-[44px] flex items-center justify-center rounded',
                'text-green-600 dark:text-green-400',
                'hover:bg-gray-100 dark:hover:bg-gray-700',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
              )}
            >
              <Check size={14} aria-hidden="true" />
            </button>
            <button
              type="button"
              onClick={cancelEdit}
              aria-label="Cancel rename"
              className={cn(
                'min-h-[44px] min-w-[44px] flex items-center justify-center rounded',
                'text-gray-500 dark:text-gray-400',
                'hover:bg-gray-100 dark:hover:bg-gray-700',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
              )}
            >
              <X size={14} aria-hidden="true" />
            </button>
          </div>
        ) : (
          <>
            <div className="flex-1 min-w-0 flex items-center gap-2">
              <span className={cn(
                'text-sm font-medium text-gray-900 dark:text-gray-100 truncate',
                isDisabled && 'opacity-50 line-through',
              )}>
                {displayLabel}
              </span>
              {account.provider === 'copilot' && <CopilotChip />}
            </div>
            {/* Action icon buttons — always icon-only so they don't crowd the name */}
            <div className="flex items-center gap-1 flex-shrink-0">
              <button
                type="button"
                className={cn(
                  'min-h-[44px] min-w-[44px] flex items-center justify-center rounded',
                  isDisabled ? 'text-green-600 dark:text-green-400' : 'text-gray-500 dark:text-gray-400',
                  'hover:bg-gray-100 dark:hover:bg-gray-700',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
                  'disabled:opacity-40',
                )}
                aria-label={`${isDisabled ? 'Enable' : 'Disable'} account ${account.name}`}
                title={isDisabled
                  ? 'Re-enable this account for selection'
                  : 'Pause this account — kept intact but never selected until re-enabled'}
                disabled={setEnabled.isPending}
                onClick={() => setEnabled.mutate({ name: account.name, enabled: isDisabled })}
              >
                <Power size={14} aria-hidden="true" />
              </button>
              <button
                type="button"
                className={cn(
                  'min-h-[44px] min-w-[44px] flex items-center justify-center rounded',
                  'text-gray-500 dark:text-gray-400',
                  'hover:bg-gray-100 dark:hover:bg-gray-700',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
                )}
                aria-label={`Rename ${account.name}`}
                onClick={startEdit}
              >
                <Pencil size={14} aria-hidden="true" />
              </button>
              <button
                type="button"
                className={cn(
                  'min-h-[44px] min-w-[44px] flex items-center justify-center rounded',
                  'text-red-500 dark:text-red-400',
                  'hover:bg-red-50 dark:hover:bg-red-900/30',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-500',
                )}
                aria-label={`Delete account ${account.name}`}
                onClick={() => onDelete(account)}
              >
                <Trash2 size={14} aria-hidden="true" />
              </button>
              <button
                type="button"
                className={cn(
                  'h-8 w-8 flex items-center justify-center rounded',
                  'text-gray-500 dark:text-gray-400',
                  'hover:bg-gray-100 dark:hover:bg-gray-700',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
                  'disabled:opacity-40',
                )}
                aria-label={`Refresh account ${account.name}`}
                disabled={refreshing}
                onClick={() => onRefresh(account.name)}
              >
                <RefreshCw
                  size={14}
                  aria-hidden="true"
                  className={cn(refreshing && 'animate-spin')}
                />
              </button>
            </div>
          </>
        )}
      </div>

      {/* Badge row: status + usage + reauth — wraps naturally on narrow screens */}
      {!editing && (
        <div className="flex items-center flex-wrap gap-x-2 gap-y-1">
          <StatusPill status={account.status} />
          {isDisabled && (
            <span
              className="rounded px-1.5 py-0.5 text-xs font-medium bg-gray-200 text-gray-600 dark:bg-gray-700 dark:text-gray-300"
            >
              Disabled
            </span>
          )}
          <SessionsBadge counts={account.liveSessions} />
          <RankBadge rank={account.rank} reason={account.rankReason} />
          {account.weeklyReservePct != null && account.usage != null &&
            account.usage.weekly.pct >= account.weeklyReservePct && <OverReserveChip />}
          {account.usage && (
            <>
              <UsageBadge
                pct={account.usage.session.pct}
                label={account.provider === 'copilot' ? 'premium-req' : 'session'}
                resetsAt={account.usage.session.resetsAt}
              />
              <UsageBadge
                pct={account.usage.weekly.pct}
                label="weekly"
                resetsAt={account.usage.weekly.resetsAt}
                markerPct={account.weeklyReservePct ?? null}
              />
            </>
          )}
          {account.provider === 'copilot' ? (
            !isReauthRunning && !isAwaitingCode && (
              <button
                type="button"
                className={cn(
                  'rounded min-h-[44px] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-500',
                  needsReauth
                    ? 'px-2 py-1 text-xs font-medium bg-amber-500 text-white hover:bg-amber-600 dark:bg-amber-600 dark:hover:bg-amber-500'
                    : 'min-w-[44px] flex items-center justify-center text-gray-400 dark:text-gray-500 hover:text-amber-500 dark:hover:text-amber-400 hover:bg-gray-100 dark:hover:bg-gray-700',
                )}
                aria-label={`Re-run device flow for ${account.name}`}
                onClick={() => onCopilotReauth?.(account)}
              >
                {needsReauth ? 'Re-authenticate' : <KeyRound size={14} aria-hidden="true" />}
              </button>
            )
          ) : (
            !isReauthRunning && !isAwaitingCode && (
              <button
                type="button"
                className={cn(
                  'rounded min-h-[44px] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-500',
                  needsReauth
                    ? 'px-2 py-1 text-xs font-medium bg-amber-500 text-white hover:bg-amber-600 dark:bg-amber-600 dark:hover:bg-amber-500'
                    : 'min-w-[44px] flex items-center justify-center text-gray-400 dark:text-gray-500 hover:text-amber-500 dark:hover:text-amber-400 hover:bg-gray-100 dark:hover:bg-gray-700',
                )}
                aria-label={`Re-authenticate ${account.name}`}
                onClick={handleReauth}
              >
                {needsReauth ? 'Re-authenticate' : <KeyRound size={14} aria-hidden="true" />}
              </button>
            )
          )}
          {(isReauthRunning || isAwaitingCode) && (
            <button
              type="button"
              className={cn(
                'rounded px-2 py-1 text-xs font-medium min-h-[44px]',
                'border border-gray-300 dark:border-gray-600',
                'text-gray-700 dark:text-gray-300',
                'hover:bg-gray-100 dark:hover:bg-gray-700',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
              )}
              aria-label="Cancel reauth"
              onClick={handleCancel}
            >
              Cancel
            </button>
          )}
        </div>
      )}

      {/* Generate long-lived (setup-token) — Anthropic accounts only */}
      {!editing && account.provider !== 'copilot' && (
        <div className="flex justify-end">
          <button
            type="button"
            className={cn(
              'rounded px-2 py-1 text-xs min-h-[44px]',
              'text-gray-500 dark:text-gray-400',
              'hover:text-gray-700 dark:hover:text-gray-200',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
              'disabled:opacity-40',
            )}
            aria-label={`Generate long-lived token for ${account.name}`}
            onClick={() => {
              setReauthOptimisticIdle(false)
              setReauthStarted(true)
              reauthAccount.mutate(
                { name: account.name, mode: 'setup_token' },
                { onError: () => setReauthStarted(false) },
              )
            }}
            disabled={isReauthRunning || isAwaitingCode}
          >
            Generate long-lived token
          </button>
        </div>
      )}

      {/* Reauth status line */}
      {!editing && isReauthRunning && (
        reauthStatus?.vncSession ? (
          <>
            <p className="text-xs text-amber-600 dark:text-amber-400">
              Sign in to Claude in the window below…
            </p>
            <ReauthBrowserPanel vncSession={reauthStatus.vncSession} />
          </>
        ) : (
          // Before the virtual display exists there is nothing to render, and the
          // wait (profile copy, Xvfb spawn, Chrome cold start) is several seconds.
          // The backend publishes a step label so this window isn't silent — and so
          // it doesn't claim "on the host screen" while a virtual display is starting.
          <p className="text-xs text-amber-600 dark:text-amber-400">
            {reauthStatus?.message ?? 'Opening Chrome on the host screen — sign in there…'}
          </p>
        )
      )}
      {!editing && isAwaitingCode && reauthStatus?.authUrl && (
        <div className="flex flex-col gap-2 mt-1">
          <p className="text-xs text-amber-600 dark:text-amber-400">
            Open this URL, approve, then paste the code below.
          </p>
          <div className="flex items-center gap-2 min-w-0">
            <a
              href={reauthStatus.authUrl}
              target="_blank"
              rel="noopener noreferrer"
              className={cn(
                'text-xs text-blue-600 dark:text-blue-400 underline truncate min-w-0 flex-1',
              )}
            >
              {reauthStatus.authUrl}
            </a>
            <button
              type="button"
              className={cn(
                'rounded px-2 py-1 text-xs flex-shrink-0',
                'border border-gray-300 dark:border-gray-600',
                'text-gray-700 dark:text-gray-300',
                'hover:bg-gray-100 dark:hover:bg-gray-700',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
              )}
              onClick={() => { void navigator.clipboard.writeText(reauthStatus.authUrl!) }}
            >
              Copy
            </button>
          </div>
          <div className="flex items-center gap-2">
            <input
              type="text"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              aria-label={`Authorization code for ${account.name}`}
              placeholder="Paste authorization code…"
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
              disabled={!code.trim()}
              onClick={() => {
                submitCode.mutate(
                  { name: account.name, code: code.trim() },
                  { onSuccess: () => setCode('') },
                )
              }}
            >
              Submit code
            </button>
          </div>
        </div>
      )}
      {!editing && isReauthSuccess && (
        <p className="text-xs text-green-600 dark:text-green-400">
          ✓ Re-authentication successful — account refreshed.
        </p>
      )}
      {!editing && isReauthError && reauthStatus?.message && (
        <p className="text-xs text-red-600 dark:text-red-400" role="alert">
          {reauthStatus.message}
        </p>
      )}
    </div>
  )
}

// ── Accounts Strip ───────────────────────────────────────────────────────────

interface AccountsStripProps {
  accounts: Account[]
  onRefresh: (name: string) => void
  refreshingName: string | null
  onSave: (update: { name: string; nickname?: string | null; weeklyReservePct?: number | null }) => void
  onDelete: (account: Account) => void
  onCopilotReauth: (account: Account) => void
}

function AccountsStrip({ accounts, onRefresh, refreshingName, onSave, onDelete, onCopilotReauth }: AccountsStripProps) {
  if (accounts.length === 0) return null

  return (
    <div className="flex flex-col gap-2">
      {accounts.map((account) => (
        <AccountRow
          key={account.name}
          account={account}
          onRefresh={onRefresh}
          refreshing={refreshingName === account.name}
          onSave={onSave}
          onDelete={onDelete}
          onCopilotReauth={onCopilotReauth}
        />
      ))}
    </div>
  )
}

// ── AccountsManager ──────────────────────────────────────────────────────────

export function AccountsManager() {
  const { data: accounts = [], isLoading: accountsLoading } = useAccounts()
  const { data: sessions = [] } = useSessions()
  const refreshAccount = useRefreshAccount()
  const updateAccount = useUpdateAccount()
  const deleteAccount = useDeleteAccount()

  const [refreshingAccount, setRefreshingAccount] = useState<string | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<Account | null>(null)
  const [showAdd, setShowAdd] = useState(false)
  /** Non-null when a Copilot account's reauth button was clicked — drives AddAccountDialog pre-selection. */
  const [copilotReauthAccount, setCopilotReauthAccount] = useState<Account | null>(null)

  function handleRefreshAccount(name: string) {
    setRefreshingAccount(name)
    refreshAccount.mutate(name, {
      onSettled: () => setRefreshingAccount(null),
    })
  }

  function handleSave(update: { name: string; nickname?: string | null; weeklyReservePct?: number | null }) {
    updateAccount.mutate(update)
  }

  function handleDeleteAccount(account: Account) {
    setDeleteTarget(account)
  }

  function confirmDelete() {
    if (!deleteTarget) return
    deleteAccount.mutate(deleteTarget.name, {
      onSuccess: () => setDeleteTarget(null),
      onError: () => setDeleteTarget(null),
    })
  }

  const deleteTargetSessions = deleteTarget
    ? sessions.filter((s) => s.accountName === deleteTarget.name)
    : []

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
          Accounts
        </h2>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => setShowAdd(true)}
          className="gap-1.5"
        >
          <Plus size={14} aria-hidden="true" />
          Add account
        </Button>
      </div>
      {!accountsLoading && (
        <AccountsStrip
          accounts={accounts}
          onRefresh={handleRefreshAccount}
          refreshingName={refreshingAccount}
          onSave={handleSave}
          onDelete={handleDeleteAccount}
          onCopilotReauth={(acct) => setCopilotReauthAccount(acct)}
        />
      )}

      {showAdd && <AddAccountDialog onClose={() => setShowAdd(false)} />}
      {copilotReauthAccount && (
        <AddAccountDialog
          onClose={() => setCopilotReauthAccount(null)}
          initialProvider="copilot"
          initialName={copilotReauthAccount.name}
        />
      )}

      {deleteTarget && (
        <DeleteAccountDialog
          account={deleteTarget}
          activeSessions={deleteTargetSessions}
          onClose={() => setDeleteTarget(null)}
          onConfirm={confirmDelete}
          isPending={deleteAccount.isPending}
        />
      )}
    </div>
  )
}
