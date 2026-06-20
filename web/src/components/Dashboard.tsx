import * as React from 'react'
import { useState, useRef, useCallback } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Plus, RefreshCw, Settings, Pencil, Check, X } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import { UsageBadge, StatusPill } from './UsageBadge'
import { SessionList } from './SessionList'
import { AllLimitedBanner } from './AllLimitedBanner'
import { SettingsPanel } from './settings/SettingsPanel'
import {
  useAccounts,
  useSessions,
  useCreateSession,
  useRenameSession,
  useCloseSession,
  useRefreshAccount,
  useUpdateAccountNickname,
  useReauthAccount,
  useReauthStatus,
  useCancelReauth,
  useGithubRepos,
  useGithubBranches,
  useSettings,
  QK,
  type CreateSessionInput,
} from '@/api/hooks'
import type { Account } from '@/api/types'
import { PERMISSION_MODES } from '@/session/permissionModes'

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

// ── Accounts Strip ───────────────────────────────────────────────────────────

interface AccountRowProps {
  account: Account
  onRefresh: (name: string) => void
  refreshing: boolean
  onSaveNickname: (name: string, nickname: string | null) => void
}

function AccountRow({ account, onRefresh, refreshing, onSaveNickname }: AccountRowProps) {
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
}

function AccountsStrip({ accounts, onRefresh, refreshingName, onSaveNickname }: AccountsStripProps) {
  if (accounts.length === 0) return null

  return (
    <section aria-label="Account status" className="space-y-2">
      <h2 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
        Accounts
      </h2>
      <div className="flex flex-col gap-2">
        {accounts.map((account) => (
          <AccountRow
            key={account.name}
            account={account}
            onRefresh={onRefresh}
            refreshing={refreshingName === account.name}
            onSaveNickname={onSaveNickname}
          />
        ))}
      </div>
    </section>
  )
}

// ── Dashboard ────────────────────────────────────────────────────────────────

interface DashboardProps {
  /** Called when the user opens a session (navigates to Terminal view). */
  onOpenSession?: (sessionId: string) => void
}

export function Dashboard({ onOpenSession }: DashboardProps) {
  const { data: accounts = [], isLoading: accountsLoading } = useAccounts()
  const { data: sessions = [], isLoading: sessionsLoading } = useSessions()
  const { data: settingsData } = useSettings()

  const createSession = useCreateSession()
  const renameSession = useRenameSession()
  const closeSession = useCloseSession()
  const refreshAccount = useRefreshAccount()
  const updateNickname = useUpdateAccountNickname()

  const [showNewDialog, setShowNewDialog] = useState(false)
  const [showSettings, setShowSettings] = useState(false)
  const [renameTarget, setRenameTarget] = useState<{ id: string; name: string } | null>(null)
  const [allLimitedReset, setAllLimitedReset] = useState<string | null | undefined>(undefined)
  const [refreshingAccount, setRefreshingAccount] = useState<string | null>(null)
  const [branchError, setBranchError] = useState<string | null>(null)

  const newButtonRef = useRef<HTMLButtonElement>(null)
  const settingsButtonRef = useRef<HTMLButtonElement>(null)

  function openNewDialog() {
    setAllLimitedReset(undefined)
    setShowNewDialog(true)
  }

  function closeNewDialog() {
    setShowNewDialog(false)
    setBranchError(null)
    // Return focus to the button that opened the dialog
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
    if (onOpenSession) {
      onOpenSession(id)
    }
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

  function handleRefreshAccount(name: string) {
    setRefreshingAccount(name)
    refreshAccount.mutate(name, {
      onSettled: () => setRefreshingAccount(null),
    })
  }

  function handleSaveNickname(name: string, nickname: string | null) {
    updateNickname.mutate({ name, nickname })
  }

  const isLoading = accountsLoading || sessionsLoading

  return (
    <div className="space-y-6">
      {/* All-limited banner */}
      {allLimitedReset !== undefined && (
        <AllLimitedBanner
          earliestReset={allLimitedReset}
          onDismiss={() => setAllLimitedReset(undefined)}
        />
      )}

      {/* Accounts strip */}
      {!accountsLoading && (
        <AccountsStrip
          accounts={accounts}
          onRefresh={handleRefreshAccount}
          refreshingName={refreshingAccount}
          onSaveNickname={handleSaveNickname}
        />
      )}

      {/* Sessions section */}
      <section aria-label="Sessions">
        <div className="flex items-center justify-between mb-3">
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
          onNew={openNewDialog}
          onOpen={handleOpen}
          onRename={handleRename}
          onClose={handleClose}
        />
      </section>

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
