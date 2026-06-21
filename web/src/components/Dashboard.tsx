import * as React from 'react'
import { useState, useRef, useCallback, useEffect } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Plus, RefreshCw, Settings, Pencil, Check, X, Trash2 } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import { UsageBadge, StatusPill } from './UsageBadge'
import { SessionList } from './SessionList'
import { AllLimitedBanner } from './AllLimitedBanner'
import { SettingsPanel } from './settings/SettingsPanel'
import { DispatchBar } from './DispatchBar'
import { ShortcutsHelp } from './ShortcutsHelp'
import {
  useAccounts,
  useSessions,
  useCreateSession,
  useRenameSession,
  useCloseSession,
  useRefreshAccount,
  useUpdateAccountNickname,
  useDeleteAccount,
  useReauthAccount,
  useReauthStatus,
  useCancelReauth,
  useGithubRepos,
  useGithubBranches,
  useSettings,
  useArchivedSessions,
  useUnarchiveSession,
  usePermanentDeleteSession,
  QK,
  type CreateSessionInput,
} from '@/api/hooks'
import type { Account, Session, ArchivedSession } from '@/api/types'
import { PERMISSION_MODES } from '@/session/permissionModes'
import { setLastRepo } from '@/lib/lastRepo'

// ── New Session Dialog ──────────────────────────────────────────────────────

interface NewSessionDialogProps {
  accounts: Account[]
  onClose: () => void
  onSubmit: (input: CreateSessionInput) => void
  isPending: boolean
  externalError?: string | null
  defaultPermissionMode?: string
}

function NewSessionDialog({ accounts, onClose, onSubmit, isPending, externalError, defaultPermissionMode }: NewSessionDialogProps) {
  const [name, setName] = useState('')
  const [account, setAccount] = useState('')
  const [initialPrompt, setInitialPrompt] = useState('')
  const [permissionMode, setPermissionMode] = useState(defaultPermissionMode ?? 'interactive')
  const [inlineError, setInlineError] = useState<string | null>(null)
  const firstInputRef = useRef<HTMLInputElement>(null)
  const dialogRef = useRef<HTMLDivElement>(null)

  // GitHub repo/branch picker state
  const [repoFilter, setRepoFilter] = useState('')
  const [selectedRepo, setSelectedRepo] = useState('')
  const [selectedBranch, setSelectedBranch] = useState('')

  const reposQuery = useGithubRepos()
  const branchesQuery = useGithubBranches(selectedRepo || undefined)

  const selectedRepoInfo = (reposQuery.data ?? []).find((r) => r.nameWithOwner === selectedRepo)

  const filteredRepos = (reposQuery.data ?? []).filter((r) =>
    r.nameWithOwner.toLowerCase().includes(repoFilter.trim().toLowerCase()),
  )

  // When branches load, default to the repo's default branch.
  React.useEffect(() => {
    if (!selectedBranch && (branchesQuery.data?.default || selectedRepoInfo?.defaultBranch)) {
      setSelectedBranch(branchesQuery.data?.default ?? selectedRepoInfo?.defaultBranch ?? '')
    }
  }, [branchesQuery.data?.default, selectedRepoInfo?.defaultBranch])

  // Reset branch when repo changes.
  function handleRepoChange(nameWithOwner: string) {
    setSelectedRepo(nameWithOwner)
    setSelectedBranch('')
  }

  React.useEffect(() => {
    firstInputRef.current?.focus()
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

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setInlineError(null)
    onSubmit({
      name: name.trim() || undefined,
      account: account || undefined,
      initialPrompt: initialPrompt.trim() || undefined,
      repo: selectedRepo || undefined,
      branch: selectedBranch || undefined,
      permissionMode,
    })
  }

  const inputClass = cn(
    'w-full rounded-md border border-gray-300 dark:border-gray-600',
    'bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100',
    'px-3 py-2 text-sm min-h-[44px]',
    'focus:outline-none focus:ring-2 focus:ring-blue-500',
    'placeholder:text-gray-400 dark:placeholder:text-gray-500',
  )

  return (
    <div
      className="fixed inset-0 z-50 flex items-end sm:items-center justify-center p-4 bg-black/50"
      role="dialog"
      aria-modal="true"
      aria-labelledby="new-session-dialog-title"
      onClick={(e) => { if (e.target === e.currentTarget) onClose() }}
    >
      <div
        ref={dialogRef}
        className={cn(
          'w-full max-w-md rounded-xl',
          'bg-white dark:bg-gray-800',
          'border border-gray-200 dark:border-gray-600',
          'shadow-xl p-6 space-y-4',
          'max-h-[90vh] overflow-y-auto',
        )}
      >
        <h2 id="new-session-dialog-title" className="text-lg font-semibold text-gray-900 dark:text-gray-100">
          New Session
        </h2>

        <form onSubmit={handleSubmit} className="space-y-4">
          {/* Name */}
          <div className="space-y-1">
            <label htmlFor="new-session-name" className="block text-sm font-medium text-gray-700 dark:text-gray-300">
              Name <span className="text-gray-400">(optional)</span>
            </label>
            <input
              ref={firstInputRef}
              id="new-session-name"
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="My session"
              className={inputClass}
            />
          </div>

          {/* Repository filter */}
          <div className="space-y-1">
            <label htmlFor="new-session-repo-filter" className="block text-sm font-medium text-gray-700 dark:text-gray-300">
              Filter repos <span className="text-gray-400">(optional)</span>
            </label>
            <input
              id="new-session-repo-filter"
              type="text"
              value={repoFilter}
              onChange={(e) => setRepoFilter(e.target.value)}
              placeholder="owner/name…"
              className={inputClass}
              autoComplete="off"
            />
          </div>

          {/* Repository */}
          <div className="space-y-1">
            <label htmlFor="new-session-repo" className="block text-sm font-medium text-gray-700 dark:text-gray-300">
              Repository <span className="text-gray-400">(optional)</span>
            </label>
            <select
              id="new-session-repo"
              value={selectedRepo}
              onChange={(e) => handleRepoChange(e.target.value)}
              className={inputClass}
              disabled={reposQuery.isLoading}
              aria-busy={reposQuery.isLoading}
            >
              <option value="">
                {reposQuery.isLoading
                  ? 'Loading…'
                  : reposQuery.isError
                    ? '— could not load repos —'
                    : '— select repo —'}
              </option>
              {filteredRepos.map((repo) => (
                <option key={repo.nameWithOwner} value={repo.nameWithOwner}>
                  {repo.nameWithOwner}
                  {repo.isPrivate ? ' 🔒' : ''}
                  {repo.cloned ? ' · ✓ cloned' : ' · ↓ clones on start'}
                </option>
              ))}
            </select>
            {selectedRepoInfo && (
              <p className="text-xs text-gray-400">
                {selectedRepoInfo.isPrivate ? 'Private · ' : ''}
                {selectedRepoInfo.cloned
                  ? '✓ Already cloned into ~/GitHub/.ccswitch'
                  : '↓ Will be cloned into ~/GitHub/.ccswitch on first start (one-time; may take a moment for large repos)'}
              </p>
            )}
          </div>

          {/* Branch */}
          <div className="space-y-1">
            <label htmlFor="new-session-branch" className="block text-sm font-medium text-gray-700 dark:text-gray-300">
              Branch <span className="text-gray-400">(optional)</span>
            </label>
            <select
              id="new-session-branch"
              value={selectedBranch}
              onChange={(e) => setSelectedBranch(e.target.value)}
              className={cn(inputClass, !selectedRepo && 'opacity-50')}
              disabled={!selectedRepo || branchesQuery.isLoading}
              aria-busy={branchesQuery.isLoading}
            >
              <option value="">{branchesQuery.isLoading ? 'Loading…' : '— default —'}</option>
              {(branchesQuery.data?.branches ?? []).map((b) => (
                <option key={b} value={b}>{b}</option>
              ))}
            </select>
            <p className="text-xs text-gray-400">
              Clones and worktrees live under <code>~/GitHub/.ccswitch</code> — your real
              checkouts are never touched. Each session runs in its own isolated git
              worktree on the selected branch.
            </p>
          </div>

          {/* Account */}
          <div className="space-y-1">
            <label htmlFor="new-session-account" className="block text-sm font-medium text-gray-700 dark:text-gray-300">
              Account <span className="text-gray-400">(optional)</span>
            </label>
            <select
              id="new-session-account"
              value={account}
              onChange={(e) => setAccount(e.target.value)}
              className={inputClass}
            >
              <option value="">Auto-select</option>
              {accounts.map((a) => (
                <option key={a.name} value={a.name}>{a.nickname || a.name}</option>
              ))}
            </select>
          </div>

          {/* Tool approvals */}
          <div className="space-y-1">
            <label htmlFor="new-session-permission" className="block text-sm font-medium text-gray-700 dark:text-gray-300">
              Tool approvals
            </label>
            <select
              id="new-session-permission"
              value={permissionMode}
              onChange={(e) => setPermissionMode(e.target.value)}
              className={inputClass}
            >
              {PERMISSION_MODES.map(({ value, label }) => (
                <option key={value} value={value}>{label}</option>
              ))}
            </select>
          </div>

          {/* Initial prompt */}
          <div className="space-y-1">
            <label htmlFor="new-session-prompt" className="block text-sm font-medium text-gray-700 dark:text-gray-300">
              Initial prompt <span className="text-gray-400">(optional)</span>
            </label>
            <textarea
              id="new-session-prompt"
              value={initialPrompt}
              onChange={(e) => setInitialPrompt(e.target.value)}
              rows={3}
              placeholder="Start with…"
              className={cn(
                'w-full rounded-md border border-gray-300 dark:border-gray-600',
                'bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100',
                'px-3 py-2 text-sm resize-none',
                'focus:outline-none focus:ring-2 focus:ring-blue-500',
                'placeholder:text-gray-400 dark:placeholder:text-gray-500',
              )}
            />
          </div>

          {(inlineError || externalError) && (
            <p role="alert" className="text-sm text-red-600 dark:text-red-400">{inlineError ?? externalError}</p>
          )}

          <div className="flex gap-3 justify-end pt-2">
            <Button type="button" variant="outline" onClick={onClose} disabled={isPending}>Cancel</Button>
            <Button type="submit" disabled={isPending} className="min-h-[44px]">
              {isPending ? 'Creating…' : 'Create'}
            </Button>
          </div>
        </form>
      </div>
    </div>
  )
}

// ── Rename Dialog ────────────────────────────────────────────────────────────

interface RenameDialogProps {
  sessionId: string
  currentName: string
  onClose: () => void
  onSubmit: (id: string, name: string) => void
  isPending: boolean
}

function RenameDialog({ sessionId, currentName, onClose, onSubmit, isPending }: RenameDialogProps) {
  const [name, setName] = useState(currentName)
  const inputRef = useRef<HTMLInputElement>(null)

  React.useEffect(() => {
    inputRef.current?.select()
  }, [])

  React.useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [onClose])

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (name.trim()) onSubmit(sessionId, name.trim())
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-end sm:items-center justify-center p-4 bg-black/50"
      role="dialog"
      aria-modal="true"
      aria-labelledby="rename-dialog-title"
      onClick={(e) => { if (e.target === e.currentTarget) onClose() }}
    >
      <div
        className={cn(
          'w-full max-w-sm rounded-xl',
          'bg-white dark:bg-gray-800',
          'border border-gray-200 dark:border-gray-600',
          'shadow-xl p-6 space-y-4',
        )}
      >
        <h2
          id="rename-dialog-title"
          className="text-lg font-semibold text-gray-900 dark:text-gray-100"
        >
          Rename Session
        </h2>
        <form onSubmit={handleSubmit} className="space-y-4">
          <input
            ref={inputRef}
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            aria-label="Session name"
            className={cn(
              'w-full rounded-md border border-gray-300 dark:border-gray-600',
              'bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100',
              'px-3 py-2 text-sm min-h-[44px]',
              'focus:outline-none focus:ring-2 focus:ring-blue-500',
            )}
          />
          <div className="flex gap-3 justify-end">
            <Button type="button" variant="outline" onClick={onClose} disabled={isPending}>
              Cancel
            </Button>
            <Button type="submit" disabled={isPending || !name.trim()} className="min-h-[44px]">
              {isPending ? 'Saving…' : 'Save'}
            </Button>
          </div>
        </form>
      </div>
    </div>
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
          Delete account <span className="font-medium">'{displayLabel}'</span>? This permanently
          removes its stored credentials and config directory. This can't be undone.
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

// ── Accounts Strip ───────────────────────────────────────────────────────────

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

  // When status reaches a terminal state, stop polling; also invalidate on success
  const prevStatusRef = useRef<string | undefined>(undefined)
  React.useEffect(() => {
    const state = reauthStatus?.state
    const isTerminal = state === 'success' || state === 'error' || state === 'timeout'
    if (isTerminal) {
      // Stop polling — disabling the query keeps last data in cache (gcTime default 5 min)
      // so the success ✓ / error message stays visible after polling halts.
      setReauthStarted(false)
    }
    if (state === 'success' && prevStatusRef.current !== 'success') {
      qc.invalidateQueries({ queryKey: QK.accounts })
    }
    prevStatusRef.current = state
  }, [reauthStatus?.state, qc])

  function startEdit() {
    setDraft(account.nickname ?? '')
    setEditing(true)
    // Focus after render
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
            {account.nickname && (
              <span className="text-xs text-gray-400 dark:text-gray-500 truncate">
                {account.name}
              </span>
            )}
          </div>
        )}
        {!editing && (
          <div className="flex items-center gap-3 flex-shrink-0">
            <StatusPill status={account.status} />
            {account.usage && (
              <>
                <UsageBadge pct={account.usage.session.pct} label="session" />
                <UsageBadge pct={account.usage.weekly.pct} label="weekly" />
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
          Opening a fresh Chrome on the host machine's screen — sign in there…
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

// ── Permanent Delete Dialog ───────────────────────────────────────────────────

interface PermanentDeleteDialogProps {
  sessionName: string
  onClose: () => void
  onConfirm: () => void
  isPending: boolean
}

function PermanentDeleteDialog({ sessionName, onClose, onConfirm, isPending }: PermanentDeleteDialogProps) {
  const dialogRef = useRef<HTMLDivElement>(null)
  const cancelRef = useRef<HTMLButtonElement>(null)

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

// ── Archived List ──────────────────────────────────────────────────────────────

interface ArchivedListProps {
  sessions: ArchivedSession[]
  onReview: (id: string) => void
  onUnarchive: (id: string) => void
  onPermanentDelete: (session: ArchivedSession) => void
  unarchivingId: string | null
}

function ArchivedList({ sessions, onReview, onUnarchive, onPermanentDelete, unarchivingId }: ArchivedListProps) {
  if (sessions.length === 0) {
    return (
      <p className="text-sm text-gray-500 dark:text-gray-400 py-6 text-center">
        No archived sessions.
      </p>
    )
  }

  return (
    <ul className="space-y-2" aria-label="Archived sessions">
      {sessions.map((s) => {
        const displayName = s.name || s.id.slice(0, 8)
        const archivedDate = new Date(s.archivedAt).toLocaleDateString(undefined, {
          year: 'numeric', month: 'short', day: 'numeric',
        })
        return (
          <li
            key={s.id}
            className={cn(
              'flex flex-col gap-2 rounded-lg border border-gray-200 dark:border-gray-700',
              'bg-white dark:bg-gray-800 px-3 py-2',
            )}
          >
            <div className="flex items-start justify-between gap-3">
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium text-gray-900 dark:text-gray-100 truncate">{displayName}</p>
                <p className="text-xs text-gray-500 dark:text-gray-400 truncate">
                  {s.account} · Archived {archivedDate}
                </p>
              </div>
              <div className="flex items-center gap-2 flex-shrink-0">
                <button
                  type="button"
                  onClick={() => onReview(s.id)}
                  className={cn(
                    'rounded px-2 py-1 text-xs font-medium min-h-[44px]',
                    'border border-gray-300 dark:border-gray-600',
                    'text-gray-700 dark:text-gray-300',
                    'hover:bg-gray-100 dark:hover:bg-gray-700',
                    'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
                  )}
                  aria-label={`Review ${displayName}`}
                >
                  Review
                </button>
                <button
                  type="button"
                  onClick={() => onUnarchive(s.id)}
                  disabled={unarchivingId === s.id}
                  className={cn(
                    'rounded px-2 py-1 text-xs font-medium min-h-[44px]',
                    'bg-blue-600 hover:bg-blue-700 text-white',
                    'dark:bg-blue-700 dark:hover:bg-blue-600',
                    'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
                    'disabled:opacity-40',
                  )}
                  aria-label={`Unarchive ${displayName}`}
                >
                  {unarchivingId === s.id ? 'Restoring…' : 'Unarchive'}
                </button>
                <button
                  type="button"
                  onClick={() => onPermanentDelete(s)}
                  className={cn(
                    'min-h-[44px] min-w-[44px] flex items-center justify-center rounded',
                    'text-red-500 dark:text-red-400',
                    'hover:bg-red-50 dark:hover:bg-red-900/30',
                    'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-500',
                  )}
                  aria-label={`Delete ${displayName} permanently`}
                >
                  <Trash2 size={14} aria-hidden="true" />
                </button>
              </div>
            </div>
          </li>
        )
      })}
    </ul>
  )
}

// ── Dashboard ────────────────────────────────────────────────────────────────

type TabId = 'sessions' | 'accounts' | 'archived'

interface DashboardProps {
  /** Called when the user opens a session (navigates to Terminal view). */
  onOpenSession?: (sessionId: string) => void
  /** Bump this to re-focus the dispatch bar (e.g. after returning from a session). */
  focusDispatchSignal?: unknown
  /** Called when the user wants to review an archived session. */
  onReviewSession?: (sessionId: string) => void
  /** If set, the archived tab is selected on mount. */
  initialTab?: TabId
}

export function Dashboard({ onOpenSession, focusDispatchSignal, onReviewSession, initialTab }: DashboardProps) {
  const { data: accounts = [], isLoading: accountsLoading } = useAccounts()
  const { data: sessions = [], isLoading: sessionsLoading } = useSessions()
  const { data: archivedSessions = [] } = useArchivedSessions()
  const { data: settingsData } = useSettings()
  const reposQuery = useGithubRepos()

  const createSession = useCreateSession()
  const renameSession = useRenameSession()
  const closeSession = useCloseSession()
  const refreshAccount = useRefreshAccount()
  const updateNickname = useUpdateAccountNickname()
  const deleteAccount = useDeleteAccount()
  const unarchiveSession = useUnarchiveSession()
  const permanentDeleteSession = usePermanentDeleteSession()

  const [activeTab, setActiveTab] = useState<TabId>(initialTab ?? 'sessions')
  const [showNewDialog, setShowNewDialog] = useState(false)
  const [showSettings, setShowSettings] = useState(false)
  const [showHelp, setShowHelp] = useState(false)
  const [renameTarget, setRenameTarget] = useState<{ id: string; name: string } | null>(null)
  const [allLimitedReset, setAllLimitedReset] = useState<string | null | undefined>(undefined)
  const [refreshingAccount, setRefreshingAccount] = useState<string | null>(null)
  const [branchError, setBranchError] = useState<string | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<Account | null>(null)
  const [permDeleteTarget, setPermDeleteTarget] = useState<ArchivedSession | null>(null)
  const [unarchivingId, setUnarchivingId] = useState<string | null>(null)

  // ── Keyboard selection state ─────────────────────────────────────────────────
  const [highlightedIndex, setHighlightedIndex] = useState(-1)
  const selectedSession = highlightedIndex >= 0 ? sessions[highlightedIndex] : undefined
  const selectedId = selectedSession?.id ?? null

  // ── Press-twice delete guard ──────────────────────────────────────────────────
  const [pendingDelete, setPendingDelete] = useState<{ id: string; ts: number } | null>(null)
  const pendingDeleteTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Clear any pending-delete timeout on unmount
  useEffect(() => {
    return () => {
      if (pendingDeleteTimeoutRef.current) clearTimeout(pendingDeleteTimeoutRef.current)
    }
  }, [])

  // Clamp highlighted index during render when the sessions list shrinks.
  // Doing this at render time (not in an effect) avoids the set-state-in-effect
  // lint rule while still being safe — React re-renders synchronously when state
  // set during render differs from the current value (React 18 batching).
  const clampedIndex =
    sessions.length === 0 ? -1 : Math.min(highlightedIndex, sessions.length - 1)
  if (clampedIndex !== highlightedIndex) {
    setHighlightedIndex(clampedIndex)
  }

  const newButtonRef = useRef<HTMLButtonElement>(null)
  const settingsButtonRef = useRef<HTMLButtonElement>(null)
  const sessionsTabRef = useRef<HTMLButtonElement>(null)
  const accountsTabRef = useRef<HTMLButtonElement>(null)
  const archivedTabRef = useRef<HTMLButtonElement>(null)

  const tabRefs: Record<TabId, React.RefObject<HTMLButtonElement | null>> = {
    sessions: sessionsTabRef,
    accounts: accountsTabRef,
    archived: archivedTabRef,
  }
  const TAB_ORDER: TabId[] = ['sessions', 'accounts', 'archived']

  function handleTabKeyDown(e: React.KeyboardEvent, current: TabId) {
    const idx = TAB_ORDER.indexOf(current)
    if (e.key === 'ArrowRight') {
      const next = TAB_ORDER[(idx + 1) % TAB_ORDER.length]
      tabRefs[next].current?.focus()
    } else if (e.key === 'ArrowLeft') {
      const prev = TAB_ORDER[(idx - 1 + TAB_ORDER.length) % TAB_ORDER.length]
      tabRefs[prev].current?.focus()
    }
  }

  const hasNeedsReauth = accounts.some(
    (a) => a.status === 'needs-reauth' || a.status === 'needs-relogin',
  )

  function openNewDialog() {
    setAllLimitedReset(undefined)
    setShowNewDialog(true)
  }

  function closeNewDialog() {
    setShowNewDialog(false)
    setBranchError(null)
    newButtonRef.current?.focus()
  }

  function handleCreateSubmit(input: CreateSessionInput) {
    createSession.mutate(input, {
      onSuccess: () => {
        setShowNewDialog(false)
        newButtonRef.current?.focus()
      },
      onError: (err) => {
        if (err.status === 409) {
          const body = err.body as { error?: { code?: string; message?: string }; earliestReset?: string | null }
          if (body?.error?.code === 'branch_switch_failed') {
            setBranchError(body.error.message ?? 'Could not switch branch')
          } else {
            const reset = body.earliestReset ?? null
            setAllLimitedReset(reset)
            setShowNewDialog(false)
          }
        }
      },
    })
  }

  function handleOpen(id: string) {
    if (onOpenSession) onOpenSession(id)
  }

  function handleRename(id: string) {
    const session = sessions.find((s) => s.id === id)
    if (session) setRenameTarget({ id, name: session.name })
  }

  function handleRenameSubmit(id: string, name: string) {
    renameSession.mutate({ id, name }, {
      onSuccess: () => setRenameTarget(null),
    })
  }

  const handleClose = useCallback((id: string) => {
    closeSession.mutate(id)
  }, [closeSession])

  // ── DispatchBar handlers ─────────────────────────────────────────────────────

  function handleNavigate(dir: 'up' | 'down') {
    if (sessions.length === 0) return
    // Moving to a different row clears the pending-delete guard
    setPendingDelete(null)
    if (pendingDeleteTimeoutRef.current) {
      clearTimeout(pendingDeleteTimeoutRef.current)
      pendingDeleteTimeoutRef.current = null
    }
    setHighlightedIndex((i) => {
      if (dir === 'up') return i <= 0 ? 0 : i - 1
      return Math.min(sessions.length - 1, i + 1)
    })
  }

  function handleOpenHighlighted() {
    if (selectedSession) handleOpen(selectedSession.id)
  }

  function handleRowAction(
    action: { type: 'rename' } | { type: 'delete' } | { type: 'jump'; index: number },
  ) {
    if (action.type === 'rename') {
      // Rename clears any pending delete
      setPendingDelete(null)
      if (pendingDeleteTimeoutRef.current) {
        clearTimeout(pendingDeleteTimeoutRef.current)
        pendingDeleteTimeoutRef.current = null
      }
      if (selectedSession) handleRename(selectedSession.id)
    } else if (action.type === 'delete') {
      if (!selectedSession) return
      const id = selectedSession.id
      const now = Date.now()
      if (pendingDelete && pendingDelete.id === id && now - pendingDelete.ts < 2000) {
        // Second Delete on the same session within 2 s → actually close it
        if (pendingDeleteTimeoutRef.current) {
          clearTimeout(pendingDeleteTimeoutRef.current)
          pendingDeleteTimeoutRef.current = null
        }
        setPendingDelete(null)
        handleClose(id)
      } else {
        // First Delete → arm the guard and show the hint
        if (pendingDeleteTimeoutRef.current) clearTimeout(pendingDeleteTimeoutRef.current)
        setPendingDelete({ id, ts: now })
        pendingDeleteTimeoutRef.current = setTimeout(() => {
          setPendingDelete(null)
          pendingDeleteTimeoutRef.current = null
        }, 2000)
      }
    } else if (action.type === 'jump') {
      // Jump clears any pending delete
      setPendingDelete(null)
      if (pendingDeleteTimeoutRef.current) {
        clearTimeout(pendingDeleteTimeoutRef.current)
        pendingDeleteTimeoutRef.current = null
      }
      const s = sessions[action.index]
      if (s) {
        setHighlightedIndex(action.index)
        handleOpen(s.id)
      }
    }
  }

  function clearPendingDelete() {
    setPendingDelete(null)
    if (pendingDeleteTimeoutRef.current) {
      clearTimeout(pendingDeleteTimeoutRef.current)
      pendingDeleteTimeoutRef.current = null
    }
  }

  function handleDispatch({ initialPrompt, repo }: { initialPrompt: string; repo: string | null }) {
    clearPendingDelete()
    const repoInfo = repo ? (reposQuery.data ?? []).find((r) => r.nameWithOwner === repo) : undefined
    createSession.mutate(
      {
        initialPrompt,
        repo: repo ?? undefined,
        branch: repoInfo?.defaultBranch ?? undefined,
      },
      {
        onSuccess: (session) => {
          if (repo) setLastRepo(repo)
          if (onOpenSession) onOpenSession(session.id)
        },
      },
    )
  }

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

  function handleUnarchive(id: string) {
    setUnarchivingId(id)
    unarchiveSession.mutate(id, {
      onSettled: () => setUnarchivingId(null),
    })
  }

  function handlePermanentDelete(session: ArchivedSession) {
    setPermDeleteTarget(session)
  }

  function confirmPermanentDelete() {
    if (!permDeleteTarget) return
    permanentDeleteSession.mutate(permDeleteTarget.id, {
      onSuccess: () => setPermDeleteTarget(null),
      onError: () => setPermDeleteTarget(null),
    })
  }

  function handleReview(id: string) {
    if (onReviewSession) onReviewSession(id)
  }

  const isLoading = accountsLoading || sessionsLoading

  // Sessions that reference the account being deleted
  const deleteTargetSessions = deleteTarget
    ? sessions.filter((s) => s.accountName === deleteTarget.name)
    : []

  return (
    <div className="space-y-4">
      {/* All-limited banner — global, above tabs */}
      {allLimitedReset !== undefined && (
        <AllLimitedBanner
          earliestReset={allLimitedReset}
          onDismiss={() => setAllLimitedReset(undefined)}
        />
      )}

      {/* Tab bar */}
      <div
        role="tablist"
        aria-label="Dashboard sections"
        className="flex gap-1 border-b border-gray-200 dark:border-gray-700"
      >
        {/* Sessions tab */}
        <button
          ref={sessionsTabRef}
          role="tab"
          id="tab-sessions"
          aria-selected={activeTab === 'sessions'}
          aria-controls="tabpanel-sessions"
          tabIndex={activeTab === 'sessions' ? 0 : -1}
          onKeyDown={(e) => handleTabKeyDown(e, 'sessions')}
          onClick={() => setActiveTab('sessions')}
          className={cn(
            'px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-colors',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-offset-2',
            activeTab === 'sessions'
              ? 'border-blue-600 text-blue-600 dark:border-blue-400 dark:text-blue-400'
              : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200',
          )}
        >
          Sessions
        </button>

        {/* Accounts tab */}
        <button
          ref={accountsTabRef}
          role="tab"
          id="tab-accounts"
          aria-selected={activeTab === 'accounts'}
          aria-controls="tabpanel-accounts"
          tabIndex={activeTab === 'accounts' ? 0 : -1}
          onKeyDown={(e) => handleTabKeyDown(e, 'accounts')}
          onClick={() => setActiveTab('accounts')}
          aria-label={hasNeedsReauth ? 'Accounts (needs attention)' : 'Accounts'}
          className={cn(
            'px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-colors flex items-center gap-1.5',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-offset-2',
            activeTab === 'accounts'
              ? 'border-blue-600 text-blue-600 dark:border-blue-400 dark:text-blue-400'
              : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200',
          )}
        >
          Accounts
          {hasNeedsReauth && (
            <span
              aria-hidden="true"
              className="inline-block w-2 h-2 rounded-full bg-amber-500 dark:bg-amber-400"
            />
          )}
        </button>

        {/* Archived tab */}
        <button
          ref={archivedTabRef}
          role="tab"
          id="tab-archived"
          aria-selected={activeTab === 'archived'}
          aria-controls="tabpanel-archived"
          tabIndex={activeTab === 'archived' ? 0 : -1}
          onKeyDown={(e) => handleTabKeyDown(e, 'archived')}
          onClick={() => setActiveTab('archived')}
          aria-label={archivedSessions.length > 0 ? `Archived (${archivedSessions.length})` : 'Archived'}
          className={cn(
            'px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-colors flex items-center gap-1.5',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-offset-2',
            activeTab === 'archived'
              ? 'border-blue-600 text-blue-600 dark:border-blue-400 dark:text-blue-400'
              : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200',
          )}
        >
          Archived
          {archivedSessions.length > 0 && (
            <span className="text-xs tabular-nums bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300 rounded-full px-1.5 py-0.5 leading-none">
              {archivedSessions.length}
            </span>
          )}
        </button>
      </div>

      {/* Sessions panel */}
      {activeTab === 'sessions' && (
        <div
          id="tabpanel-sessions"
          role="tabpanel"
          aria-labelledby="tab-sessions"
          className="space-y-3"
        >
          <div className="flex items-center justify-between">
            <h2 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
              Sessions
            </h2>
            <div className="flex items-center gap-2">
              <Button
                ref={settingsButtonRef}
                size="sm"
                variant="outline"
                className="min-h-[44px]"
                onClick={() => setShowSettings(true)}
                aria-label="Settings"
              >
                <Settings size={14} aria-hidden="true" className="mr-1" />
                Settings
              </Button>
              <Button
                ref={newButtonRef}
                size="sm"
                className="min-h-[44px]"
                onClick={openNewDialog}
                aria-label="New session"
              >
                <Plus size={14} aria-hidden="true" className="mr-1" />
                New
              </Button>
            </div>
          </div>

          <SessionList
            sessions={sessions}
            accounts={accounts}
            isLoading={isLoading}
            selectedId={selectedId}
            onNew={openNewDialog}
            onOpen={handleOpen}
            onRename={handleRename}
            onClose={handleClose}
          />

          <DispatchBar
            repos={reposQuery.data ?? []}
            reposLoading={reposQuery.isLoading}
            reposError={reposQuery.isError}
            onDispatch={handleDispatch}
            onNavigate={handleNavigate}
            onOpenHighlighted={handleOpenHighlighted}
            onRowAction={handleRowAction}
            onShowHelp={() => setShowHelp(true)}
            onEscape={() => { setHighlightedIndex(-1); clearPendingDelete() }}
            confirmDeleteName={
              pendingDelete && selectedSession?.id === pendingDelete.id
                ? selectedSession.name
                : null
            }
            activeDescendantId={selectedId ? `row-${selectedId}` : undefined}
            autoFocusKey={focusDispatchSignal}
            isPending={createSession.isPending}
          />
        </div>
      )}

      {/* Accounts panel */}
      {activeTab === 'accounts' && (
        <div
          id="tabpanel-accounts"
          role="tabpanel"
          aria-labelledby="tab-accounts"
          className="space-y-2"
        >
          <h2 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
            Accounts
          </h2>
          {!accountsLoading && (
            <AccountsStrip
              accounts={accounts}
              onRefresh={handleRefreshAccount}
              refreshingName={refreshingAccount}
              onSaveNickname={handleSaveNickname}
              onDelete={handleDeleteAccount}
            />
          )}
        </div>
      )}

      {/* Archived panel */}
      {activeTab === 'archived' && (
        <div
          id="tabpanel-archived"
          role="tabpanel"
          aria-labelledby="tab-archived"
          className="space-y-3"
        >
          <h2 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
            Archived Sessions
          </h2>
          <ArchivedList
            sessions={archivedSessions}
            onReview={handleReview}
            onUnarchive={handleUnarchive}
            onPermanentDelete={handlePermanentDelete}
            unarchivingId={unarchivingId}
          />
        </div>
      )}

      {/* Dialogs */}
      {showNewDialog && (
        <NewSessionDialog
          accounts={accounts}
          onClose={closeNewDialog}
          onSubmit={handleCreateSubmit}
          isPending={createSession.isPending}
          externalError={branchError}
          defaultPermissionMode={settingsData?.defaultPermissionMode}
        />
      )}

      {renameTarget && (
        <RenameDialog
          sessionId={renameTarget.id}
          currentName={renameTarget.name}
          onClose={() => setRenameTarget(null)}
          onSubmit={handleRenameSubmit}
          isPending={renameSession.isPending}
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

      {permDeleteTarget && (
        <PermanentDeleteDialog
          sessionName={permDeleteTarget.name || permDeleteTarget.id.slice(0, 8)}
          onClose={() => setPermDeleteTarget(null)}
          onConfirm={confirmPermanentDelete}
          isPending={permanentDeleteSession.isPending}
        />
      )}

      {showHelp && (
        <ShortcutsHelp onClose={() => setShowHelp(false)} />
      )}

      {showSettings && (
        <SettingsPanel
          onClose={() => {
            setShowSettings(false)
            settingsButtonRef.current?.focus()
          }}
        />
      )}
    </div>
  )
}
