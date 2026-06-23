/**
 * AccountsManager — self-contained accounts panel: account rows (status, usage,
 * reauth, rename, delete, refresh) plus the delete-confirmation dialog. Owns its
 * own data hooks so it can render inside the Dashboard tab panel (mobile) or the
 * desktop main pane unchanged.
 */

import * as React from 'react'
import { useState, useRef } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { RefreshCw, Pencil, Check, X, Trash2, Plus } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import { UsageBadge, StatusPill } from './UsageBadge'
import {
  useAccounts,
  useSessions,
  useRefreshAccount,
  useUpdateAccountNickname,
  useDeleteAccount,
  useReauthAccount,
  useReauthStatus,
  useCancelReauth,
  QK,
} from '@/api/hooks'
import { AddAccountDialog } from './AddAccountDialog'
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

// ── Account Row ──────────────────────────────────────────────────────────────

interface AccountRowProps {
  account: Account
  onRefresh: (name: string) => void
  refreshing: boolean
  onSaveNickname: (name: string, nickname: string | null) => void
  onDelete: (account: Account) => void
}

function AccountRow({ account, onRefresh, refreshing, onSaveNickname, onDelete }: AccountRowProps) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)

  // Reauth state: track whether we've started a reauth (controls polling)
  const [reauthStarted, setReauthStarted] = useState(false)
  // Optimistic local idle override — set when user cancels
  const [reauthOptimisticIdle, setReauthOptimisticIdle] = useState(false)

  const qc = useQueryClient()
  const reauthAccount = useReauthAccount()
  const cancelReauth = useCancelReauth()

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
    setEditing(true)
    setTimeout(() => inputRef.current?.focus(), 0)
  }

  function cancelEdit() {
    setEditing(false)
    setDraft('')
  }

  function saveEdit() {
    onSaveNickname(account.name, draft.trim() || null)
    setEditing(false)
    setDraft('')
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'Enter') { e.preventDefault(); saveEdit() }
    if (e.key === 'Escape') { e.preventDefault(); cancelEdit() }
  }

  const displayLabel = account.nickname || account.name

  const isReauthRunning = reauthStatus?.state === 'running' || reauthStatus?.state === 'cancelling'
  const isReauthSuccess = reauthStatus?.state === 'success'
  const isReauthError = reauthStatus?.state === 'error' || reauthStatus?.state === 'timeout'

  return (
    <div
      className={cn(
        'flex flex-col gap-1 rounded-lg border border-gray-200 dark:border-gray-700',
        'bg-white dark:bg-gray-800 px-3 py-2',
      )}
    >
      <div className="flex items-center gap-3">
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
            <button
              type="button"
              onClick={saveEdit}
              aria-label="Save nickname"
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
          <div className="flex-1 min-w-0 flex flex-col">
            <span className="text-sm font-medium text-gray-900 dark:text-gray-100 truncate">
              {displayLabel}
            </span>
          </div>
        )}
        {!editing && (
          <div className="flex items-center gap-3 flex-shrink-0">
            <StatusPill status={account.status} />
            <SessionsBadge counts={account.liveSessions} />
            <RankBadge rank={account.rank} reason={account.rankReason} />
            {account.usage && (
              <>
                <UsageBadge
                  pct={account.usage.session.pct}
                  label="session"
                  resetsAt={account.usage.session.resetsAt}
                />
                <UsageBadge
                  pct={account.usage.weekly.pct}
                  label="weekly"
                  resetsAt={account.usage.weekly.resetsAt}
                />
              </>
            )}
            {needsReauth && (
              <button
                type="button"
                className={cn(
                  'rounded px-2 py-1 text-xs font-medium min-h-[44px]',
                  'bg-amber-500 text-white hover:bg-amber-600',
                  'dark:bg-amber-600 dark:hover:bg-amber-500',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-500',
                  'disabled:opacity-40',
                )}
                aria-label={`Re-authenticate ${account.name}`}
                onClick={handleReauth}
                disabled={isReauthRunning}
              >
                Re-authenticate
              </button>
            )}
            {isReauthRunning && (
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
        )}
      </div>

      {/* Reauth status line */}
      {!editing && isReauthRunning && (
        <p className="text-xs text-amber-600 dark:text-amber-400">
          Opening a fresh Chrome on the host machine&rsquo;s screen — sign in there…
        </p>
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
  onSaveNickname: (name: string, nickname: string | null) => void
  onDelete: (account: Account) => void
}

function AccountsStrip({ accounts, onRefresh, refreshingName, onSaveNickname, onDelete }: AccountsStripProps) {
  if (accounts.length === 0) return null

  return (
    <div className="flex flex-col gap-2">
      {accounts.map((account) => (
        <AccountRow
          key={account.name}
          account={account}
          onRefresh={onRefresh}
          refreshing={refreshingName === account.name}
          onSaveNickname={onSaveNickname}
          onDelete={onDelete}
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
  const updateNickname = useUpdateAccountNickname()
  const deleteAccount = useDeleteAccount()

  const [refreshingAccount, setRefreshingAccount] = useState<string | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<Account | null>(null)
  const [showAdd, setShowAdd] = useState(false)

  function handleRefreshAccount(name: string) {
    setRefreshingAccount(name)
    refreshAccount.mutate(name, {
      onSettled: () => setRefreshingAccount(null),
    })
  }

  function handleSaveNickname(name: string, nickname: string | null) {
    updateNickname.mutate({ name, nickname })
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
          onSaveNickname={handleSaveNickname}
          onDelete={handleDeleteAccount}
        />
      )}

      {showAdd && <AddAccountDialog onClose={() => setShowAdd(false)} />}

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
