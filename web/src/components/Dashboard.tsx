import * as React from 'react'
import { useState, useRef, useCallback, useEffect } from 'react'
import { Plus, Settings } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import { SessionList } from './SessionList'
import { AllLimitedBanner } from './AllLimitedBanner'
import { SettingsPanel } from './settings/SettingsPanel'
import { DispatchBar } from './DispatchBar'
import { ShortcutsHelp } from './ShortcutsHelp'
import { AccountsManager } from './AccountsManager'
import { ArchivedManager } from './ArchivedManager'
import {
  useAccounts,
  useSessions,
  useCreateSession,
  useRenameSession,
  useCloseSession,
  useGithubRepos,
  useGithubBranches,
  useSettings,
  useArchivedSessions,
  type CreateSessionInput,
} from '@/api/hooks'
import type { Account } from '@/api/types'
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

export function NewSessionDialog({ accounts, onClose, onSubmit, isPending, externalError, defaultPermissionMode }: NewSessionDialogProps) {
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

export function RenameDialog({ sessionId, currentName, onClose, onSubmit, isPending }: RenameDialogProps) {
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

  const [activeTab, setActiveTab] = useState<TabId>(initialTab ?? 'sessions')
  const [showNewDialog, setShowNewDialog] = useState(false)
  const [showSettings, setShowSettings] = useState(false)
  const [showHelp, setShowHelp] = useState(false)
  const [renameTarget, setRenameTarget] = useState<{ id: string; name: string } | null>(null)
  const [allLimitedReset, setAllLimitedReset] = useState<string | null | undefined>(undefined)
  const [branchError, setBranchError] = useState<string | null>(null)

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
        if (pendingDeleteTimeoutRef.current) {
          clearTimeout(pendingDeleteTimeoutRef.current)
          pendingDeleteTimeoutRef.current = null
        }
        setPendingDelete(null)
        handleClose(id)
      } else {
        if (pendingDeleteTimeoutRef.current) clearTimeout(pendingDeleteTimeoutRef.current)
        setPendingDelete({ id, ts: now })
        pendingDeleteTimeoutRef.current = setTimeout(() => {
          setPendingDelete(null)
          pendingDeleteTimeoutRef.current = null
        }, 2000)
      }
    } else if (action.type === 'jump') {
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

  function handleReview(id: string) {
    if (onReviewSession) onReviewSession(id)
  }

  const isLoading = accountsLoading || sessionsLoading

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
        >
          <AccountsManager />
        </div>
      )}

      {/* Archived panel */}
      {activeTab === 'archived' && (
        <div
          id="tabpanel-archived"
          role="tabpanel"
          aria-labelledby="tab-archived"
        >
          <ArchivedManager onReviewSession={handleReview} />
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
