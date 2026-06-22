/**
 * DesktopShell — wide-screen (lg+) master-detail layout.
 *
 *   ┌───────────────────────────────────────────────┐
 *   │  header                                        │
 *   ├──────────────┬────────────────────────────────┤
 *   │  sidebar      │  main pane                      │
 *   │  tabs + list  │  focused session / accounts /   │
 *   │  + New slot   │  archived / welcome             │
 *   ├──────────────┴────────────────────────────────┤
 *   │  unified bottom prompt (spans full width)      │
 *   └───────────────────────────────────────────────┘
 *
 * Selection is the single source of truth: the "New Session" slot targets
 * creation (bottom prompt = DispatchBar); a real session targets messaging
 * (bottom prompt = Composer). Exactly one session is mounted/attached at a time
 * via SessionRuntimeProvider keyed by the focused id.
 */

import { useState, useRef, useCallback, useEffect } from 'react'
import { Plus, Settings } from 'lucide-react'
import { cn } from '@/lib/utils'
import { SessionList } from './SessionList'
import { DispatchBar } from './DispatchBar'
import { AllLimitedBanner } from './AllLimitedBanner'
import { ShortcutsHelp } from './ShortcutsHelp'
import { SettingsPanel } from './settings/SettingsPanel'
import { AccountsManager } from './AccountsManager'
import { ArchivedManager } from './ArchivedManager'
import { ReviewView } from './ReviewView'
import { RenameDialog, PermanentDeleteDialog } from './Dashboard'
import { SessionRuntimeProvider, useSessionRuntimeContext } from './session/SessionRuntimeProvider'
import { SessionBody } from './session/SessionBody'
import { StatusBar } from './session/StatusBar'
import { Composer } from './session/Composer'
import {
  useAccounts,
  useSessions,
  useArchivedSessions,
  useCreateSession,
  useRenameSession,
  useCloseSession,
  usePermanentDeleteSession,
  useGithubRepos,
} from '@/api/hooks'
import { setLastRepo } from '@/lib/lastRepo'

type TabId = 'sessions' | 'accounts' | 'archived'
type Target = { type: 'new' } | { type: 'session'; id: string; initialPrompt?: string }

// ── Session subtree (rendered only under SessionRuntimeProvider) ──────────────

function MainPaneSession() {
  const rt = useSessionRuntimeContext()
  const toolToggleText =
    rt.toolDetailMode === 'auto' ? 'Collapse tools'
    : rt.toolDetailMode === 'all-closed' ? 'Expand tools'
    : 'Auto-fold'
  const toolToggleAria =
    rt.toolDetailMode === 'auto' ? 'Collapse tool calls'
    : rt.toolDetailMode === 'all-closed' ? 'Expand tool calls'
    : 'Auto-fold tool calls'
  return (
    <div className="flex flex-col h-full min-h-0">
      <div className={cn(
        'flex items-center gap-3 px-3 py-2 flex-shrink-0',
        'bg-gray-100 dark:bg-gray-900 border-b border-gray-200 dark:border-gray-700',
      )}>
        <span className="text-sm font-semibold text-gray-900 dark:text-gray-100 truncate">{rt.title}</span>
        <button
          type="button"
          onClick={rt.toggleToolDetail}
          aria-label={toolToggleAria}
          className={cn(
            'ml-auto flex items-center gap-1.5 px-3 py-2 rounded-md min-h-[44px]',
            'text-sm font-medium text-gray-700 dark:text-gray-200',
            'hover:bg-gray-200 dark:hover:bg-gray-700 active:bg-gray-300 dark:active:bg-gray-600',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
          )}
        >
          {toolToggleText}
        </button>
        <StatusBar status={rt.state.status} connectionState={rt.connectionState} />
      </div>
      <div className="flex-1 min-h-0 flex flex-col overflow-hidden bg-white dark:bg-gray-950">
        <SessionBody rt={rt} />
      </div>
    </div>
  )
}

function SessionComposer() {
  const rt = useSessionRuntimeContext()
  return (
    <Composer
      status={rt.state.status}
      mode={rt.mode}
      onPrompt={rt.onPrompt}
      onInterrupt={rt.onInterrupt}
      onSetMode={rt.onSetMode}
      queued={rt.state.queued}
      onCancelQueued={rt.onCancelQueued}
    />
  )
}

// ── Welcome / empty main-pane state (when New Session is the target) ───────────

function WelcomePane() {
  return (
    <div className="flex flex-col items-center justify-center h-full gap-2 text-center px-6">
      <p className="text-gray-600 dark:text-gray-400 text-sm">
        Select a session from the left, or type below to start a new one.
      </p>
    </div>
  )
}

// ── DesktopShell ──────────────────────────────────────────────────────────────

export function DesktopShell() {
  const { data: accounts = [] } = useAccounts()
  const { data: sessions = [], isLoading: sessionsLoading } = useSessions()
  const { data: archivedSessions = [] } = useArchivedSessions()
  const reposQuery = useGithubRepos()

  const createSession = useCreateSession()
  const renameSession = useRenameSession()
  const closeSession = useCloseSession()
  const permanentDeleteSession = usePermanentDeleteSession()

  const [tab, setTab] = useState<TabId>('sessions')
  const [selection, setSelection] = useState<Target>({ type: 'new' })
  const [reviewId, setReviewId] = useState<string | null>(null)
  const [showSettings, setShowSettings] = useState(false)
  const [showHelp, setShowHelp] = useState(false)
  const [renameTarget, setRenameTarget] = useState<{ id: string; name: string } | null>(null)
  const [sessionDeleteTarget, setSessionDeleteTarget] = useState<{ id: string; name: string } | null>(null)
  const [allLimitedReset, setAllLimitedReset] = useState<string | null | undefined>(undefined)

  // Keyboard highlight (for the new-session DispatchBar nav affordances)
  const [highlightedIndex, setHighlightedIndex] = useState(-1)
  const highlighted = highlightedIndex >= 0 ? sessions[highlightedIndex] : undefined

  // Press-twice delete guard
  const [pendingDelete, setPendingDelete] = useState<{ id: string; ts: number } | null>(null)
  const pendingDeleteTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => {
    if (pendingDeleteTimeoutRef.current) clearTimeout(pendingDeleteTimeoutRef.current)
  }, [])

  // Clamp highlight when the list shrinks.
  const clampedIndex = sessions.length === 0 ? -1 : Math.min(highlightedIndex, sessions.length - 1)
  if (clampedIndex !== highlightedIndex) setHighlightedIndex(clampedIndex)

  const settingsButtonRef = useRef<HTMLButtonElement>(null)

  const selectedSessionId = selection.type === 'session' ? selection.id : null

  function clearPendingDelete() {
    setPendingDelete(null)
    if (pendingDeleteTimeoutRef.current) {
      clearTimeout(pendingDeleteTimeoutRef.current)
      pendingDeleteTimeoutRef.current = null
    }
  }

  function focusSession(id: string, initialPrompt?: string) {
    clearPendingDelete()
    setTab('sessions')
    setSelection({ type: 'session', id, initialPrompt })
  }

  function focusNew() {
    clearPendingDelete()
    setTab('sessions')
    setSelection({ type: 'new' })
  }

  const handleClose = useCallback((id: string) => {
    closeSession.mutate(id)
    setSelection((cur) => (cur.type === 'session' && cur.id === id ? { type: 'new' } : cur))
  }, [closeSession])

  function handleRename(id: string) {
    const session = sessions.find((s) => s.id === id)
    if (session) setRenameTarget({ id, name: session.name })
  }

  function handleRenameSubmit(id: string, name: string) {
    renameSession.mutate({ id, name }, { onSuccess: () => setRenameTarget(null) })
  }

  // Permanent delete is destructive, so it goes through the same confirm dialog
  // as Dashboard: open the dialog here, run the mutation only on confirm.
  function handleDelete(id: string) {
    const session = sessions.find((s) => s.id === id)
    setSessionDeleteTarget({ id, name: session?.name ?? id.slice(0, 8) })
  }

  function confirmSessionDelete() {
    if (!sessionDeleteTarget) return
    const id = sessionDeleteTarget.id
    permanentDeleteSession.mutate(id, {
      onSuccess: () => {
        setSessionDeleteTarget(null)
        // If the deleted session was selected, fall back to the new-session slot.
        setSelection((cur) => (cur.type === 'session' && cur.id === id ? { type: 'new' } : cur))
      },
      onError: () => setSessionDeleteTarget(null),
    })
  }

  // ── DispatchBar handlers (new-session mode) ─────────────────────────────────

  function handleNavigate(dir: 'up' | 'down') {
    if (sessions.length === 0) return
    clearPendingDelete()
    setHighlightedIndex((i) => {
      if (dir === 'up') return i <= 0 ? 0 : i - 1
      return Math.min(sessions.length - 1, i + 1)
    })
  }

  function handleOpenHighlighted() {
    if (highlighted) focusSession(highlighted.id)
  }

  function handleRowAction(
    action: { type: 'rename' } | { type: 'delete' } | { type: 'jump'; index: number },
  ) {
    if (action.type === 'rename') {
      clearPendingDelete()
      if (highlighted) handleRename(highlighted.id)
    } else if (action.type === 'delete') {
      if (!highlighted) return
      const id = highlighted.id
      const now = Date.now()
      if (pendingDelete && pendingDelete.id === id && now - pendingDelete.ts < 2000) {
        clearPendingDelete()
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
      clearPendingDelete()
      const s = sessions[action.index]
      if (s) {
        setHighlightedIndex(action.index)
        focusSession(s.id)
      }
    }
  }

  function handleDispatch({ initialPrompt, repo, images }: { initialPrompt: string; repo: string | null; images: { sha: string; mime: string; name: string }[] }) {
    clearPendingDelete()
    const repoInfo = repo ? (reposQuery.data ?? []).find((r) => r.nameWithOwner === repo) : undefined
    createSession.mutate(
      {
        initialPrompt,
        repo: repo ?? undefined,
        branch: repoInfo?.defaultBranch ?? undefined,
        images: images.length ? images : undefined,
      },
      {
        onSuccess: (session) => {
          if (repo) setLastRepo(repo)
          focusSession(session.id, initialPrompt)
        },
        onError: (err) => {
          if (err.status === 409) {
            const body = err.body as { error?: { code?: string }; earliestReset?: string | null }
            if (body?.error?.code !== 'branch_switch_failed') {
              setAllLimitedReset(body.earliestReset ?? null)
            }
          }
        },
      },
    )
  }

  const hasNeedsReauth = accounts.some(
    (a) => a.status === 'needs-reauth' || a.status === 'needs-relogin',
  )

  // ── Pieces shared between the provider and no-provider branches ──────────────

  const sidebar = (
    <aside className="w-80 shrink-0 border-r border-gray-200 dark:border-gray-700 flex flex-col min-h-0">
      {/* Tab bar */}
      <div role="tablist" aria-label="Dashboard sections" className="flex gap-1 border-b border-gray-200 dark:border-gray-700 px-2">
        {(['sessions', 'accounts', 'archived'] as TabId[]).map((t) => (
          <button
            key={t}
            role="tab"
            aria-selected={tab === t}
            aria-label={
              t === 'accounts' && hasNeedsReauth ? 'Accounts (needs attention)'
              : t === 'archived' && archivedSessions.length > 0 ? `Archived (${archivedSessions.length})`
              : t.charAt(0).toUpperCase() + t.slice(1)
            }
            onClick={() => { setTab(t); if (t !== 'archived') setReviewId(null) }}
            className={cn(
              'px-3 py-2 text-sm font-medium border-b-2 -mb-px transition-colors flex items-center gap-1.5',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
              tab === t
                ? 'border-blue-600 text-blue-600 dark:border-blue-400 dark:text-blue-400'
                : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200',
            )}
          >
            {t.charAt(0).toUpperCase() + t.slice(1)}
            {t === 'accounts' && hasNeedsReauth && (
              <span aria-hidden="true" className="inline-block w-2 h-2 rounded-full bg-amber-500 dark:bg-amber-400" />
            )}
            {t === 'archived' && archivedSessions.length > 0 && (
              <span className="text-xs tabular-nums bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300 rounded-full px-1.5 py-0.5 leading-none">
                {archivedSessions.length}
              </span>
            )}
          </button>
        ))}
      </div>

      {/* Session list with permanent New Session slot */}
      <div className="flex-1 min-h-0 overflow-y-auto p-2 space-y-2">
        <button
          type="button"
          aria-label="New session"
          data-selected={selection.type === 'new'}
          onClick={focusNew}
          className={cn(
            'w-full flex items-center gap-2 rounded-lg border px-3 py-2 text-sm font-medium min-h-[44px]',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
            selection.type === 'new'
              ? 'border-blue-500 bg-blue-50 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300'
              : 'border-gray-200 dark:border-gray-700 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800',
          )}
        >
          <Plus size={16} aria-hidden="true" />
          New Session
        </button>

        <SessionList
          sessions={sessions}
          accounts={accounts}
          isLoading={sessionsLoading}
          selectedId={selectedSessionId}
          onNew={focusNew}
          onOpen={focusSession}
          onRename={handleRename}
          onClose={handleClose}
          onDelete={handleDelete}
        />
      </div>
    </aside>
  )

  let mainPane: React.ReactNode
  if (tab === 'accounts') {
    mainPane = <div className="p-4 overflow-y-auto h-full"><AccountsManager /></div>
  } else if (tab === 'archived') {
    mainPane = reviewId
      ? <ReviewView sessionId={reviewId} onBack={() => setReviewId(null)} />
      : <div className="p-4 overflow-y-auto h-full"><ArchivedManager onReviewSession={setReviewId} /></div>
  } else {
    mainPane = selection.type === 'session' ? <MainPaneSession /> : <WelcomePane />
  }

  const sessionActive = tab === 'sessions' && selection.type === 'session'

  const bottomPrompt = sessionActive ? (
    <SessionComposer />
  ) : (
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
        pendingDelete && highlighted?.id === pendingDelete.id ? highlighted.name : null
      }
      activeDescendantId={highlighted ? `row-${highlighted.id}` : undefined}
      isPending={createSession.isPending}
    />
  )

  const shell = (
    <div className="flex flex-col h-screen bg-background text-foreground">
      <header className="flex items-center justify-between px-4 py-2 border-b border-border flex-shrink-0">
        <h1 className="text-lg font-bold tracking-tight">ccswitch</h1>
        <button
          ref={settingsButtonRef}
          type="button"
          onClick={() => setShowSettings(true)}
          aria-label="Settings"
          className={cn(
            'flex items-center justify-center h-9 w-9 rounded-md',
            'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200',
            'hover:bg-gray-100 dark:hover:bg-gray-800',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
          )}
        >
          <Settings size={18} aria-hidden="true" />
        </button>
      </header>

      {allLimitedReset !== undefined && (
        <div className="px-4 pt-2 flex-shrink-0">
          <AllLimitedBanner earliestReset={allLimitedReset} onDismiss={() => setAllLimitedReset(undefined)} />
        </div>
      )}

      <div className="flex-1 flex min-h-0">
        {sidebar}
        <main className="flex-1 min-h-0 overflow-hidden flex flex-col">{mainPane}</main>
      </div>

      <div className="flex-shrink-0">{bottomPrompt}</div>

      {/* Dialogs */}
      {renameTarget && (
        <RenameDialog
          sessionId={renameTarget.id}
          currentName={renameTarget.name}
          onClose={() => setRenameTarget(null)}
          onSubmit={handleRenameSubmit}
          isPending={renameSession.isPending}
        />
      )}
      {sessionDeleteTarget && (
        <PermanentDeleteDialog
          sessionName={sessionDeleteTarget.name}
          onClose={() => setSessionDeleteTarget(null)}
          onConfirm={confirmSessionDelete}
          isPending={permanentDeleteSession.isPending}
        />
      )}
      {showHelp && <ShortcutsHelp onClose={() => setShowHelp(false)} />}
      {showSettings && (
        <SettingsPanel onClose={() => { setShowSettings(false); settingsButtonRef.current?.focus() }} />
      )}
    </div>
  )

  // One SessionRuntimeProvider mounted at a time, keyed by the focused id so each
  // session gets a fresh reducer + clean attach/detach.
  return sessionActive
    ? <SessionRuntimeProvider key={selection.id} sessionId={selection.id} initialPrompt={selection.initialPrompt}>{shell}</SessionRuntimeProvider>
    : shell
}
