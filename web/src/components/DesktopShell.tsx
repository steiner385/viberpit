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
import { Plus, Settings, HelpCircle } from 'lucide-react'
import { cn } from '@/lib/utils'
import { SessionGoalText } from './session/SessionGoalText'
import { SessionList } from './SessionList'
import { SessionListControls } from './SessionListControls'
import { DispatchBar } from './DispatchBar'
import { AllLimitedBanner } from './AllLimitedBanner'
import { DeployPanel } from './DeployPanel'
import { HelpDrawer } from './HelpDrawer'
import { useGlobalHelpKey } from './useGlobalHelpKey'
import { SettingsPanel } from './settings/SettingsPanel'
import { AccountsManager } from './AccountsManager'
import { ArchivedManager } from './ArchivedManager'
import { PipelineView } from './PipelineView'
import { AdoptPanel } from './AdoptPanel'
import { AutomationView } from './AutomationView'
import { InsightsView } from './InsightsView'
import { ReviewView } from './ReviewView'
import { SidebarResizer } from './SidebarResizer'
import { useSidebarWidth } from '@/lib/useSidebarWidth'
import { RenameDialog } from './Dashboard'
import { PermanentDeleteDialog } from '@/components/PermanentDeleteDialog'
import { SessionRuntimeProvider, useSessionRuntimeContext } from './session/SessionRuntimeProvider'
import { SessionBody } from './session/SessionBody'
import { SessionComposer } from './session/SessionComposer'
import { CommandResultOverlay } from './session/CommandResultOverlay'
import { SessionActions } from '@/components/session/SessionActions'
import {
  useAccounts,
  useSessions,
  useArchivedSessions,
  useCreateSession,
  useRenameSession,
  useCloseSession,
  usePermanentDeleteSession,
  useGithubRepos,
  useModels,
  useSendPrompt,
  useConfig,
  useAllPrs,
} from '@/api/hooks'
import { TabBar } from './TabBar'
import { buildTabs } from './tabDefs'
import { inFlightPrCount } from '@/session/prStatus'
import { setLastRepo } from '@/lib/lastRepo'
import { toast } from 'sonner'
import { useSessionOrdering } from '@/session/useSessionOrdering'
import { useSessionSearch } from '@/session/useSessionSearch'
import { useListNavigation } from '@/session/useListNavigation'
import { nextVisibleSessionId } from '@/session/nextAfterArchive'

type TabId = 'sessions' | 'accounts' | 'archived' | 'pipeline' | 'automation' | 'insights'
type Target = { type: 'new' } | { type: 'session'; id: string; initialPrompt?: string }

// ── Session subtree (rendered only under SessionRuntimeProvider) ──────────────

export function MainPaneSession({ sessionId, onClosed }: { sessionId: string; onClosed: () => void }) {
  const rt = useSessionRuntimeContext()
  return (
    <div className="flex flex-col h-full min-h-0">
      <div className={cn(
        'flex items-center gap-3 px-3 py-2 flex-shrink-0',
        'bg-gray-100 dark:bg-gray-900 border-b border-gray-200 dark:border-gray-700',
      )}>
        <span className="flex-1 min-w-0 flex items-baseline gap-2">
          <span className="min-w-0 text-sm font-semibold text-gray-900 dark:text-gray-100 truncate">{rt.title}</span>
          <SessionGoalText goal={rt.goal} source={rt.goalSource} title={rt.title} />
        </span>
        <SessionActions
          sessionId={sessionId}
          sessionName={rt.title}
          onDone={onClosed}
          variant="light"
        />
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
  const sessionSearch = useSessionSearch()
  const { groupBy, sortBy, setGroupBy, setSortBy, groups, orderedSessions, navItems, collapsedKeys, toggleCollapse, expandGroup, collapseGroup } =
    useSessionOrdering(sessions, sessionSearch.matchedIds)
  const { data: archivedSessions = [] } = useArchivedSessions()
  const reposQuery = useGithubRepos()
  const { data: models = [] } = useModels()
  const { data: config } = useConfig()

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
  const {
    highlightedIndex, setHighlightedIndex,
    highlightedSession, highlightedGroupKey, navigate, highlightSession,
  } = useListNavigation(navItems)

  // Bumped to return focus to the DispatchBar after detaching from a session.
  const [dispatchFocusNonce, setDispatchFocusNonce] = useState(0)

  // List-focus mode
  const [listMode, setListMode] = useState(false)

  // `?` opens the help slide-out from anywhere on the sessions tab.
  useGlobalHelpKey({ enabled: tab === 'sessions', onShowHelp: () => setShowHelp(true) })

  const settingsButtonRef = useRef<HTMLButtonElement>(null)

  const selectedSessionId = selection.type === 'session' ? selection.id : null

  const focusSession = useCallback((id: string, initialPrompt?: string) => {
    if (id.startsWith('pending-')) return
    setTab('sessions')
    setSelection({ type: 'session', id, initialPrompt })
  }, [])

  // Map a session that just left the live list to the next detail target: the
  // neighbor that takes its place, else the New Session slot. orderedSessions is
  // read at call time (pre-archive, since callers run synchronously on click).
  const advanceFrom = useCallback((removedId: string): Target => {
    const next = nextVisibleSessionId(orderedSessions.map((s) => s.id), removedId)
    return next ? { type: 'session', id: next } : { type: 'new' }
  }, [orderedSessions])

  function focusNew() {
    setTab('sessions')
    setSelection({ type: 'new' })
  }

  const detachToList = useCallback(() => {
    const id = selection.type === 'session' ? selection.id : null
    setSelection({ type: 'new' })
    if (id) highlightSession(id)
    setDispatchFocusNonce((n) => n + 1) // return focus to DispatchBar for ↑/↓ nav
  }, [selection, highlightSession])

  // In-session detach gestures (desktop): ← / Esc / Ctrl+Z return to the list.
  // Active only while a session is focused. Guards: Esc/Ctrl+Z yield to the composer
  // while it holds a draft; ← only detaches from the prompt's left edge (empty, or
  // caret at the very start with no selection) so it otherwise moves the caret.
  // Faithful detach → main pane shows WelcomePane.
  useEffect(() => {
    const active = tab === 'sessions' && selection.type === 'session'
    if (!active) return
    function onKey(e: KeyboardEvent) {
      const ae = document.activeElement as HTMLTextAreaElement | null
      const inComposer = ae?.getAttribute('aria-label') === 'Message'
      const inComposerWithText = inComposer && (ae?.value ?? '') !== ''
      const caretPastComposerStart =
        inComposer && !(ae!.selectionStart === 0 && ae!.selectionEnd === 0)
      if (e.ctrlKey && (e.key === 'z' || e.key === 'Z')) {
        if (inComposerWithText) return
        e.preventDefault()
        detachToList()
        return
      }
      if (e.key === 'Escape') {
        if (inComposerWithText) return
        e.preventDefault()
        detachToList()
        return
      }
      if (e.key === 'ArrowLeft') {
        if (caretPastComposerStart) return
        e.preventDefault()
        detachToList()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [tab, selection, detachToList])

  const handleClose = useCallback((id: string) => {
    closeSession.mutate(id)
    setSelection((cur) => (cur.type === 'session' && cur.id === id ? advanceFrom(id) : cur))
  }, [closeSession, advanceFrom])

  const sendPrompt = useSendPrompt()
  const handleQuickResume = useCallback((id: string, text?: string) => {
    // Always fire in place — the backend lazily resumes a detached session first.
    sendPrompt.mutate({ id, text: text || 'continue' })
  }, [sendPrompt])

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
        // If the deleted session was open, advance to its neighbor.
        setSelection((cur) => (cur.type === 'session' && cur.id === id ? advanceFrom(id) : cur))
      },
      onError: () => setSessionDeleteTarget(null),
    })
  }

  // ── DispatchBar handlers (new-session mode) ─────────────────────────────────

  function handleOpenHighlighted() {
    if (highlightedSession) focusSession(highlightedSession.id)
  }

  function handleEnterList() {
    setListMode(true)
    if (highlightedIndex < 0) setHighlightedIndex(0)
  }
  function handleExitList() {
    setListMode(false)
    setDispatchFocusNonce((n) => n + 1)
  }
  function handleJump(index: number) {
    const s = orderedSessions[index]
    if (s) { highlightSession(s.id); focusSession(s.id) }
  }

  function handleExpandHighlighted() { if (highlightedGroupKey) expandGroup(highlightedGroupKey) }
  function handleCollapseHighlighted() { if (highlightedGroupKey) collapseGroup(highlightedGroupKey) }
  function handleToggleHighlighted() { if (highlightedGroupKey) toggleCollapse(highlightedGroupKey) }

  function handleDispatch({ initialPrompt, repo, account, model, attachmentIds }: { initialPrompt: string; repo: string | null; account: string | null; model: string | null; attachmentIds: string[] }) {
    const repoInfo = repo ? (reposQuery.data ?? []).find((r) => r.nameWithOwner === repo) : undefined
    createSession.mutate(
      {
        initialPrompt,
        repo: repo ?? undefined,
        branch: repoInfo?.defaultBranch ?? undefined,
        account: account ?? undefined,
        model: model ?? undefined,
        attachmentIds: attachmentIds.length ? attachmentIds : undefined,
      },
      {
        onSuccess: (session) => {
          if (repo) setLastRepo(repo)
          if (session.usageUnverified) {
            toast("Couldn't verify account usage — started anyway.")
          }
          focusSession(session.id, initialPrompt)
        },
        onError: (err) => {
          if (err.status === 409) {
            const body = err.body as { error?: { code?: string }; earliestReset?: string | null }
            if (body?.error?.code === 'branch_switch_failed') {
              // branch errors are handled elsewhere; ignore here
            } else if (body?.error?.code === 'accounts_need_reauth') {
              toast.error('No usable accounts — check the Accounts tab to re-authenticate.')
            } else {
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

  // Live count badges for the header tabs (shared cache — the Pipeline tab reads
  // the same useAllPrs query, so this adds no extra fetch).
  const { data: prs = [] } = useAllPrs()
  const tabs = buildTabs({
    sessions: orderedSessions.length,
    accounts: accounts.length,
    pipeline: inFlightPrCount(prs),
    archived: archivedSessions.length,
    accountsAttention: hasNeedsReauth,
  })

  // Auto-clear the all-limited banner once any account becomes selectable.
  // Done at render time (not in an effect) — React 18 batches synchronous setState
  // calls during render and re-renders synchronously when state changes.
  if (allLimitedReset !== undefined && accounts.some((a) => a.rank !== null)) {
    setAllLimitedReset(undefined)
  }

  // Auto-clear when the reset time passes (uses setTimeout to defer the setState
  // call so that Date.now() is not called during render per react-hooks/purity,
  // and setState is called from the timeout callback per react-hooks/set-state-in-effect).
  useEffect(() => {
    if (allLimitedReset === undefined || allLimitedReset === null) return
    const id = setTimeout(() => setAllLimitedReset(undefined), Math.max(0, new Date(allLimitedReset).getTime() - Date.now()))
    return () => clearTimeout(id)
  }, [allLimitedReset])

  // ── Pieces shared between the provider and no-provider branches ──────────────

  const sidebar = (
    <aside
      style={{ width: sidebarWidth }}
      className="shrink-0 flex flex-col min-h-0 border-t border-gray-200 dark:border-gray-700"
    >
      {/* Tabs now live in the app header (see shell below); the sidebar is a pure
          session-list column. */}

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
            searchQuery={sessionSearch.query}
            onSearchChange={sessionSearch.setQuery}
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
          selectedId={selectedSessionId ?? highlightedSession?.id}
          selectedGroupKey={highlightedGroupKey ?? null}
          onNew={focusNew}
          onOpen={focusSession}
          onRename={handleRename}
          onClose={handleClose}
          onDelete={handleDelete}
          isFiltered={sessionSearch.isFiltered}
          onClearSearch={() => sessionSearch.setQuery('')}
          onQuickResume={handleQuickResume}
          listMode={listMode}
          onNavigate={navigate}
          onExit={handleExitList}
          collapsedKeys={collapsedKeys}
          onToggleCollapse={toggleCollapse}
          onExpand={expandGroup}
          onCollapse={collapseGroup}
        />

        {/* Untracked-session adoption, mirroring the mobile Dashboard's Sessions
            tab: same component + same feature flag, mounted at the bottom of the
            session list so wide screens (≥1024px) can adopt sessions too. */}
        {config?.adoptionEnabled && <AdoptPanel />}
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
  } else if (tab === 'pipeline') {
    mainPane = <div className="p-4 overflow-y-auto h-full"><PipelineView /></div>
  } else if (tab === 'automation') {
    mainPane = <div className="p-4 overflow-y-auto h-full"><AutomationView onOpenSession={focusSession} /></div>
  } else if (tab === 'insights') {
    mainPane = <div className="p-4 overflow-y-auto h-full"><InsightsView /></div>
  } else {
    mainPane = selection.type === 'session'
      ? <MainPaneSession sessionId={selection.id} onClosed={() => setSelection(advanceFrom(selection.id))} />
      : <WelcomePane />
  }

  const sessionActive = tab === 'sessions' && selection.type === 'session'

  const bottomPrompt = sessionActive ? (
    <BottomComposer />
  ) : (
    <DispatchBar
      repos={reposQuery.data ?? []}
      reposLoading={reposQuery.isLoading}
      reposError={reposQuery.isError}
      accounts={accounts}
      models={models}
      onDispatch={handleDispatch}
      onNavigate={navigate}
      onOpenHighlighted={handleOpenHighlighted}
      onExpandHighlighted={handleExpandHighlighted}
      onCollapseHighlighted={handleCollapseHighlighted}
      onToggleHighlighted={handleToggleHighlighted}
      onEnterList={handleEnterList}
      onJump={handleJump}
      onShowHelp={() => setShowHelp(true)}
      onEscape={() => setHighlightedIndex(-1)}
      activeDescendantId={
        highlightedSession ? `row-${highlightedSession.id}`
        : highlightedGroupKey ? `group-header-${highlightedGroupKey}`
        : undefined
      }
      activeDescendantIsHeader={!!highlightedGroupKey}
      autoFocusKey={dispatchFocusNonce}
      isPending={createSession.isPending}
    />
  )

  const iconBtn = cn(
    'flex items-center justify-center h-9 w-9 rounded-md',
    'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200',
    'hover:bg-gray-100 dark:hover:bg-gray-800',
    'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
  )
  const shell = (
    <div className="flex flex-col h-screen bg-background text-foreground">
      <header className="flex items-stretch justify-between border-b border-border flex-shrink-0">
        <div className="flex items-center px-4 shrink-0">
          <h1 className="text-lg font-bold tracking-tight">ccswitch</h1>
        </div>
        {/* Tabs span the header, between the logo and the action icons. `min-w-0`
            lets the strip shrink and scroll rather than shove the icons off-screen. */}
        <TabBar
          tabs={tabs}
          activeTab={tab}
          onSelect={(t) => { setTab(t); if (t !== 'archived') setReviewId(null) }}
          label="Dashboard sections"
          className="flex-1 min-w-0 items-stretch px-2"
        />
        <div className="flex items-center gap-1 px-2 shrink-0">
          <button
            type="button"
            onClick={() => setShowHelp(true)}
            aria-label="Open help"
            className={iconBtn}
          >
            <HelpCircle size={18} aria-hidden="true" />
          </button>
          <button
            ref={settingsButtonRef}
            type="button"
            onClick={() => setShowSettings(true)}
            aria-label="Settings"
            className={iconBtn}
          >
            <Settings size={18} aria-hidden="true" />
          </button>
        </div>
      </header>

      {allLimitedReset !== undefined && (
        <div className="px-4 pt-2 flex-shrink-0">
          <AllLimitedBanner earliestReset={allLimitedReset} onDismiss={() => setAllLimitedReset(undefined)} />
        </div>
      )}

      {/* Pending-deploy banner. DeployPanel self-returns null when up to date, so the
          wrapper collapses (`:empty` → hidden) and adds no gap on the common idle path.
          Mounted here as well as in the mobile Dashboard so wide screens see it too. */}
      <div className="px-4 pt-2 flex-shrink-0 [&:empty]:hidden">
        <DeployPanel />
      </div>

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
      {showHelp && <HelpDrawer onClose={() => setShowHelp(false)} />}
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
