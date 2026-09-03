import { useState, useRef, useCallback, useEffect, useMemo } from 'react'
import { Plus, Settings, HelpCircle } from 'lucide-react'
import { Button } from '@/ui/Button'
import { SessionList } from './SessionList'
import { QueuedSection } from './QueuedSection'
import { PendingApprovalSection } from './PendingApprovalSection'
import { AdoptPanel } from './AdoptPanel'
import { AllLimitedBanner } from './AllLimitedBanner'
import { UpdatePendingBanner } from './UpdatePendingBanner'
import { BannerStack } from '@/ui/BannerStack'
import { useInputHold } from '@/hooks/useInputHold'
import { SettingsView } from './settings/SettingsView'
import { DaemonHealthIndicator } from './DaemonHealthIndicator'
import { DispatchBar } from './DispatchBar'
import { useGlobalListNav } from './useGlobalListNav'
import { useGlobalHelpKey } from './useGlobalHelpKey'
import { HelpDrawer } from './HelpDrawer'
import { ArchivedManager } from './ArchivedManager'
import { PipelineTab } from '@/pipeline/PipelineTab'
import { InsightsView } from './InsightsView'
import { AutomationsTab } from '@/automations/AutomationsTab'
import { InitiativesTab } from '@/initiatives/InitiativesTab'
import { ProjectsTab } from '@/projects/ProjectsTab'
import { TabBar } from './TabBar'
import { buildTabs } from './tabDefs'
import { inFlightPrCount } from '@/session/prStatus'
import { isPreSpawnStatus } from '@/api/types'
import { PermanentDeleteDialog } from '@/components/PermanentDeleteDialog'
import {
  useAccounts,
  useSessions,
  useSessionsViewing,
  useCreateSession,
  useRenameSession,
  useCloseSession,
  useGithubRepos,
  useModels,
  useSettings,
  useArchivedSessions,
  usePermanentDeleteSession,
  useConfig,
  useSendPrompt,
  useAllPrs,
  useAutomations,
  type CreateSessionInput,
} from '@/api/hooks'
import { usePageVisible } from '@/hooks/usePageVisible'
import { useFleetHosts, enabledHosts } from '@/api/fleet'
import { toast } from 'sonner'
import { setLastRepo } from '@/lib/lastRepo'
import { SessionListControls } from './SessionListControls'
import { useSessionOrdering } from '@/session/useSessionOrdering'
import { useSessionSearch } from '@/session/useSessionSearch'
import { useListNavigation } from '@/session/useListNavigation'
import { useNeedsYouTitle } from '@/hooks/useNeedsYouTitle'
import { DeployPanel } from './DeployPanel'
import { NewSessionDialog } from '@/features/sessions/NewSessionDialog'
import { RenameDialog } from '@/features/sessions/RenameDialog'

// ── Dashboard ────────────────────────────────────────────────────────────────

export type TabId = 'sessions' | 'archived' | 'pipeline' | 'automations' | 'initiatives' | 'projects' | 'insights' | 'settings'

interface DashboardProps {
  /** Called when the user opens a session (navigates to Terminal view).
   *  `initialPrompt` is passed when opening straight from create, so the prompt
   *  can be shown immediately in the conversation. */
  onOpenSession?: (sessionId: string, initialPrompt?: string) => void
  /** Bump this to re-focus the dispatch bar (e.g. after returning from a session). */
  focusDispatchSignal?: unknown
  /** Called when the user wants to review an archived session. */
  onReviewSession?: (sessionId: string) => void
  /** If set, the archived tab is selected on mount (uncontrolled fallback only). */
  initialTab?: TabId
  /** Controlled active tab. When provided with onTabChange, the route owns the tab. */
  activeTab?: TabId
  /** Called when the user selects a tab. Presence switches the component to controlled mode. */
  onTabChange?: (tab: TabId) => void
  /** Controlled dialog state. Presence of onDialogChange switches to controlled mode. */
  dialog?: 'new' | null
  /** Called to open/close a URL-addressable dialog. */
  onDialogChange?: (dialog: 'new' | null) => void
}

export function Dashboard({ onOpenSession, focusDispatchSignal, onReviewSession, initialTab, activeTab: activeTabProp, onTabChange, dialog, onDialogChange }: DashboardProps) {
  const { data: accounts = [], isLoading: accountsLoading } = useAccounts()
  const { data: sessions = [], isLoading: sessionsLoading } = useSessions()
  const sessionSearch = useSessionSearch()
  // Queued sessions are displayed separately by QueuedSection and must not
  // enter useSessionOrdering — when groups are provided, SessionList renders
  // groups directly and ignores the sessions prop, so filtering after the fact
  // has no effect on what appears in the grouped list.
  const queuedSessions = useMemo(
    () => sessions.filter(s => s.status === 'queued')
      .sort((a, b) => (a.queuePosition ?? 0) - (b.queuePosition ?? 0)),
    [sessions],
  )
  // Pending-approval sessions (launch-time model gate) are also surfaced separately
  // so they never enter the grouped list and always have visible Approve/Deny controls.
  const pendingApprovalSessions = useMemo(
    () => sessions.filter(s => s.status === 'pending_approval'),
    [sessions],
  )
  const nonQueuedSessions = useMemo(
    () => sessions.filter(s => !isPreSpawnStatus(s.status)),
    [sessions],
  )
  const { groupBy, sortBy, setGroupBy, setSortBy, groups, orderedSessions: liveSessions, navItems, collapsedKeys, toggleCollapse, expandGroup, collapseGroup } =
    useSessionOrdering(nonQueuedSessions, sessionSearch.matchedIds)
  useNeedsYouTitle(sessions)
  const { data: archivedSessions = [] } = useArchivedSessions()
  const { data: settingsData } = useSettings()
  const { data: config } = useConfig()
  const reposQuery = useGithubRepos()
  const { data: models = [] } = useModels()
  const { data: fleetData } = useFleetHosts()
  const enabledFleetHosts = enabledHosts(fleetData)

  const createSession = useCreateSession()
  const renameSession = useRenameSession()
  const closeSession = useCloseSession()
  const permanentDeleteSession = usePermanentDeleteSession()

  const [internalTab, setInternalTab] = useState<TabId>(activeTabProp ?? initialTab ?? 'sessions')
  const activeTab = activeTabProp ?? internalTab
  const pageVisible = usePageVisible()
  useSessionsViewing(activeTab === 'sessions' && pageVisible)
  const setActiveTab = (tab: TabId) => {
    if (onTabChange) onTabChange(tab)
    else setInternalTab(tab)
  }
  const [showNewDialog, setShowNewDialog] = useState(false)
  const dialogControlled = !!onDialogChange
  const newDialogOpen = dialogControlled ? dialog === 'new' : showNewDialog
  function dismissNewDialog() {
    if (dialogControlled) onDialogChange!(null)
    else setShowNewDialog(false)
  }
  const [showHelp, setShowHelp] = useState(false)
  const [renameTarget, setRenameTarget] = useState<{ id: string; name: string } | null>(null)
  const [allLimitedReset, setAllLimitedReset] = useState<string | null | undefined>(undefined)
  const inputHold = useInputHold()
  const [branchError, setBranchError] = useState<string | null>(null)
  // Permanent-delete confirmation for a live (non-archived) session card.
  const [sessionDeleteTarget, setSessionDeleteTarget] = useState<{ id: string; name: string } | null>(null)

  // ── Keyboard selection state ─────────────────────────────────────────────────
  const {
    highlightedIndex, setHighlightedIndex,
    highlightedSession, highlightedGroupKey, navigate, highlightSession,
  } = useListNavigation(navItems)
  const selectedId = highlightedSession?.id ?? null

  // ── List-focus mode ───────────────────────────────────────────────────────────
  const [listMode, setListMode] = useState(false)
  const [focusNonce, setFocusNonce] = useState(0)

  function handleEnterList() {
    setListMode(true)
    if (highlightedIndex < 0) setHighlightedIndex(0)
  }
  function handleExitList() {
    setListMode(false)
    setFocusNonce((n) => n + 1) // pull focus back to the prompt
  }
  function handleJump(index: number) {
    const s = liveSessions[index]
    if (s) { highlightSession(s.id); handleOpen(s.id) }
  }

  const newButtonRef = useRef<HTMLButtonElement>(null)

  const hasNeedsReauth = accounts.some(
    (a) => a.status === 'needs-reauth' || a.status === 'needs-relogin',
  )

  // Live count badges for the tab strip (shared cache with the Pipeline tab).
  const { data: prs = [] } = useAllPrs()
  const { data: automationsData = [] } = useAutomations()
  const automationsActiveSessions = automationsData.reduce((n, a) => n + a.activeSessions, 0)
  const tabs = buildTabs({
    sessions: sessions.length,
    pipeline: inFlightPrCount(prs),
    automations: automationsActiveSessions || undefined,
    archived: archivedSessions.length,
    settingsAttention: hasNeedsReauth,
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

  function openNewDialog() {
    setAllLimitedReset(undefined)
    if (dialogControlled) onDialogChange!('new')
    else setShowNewDialog(true)
  }

  function closeNewDialog() {
    setBranchError(null)
    dismissNewDialog()
    newButtonRef.current?.focus()
  }

  function handleCreateSubmit(input: CreateSessionInput) {
    createSession.mutate(input, {
      onSuccess: (session) => {
        dismissNewDialog()
        if (session.usageUnverified) {
          toast("Couldn't verify account usage — started anyway.")
        }
        // Auto-open the new session and carry the prompt so it shows immediately.
        if (onOpenSession) onOpenSession(session.id, input.initialPrompt)
      },
      onError: (err) => {
        if (err.status === 409) {
          const body = err.body as { error?: { code?: string; message?: string }; earliestReset?: string | null }
          if (body?.error?.code === 'branch_switch_failed') {
            setBranchError(body.error.message ?? 'Could not switch branch')
          } else if (body?.error?.code === 'accounts_need_reauth') {
            toast.error('No usable accounts — check Settings → Accounts to re-authenticate.')
            dismissNewDialog()
          } else {
            const reset = body.earliestReset ?? null
            setAllLimitedReset(reset)
            dismissNewDialog()
          }
        }
      },
    })
  }

  const handleOpen = useCallback((id: string) => {
    if (id.startsWith('pending-')) return
    if (onOpenSession) onOpenSession(id)
  }, [onOpenSession])

  const sendPrompt = useSendPrompt()
  const handleQuickResume = useCallback((id: string, text?: string) => {
    // Always fire in place — the backend lazily resumes a detached session first,
    // so this unsticks idle/"Ready" sessions without leaving the list.
    sendPrompt.mutate({ id, text: text || 'continue' })
  }, [sendPrompt])

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

  // Open the permanent-delete confirmation for a live session card.
  const handleSessionDelete = useCallback((id: string) => {
    const session = sessions.find((s) => s.id === id)
    setSessionDeleteTarget({ id, name: session?.name ?? id.slice(0, 8) })
  }, [sessions])

  function confirmSessionDelete() {
    if (!sessionDeleteTarget) return
    permanentDeleteSession.mutate(sessionDeleteTarget.id, {
      onSuccess: () => setSessionDeleteTarget(null),
      onError: () => setSessionDeleteTarget(null),
    })
  }

  // ── DispatchBar handlers ─────────────────────────────────────────────────────

  useGlobalListNav({ enabled: activeTab === 'sessions', onNavigate: navigate })
  useGlobalHelpKey({ enabled: activeTab === 'sessions', onShowHelp: () => setShowHelp(true) })

  function handleOpenHighlighted() {
    if (highlightedSession) handleOpen(highlightedSession.id)
  }

  function handleExpandHighlighted() { if (highlightedGroupKey) expandGroup(highlightedGroupKey) }
  function handleCollapseHighlighted() { if (highlightedGroupKey) collapseGroup(highlightedGroupKey) }
  function handleToggleHighlighted() { if (highlightedGroupKey) toggleCollapse(highlightedGroupKey) }

  function handleDispatch({ initialPrompt, repo, account, model, host, attachmentIds }: { initialPrompt: string; repo: string | null; account: string | null; model: string | null; host: string | null; attachmentIds: string[] }): Promise<void> {
    const repoInfo = repo ? (reposQuery.data ?? []).find((r) => r.nameWithOwner === repo) : undefined
    return new Promise((resolve, reject) => {
      createSession.mutate(
        {
          initialPrompt,
          repo: repo ?? undefined,
          branch: repoInfo?.defaultBranch ?? undefined,
          account: account ?? undefined,
          model: model ?? undefined,
          host: host ?? undefined,
          attachmentIds: attachmentIds.length ? attachmentIds : undefined,
        },
        {
          onSuccess: (session) => {
            if (repo) setLastRepo(repo)
            if (onOpenSession) onOpenSession(session.id, initialPrompt)
            resolve()
          },
          onError: (err) => reject(err),
        },
      )
    })
  }

  function handleReview(id: string) {
    if (onReviewSession) onReviewSession(id)
  }

  const isLoading = accountsLoading || sessionsLoading

  return (
    <div className="flex flex-1 min-h-0 flex-col gap-4 pt-4">
      {/* Banner region — global, above tabs. One BannerStack per shell (see DesktopShell's
          equivalent), priority-ordered, so a banner added here can't be forgotten there. */}
      <BannerStack items={[
        { id: 'all-limited', priority: 70, node: allLimitedReset !== undefined
          ? <AllLimitedBanner earliestReset={allLimitedReset} onDismiss={() => setAllLimitedReset(undefined)} />
          : null },
        { id: 'input-hold', priority: 60, node: inputHold.holding
          ? <UpdatePendingBanner queuedCount={inputHold.count} mode={inputHold.mode} />
          : null },
        { id: 'deploy', priority: 50, node: <DeployPanel /> },
      ]} />

      {/* Tab strip — horizontally scrollable so all sections stay reachable on a
          narrow viewport. */}
      <TabBar
        tabs={tabs}
        activeTab={activeTab}
        onSelect={setActiveTab}
        label="Dashboard sections"
        panelId={(id) => `tabpanel-${id}`}
        className="border-b border-gray-200 dark:border-gray-700"
      />

      {/* Sessions panel */}
      {activeTab === 'sessions' && (
        <div
          id="tabpanel-sessions"
          role="tabpanel"
          aria-labelledby="tab-sessions"
          className="flex flex-1 min-h-0 flex-col gap-3"
        >
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <h2 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
                Sessions
              </h2>
              <DaemonHealthIndicator />
            </div>
            <div className="flex items-center gap-2">
              <Button
                size="sm"
                variant="secondary"
                className="min-h-[44px]"
                onClick={() => setShowHelp(true)}
                aria-label="Open help"
              >
                <HelpCircle size={14} aria-hidden="true" />
              </Button>
              <Button
                size="sm"
                variant="secondary"
                className="min-h-[44px]"
                onClick={() => setActiveTab('settings')}
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
          {/* Scroll region: only the session list scrolls; the controls above
              and the DispatchBar below stay fixed within the viewport. */}
          <div className="flex-1 min-h-0 overflow-y-auto space-y-3">
          <PendingApprovalSection sessions={pendingApprovalSessions} onOpen={handleOpen} />
          <QueuedSection sessions={queuedSessions} onOpen={handleOpen} />
          <SessionList
            sessions={liveSessions}
            groups={groups}
            accounts={accounts}
            isLoading={isLoading}
            selectedId={selectedId}
            selectedGroupKey={highlightedGroupKey ?? null}
            onNew={openNewDialog}
            onOpen={handleOpen}
            onRename={handleRename}
            onClose={handleClose}
            onDelete={handleSessionDelete}
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

          {config?.adoptionEnabled && <AdoptPanel />}
          </div>

          <DispatchBar
            repos={reposQuery.data ?? []}
            reposLoading={reposQuery.isLoading}
            reposError={reposQuery.isError}
            accounts={accounts}
            models={models}
            hosts={enabledFleetHosts}
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
            autoFocusKey={`${String(focusDispatchSignal)}:${focusNonce}`}
            isPending={createSession.isPending}
          />
        </div>
      )}

      {/* Archived panel — single source of truth: ArchivedManager owns its own data/state */}
      {activeTab === 'archived' && (
        <div
          id="tabpanel-archived"
          role="tabpanel"
          aria-labelledby="tab-archived"
          className="flex-1 min-h-0 overflow-y-auto space-y-3 pb-4"
        >
          <ArchivedManager onReviewSession={handleReview} />
        </div>
      )}

      {/* Pipeline panel — PipelineTab owns its own scroll region
          (flex-1 min-h-0 overflow-y-auto), so the panel wrapper only
          provides the flex-column context it needs. */}
      {activeTab === 'pipeline' && (
        <div className="flex flex-col flex-1 min-h-0">
          <PipelineTab onOpenInsights={() => setActiveTab('insights')} />
        </div>
      )}

      {/* Automations panel — live status and actions for each automation */}
      {activeTab === 'automations' && (
        <div
          id="tabpanel-automations"
          role="tabpanel"
          aria-labelledby="tab-automations"
          className="flex-1 min-h-0 overflow-y-auto pb-4"
        >
          <AutomationsTab
            onOpenSession={onOpenSession ? (id) => onOpenSession(id) : undefined}
            onReviewSession={onReviewSession ? handleReview : undefined}
          />
        </div>
      )}

      {/* Initiatives panel — the SDLC artifact-chain board (owns its scroll region) */}
      {activeTab === 'initiatives' && (
        <div
          id="tabpanel-initiatives"
          role="tabpanel"
          aria-labelledby="tab-initiatives"
          className="flex flex-col flex-1 min-h-0"
        >
          <InitiativesTab />
        </div>
      )}

      {/* Products panel — PM phase-1 project registry */}
      {activeTab === 'projects' && (
        <div
          id="tabpanel-projects"
          role="tabpanel"
          aria-labelledby="tab-projects"
          className="flex flex-col flex-1 min-h-0"
        >
          <ProjectsTab />
        </div>
      )}

      {/* Insights panel */}
      {activeTab === 'insights' && (
        <div className="flex-1 min-h-0 overflow-y-auto pb-4">
          <InsightsView />
        </div>
      )}

      {/* Settings panel — own scroll region, like every sibling panel: the tab
          area is a clipped flex column (flex-1 min-h-0), so without this an
          expanded section overflows with nowhere to scroll on mobile. */}
      {activeTab === 'settings' && (
        <div
          id="tabpanel-settings"
          role="tabpanel"
          aria-labelledby="tab-settings"
          className="flex-1 min-h-0 overflow-y-auto pb-4 px-4"
        >
          <SettingsView />
        </div>
      )}

      {/* Dialogs */}
      {newDialogOpen && (
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

      {sessionDeleteTarget && (
        <PermanentDeleteDialog
          sessionName={sessionDeleteTarget.name}
          onClose={() => setSessionDeleteTarget(null)}
          onConfirm={confirmSessionDelete}
          isPending={permanentDeleteSession.isPending}
        />
      )}

      {showHelp && <HelpDrawer onClose={() => setShowHelp(false)} />}
    </div>
  )
}
