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
import { SessionListControls } from './SessionListControls'
import { DispatchBar } from './DispatchBar'
import { AllLimitedBanner } from './AllLimitedBanner'
import { ShortcutsHelp } from './ShortcutsHelp'
import { SettingsPanel } from './settings/SettingsPanel'
import { AccountsManager } from './AccountsManager'
import { ArchivedManager } from './ArchivedManager'
import { ReviewView } from './ReviewView'
import { SidebarResizer } from './SidebarResizer'
import { useSidebarWidth } from '@/lib/useSidebarWidth'
import { RenameDialog, PermanentDeleteDialog } from './Dashboard'
import { SessionRuntimeProvider, useSessionRuntimeContext } from './session/SessionRuntimeProvider'
import { SessionBody } from './session/SessionBody'
import { SessionComposer } from './session/SessionComposer'
import { CommandResultOverlay } from './session/CommandResultOverlay'
import {
  useAccounts,
  useSessions,
  useArchivedSessions,
  useCreateSession,
  useRenameSession,
  useCloseSession,
  usePermanentDeleteSession,
  useGithubRepos,
  useSendPrompt,
} from '@/api/hooks'
import { setLastRepo } from '@/lib/lastRepo'
import { useSessionOrdering } from '@/session/useSessionOrdering'

type TabId = 'sessions' | 'accounts' | 'archived'
type Target = { type: 'new' } | { type: 'session'; id: string; initialPrompt?: string }

// ── Session subtree (rendered only under SessionRuntimeProvider) ──────────────

function MainPaneSession() {
  const rt = useSessionRuntimeContext()
  return (
    <div className="flex flex-col h-full min-h-0">
      <div className={cn(
        'flex items-center gap-3 px-3 py-2 flex-shrink-0',
        'bg-gray-100 dark:bg-gray-900 border-b border-gray-200 dark:border-gray-700',
      )}>
        <span className="text-sm font-semibold text-gray-900 dark:text-gray-100 truncate">{rt.title}</span>
      </div>
      <div className="flex-1 min-h-0 flex flex-col overflow-hidden bg-white dark:bg-gray-950">
        <SessionBody rt={rt} />
      </div>
      <CommandResultOverlay
        result={rt.commandResult}
        commands={rt.commands}
        onClose={rt.dismissCommandResult}
      />
    </div>
  )
}

// Context bridge: the bottom prompt is rendered inside the SessionRuntimeProvider,
// so it can read the runtime from context and hand it to the shared SessionComposer.
function BottomComposer() {
  const rt = useSessionRuntimeContext()
  return <SessionComposer rt={rt} />
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
  const { groupBy, sortBy, setGroupBy, setSortBy, groups, orderedSessions } = useSessionOrdering(sessions)
  const { data: archivedSessions = [] } = useArchivedSessions()
  const reposQuery = useGithubRepos()

  const createSession = useCreateSession()
  const renameSession = useRenameSession()
  const closeSession = useCloseSession()
  const permanentDeleteSession = usePermanentDeleteSession()

  const [tab, setTab] = useState<TabId>('sessions')
  const [sidebarWidth, setSidebarWidth] = useSidebarWidth()
  const [selection, setSelection] = useState<Target>({ type: 'new' })
  const [reviewId, setReviewId] = useState<string | null>(null)
  const [showSettings, setShowSettings] = useState(false)
  const [showHelp, setShowHelp] = useState(false)
  const [renameTarget, setRenameTarget] = useState<{ id: string; name: string } | null>(null)
  const [sessionDeleteTarget, setSessionDeleteTarget] = useState<{ id: string; name: string } | null>(null)
  const [allLimitedReset, setAllLimitedReset] = useState<string | null | undefined>(undefined)

  // Keyboard highlight (for the new-session DispatchBar nav affordances)
  const [highlightedIndex, setHighlightedIndex] = useState(-1)
  const highlighted = highlightedIndex >= 0 ? orderedSessions[highlightedIndex] : undefined

  // Bumped to return focus to the DispatchBar after detaching from a session.
  const [dispatchFocusNonce, setDispatchFocusNonce] = useState(0)

  // Press-twice delete guard
  const [pendingDelete, setPendingDelete] = useState<{ id: string; ts: number } | null>(null)
  const pendingDeleteTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => {
    if (pendingDeleteTimeoutRef.current) clearTimeout(pendingDeleteTimeoutRef.current)
  }, [])

  // Clamp highlight when the list shrinks.
  const clampedIndex = orderedSessions.length === 0 ? -1 : Math.min(highlightedIndex, orderedSessions.length - 1)
  if (clampedIndex !== highlightedIndex) setHighlightedIndex(clampedIndex)

  const settingsButtonRef = useRef<HTMLButtonElement>(null)

  const selectedSessionId = selection.type === 'session' ? selection.id : null

  const clearPendingDelete = useCallback(() => {
    setPendingDelete(null)
    if (pendingDeleteTimeoutRef.current) {
      clearTimeout(pendingDeleteTimeoutRef.current)
      pendingDeleteTimeoutRef.current = null
    }
  }, [])

  const focusSession = useCallback((id: string, initialPrompt?: string) => {
    clearPendingDelete()
    setTab('sessions')
    setSelection({ type: 'session', id, initialPrompt })
  }, [clearPendingDelete])

  function focusNew() {
    clearPendingDelete()
    setTab('sessions')
    setSelection({ type: 'new' })
  }

  const detachToList = useCallback(() => {
    const id = selection.type === 'session' ? selection.id : null
    setSelection({ type: 'new' })
    if (id) {
      const idx = orderedSessions.findIndex((s) => s.id === id)
      if (idx >= 0) setHighlightedIndex(idx)
    }
    setDispatchFocusNonce((n) => n + 1) // return focus to DispatchBar for ↑/↓ nav
  }, [selection, orderedSessions])

  // In-session detach gestures (desktop): ← / Esc / Ctrl+Z return to the list.
  // Active only while a session is focused. Guards: yield to the composer while it
  // holds a draft (cursor move / undo). Faithful detach → main pane shows WelcomePane.
  useEffect(() => {
    const active = tab === 'sessions' && selection.type === 'session'
    if (!active) return
    function onKey(e: KeyboardEvent) {
      const ae = document.activeElement as HTMLTextAreaElement | null
      const inComposerWithText =
        ae?.getAttribute('aria-label') === 'Message' && (ae?.value ?? '') !== ''
      if (e.ctrlKey && (e.key === 'z' || e.key === 'Z')) {
        if (inComposerWithText) return
        e.preventDefault()
        detachToList()
        return
      }
      if (e.key === 'ArrowLeft' || e.key === 'Escape') {
        if (inComposerWithText) return
        e.preventDefault()
        detachToList()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [tab, selection, detachToList])

  const handleClose = useCallback((id: string) => {
    closeSession.mutate(id)
    setSelection((cur) => (cur.type === 'session' && cur.id === id ? { type: 'new' } : cur))
  }, [closeSession])

  const sendPrompt = useSendPrompt()
  const handleQuickResume = useCallback((id: string, text?: string) => {
    if (text) {
      sendPrompt.mutate({ id, text })
    } else {
      focusSession(id)
    }
  }, [sendPrompt, focusSession])

  function handleRename(id: string) {
    const session = orderedSessions.find((s) => s.id === id)
    if (session) setRenameTarget({ id, name: session.name })
  }

  function handleRenameSubmit(id: string, name: string) {
    renameSession.mutate({ id, name }, { onSuccess: () => setRenameTarget(null) })
  }

  // Permanent delete is destructive, so it goes through the same confirm dialog
  // as Dashboard: open the dialog here, run the mutation only on confirm.
  function handleDelete(id: string) {
    const session = orderedSessions.find((s) => s.id === id)
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
    if (orderedSessions.length === 0) return
    clearPendingDelete()
    setHighlightedIndex((i) => {
      if (dir === 'up') return i <= 0 ? 0 : i - 1
      return Math.min(orderedSessions.length - 1, i + 1)
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
      const s = orderedSessions[action.index]
      if (s) {
        setHighlightedIndex(action.index)
        focusSession(s.id)
      }
    }
  }

  function handleDispatch({ initialPrompt, repo, attachmentIds }: { initialPrompt: string; repo: string | null; attachmentIds: string[] }) {
    clearPendingDelete()
    const repoInfo = repo ? (reposQuery.data ?? []).find((r) => r.nameWithOwner === repo) : undefined
    createSession.mutate(
      {
        initialPrompt,
        repo: repo ?? undefined,
        branch: repoInfo?.defaultBranch ?? undefined,
        attachmentIds: attachmentIds.length ? attachmentIds : undefined,
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
    <aside
      style={{ width: sidebarWidth }}
      className="shrink-0 flex flex-col min-h-0"
    >
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

        {sessions.length > 0 && (
          <SessionListControls
            groupBy={groupBy}
            sortBy={sortBy}
            onGroupByChange={setGroupBy}
            onSortByChange={setSortBy}
          />
        )}
        <SessionList
          sessions={orderedSessions}
          groups={groups}
          accounts={accounts}
          isLoading={sessionsLoading}
          // Visible cursor: mark the open session, else the keyboard-highlighted
          // row. The two are mutually exclusive in practice — the list-nav
          // DispatchBar only renders when no session is open — so this also makes
          // ↑/↓ visibly move the highlight on the new-session view.
          selectedId={selectedSessionId ?? highlighted?.id}
          onNew={focusNew}
          onOpen={focusSession}
          onRename={handleRename}
          onClose={handleClose}
          onDelete={handleDelete}
          onQuickResume={handleQuickResume}
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
    <BottomComposer />
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
      autoFocusKey={dispatchFocusNonce}
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
        <SidebarResizer width={sidebarWidth} onResize={setSidebarWidth} />
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
