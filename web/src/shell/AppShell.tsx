/* eslint-disable react-refresh/only-export-components -- mixed export file: the
   shell component and the region-visibility matrix it is built from. Splitting
   them would put the layout and the rule that decides what the layout contains
   in two files, which is the drift this module exists to prevent. */
import * as React from 'react'
import { useLocation, useNavigate, useParams } from 'react-router-dom'
import { cn } from '@/lib/utils'
import { DeployControllerProvider } from '@/lib/deployController'
import { BannerStack } from '@/ui/BannerStack'
import {
  useAccounts, useCloseSession, useConfig, usePermanentDeleteSession,
  useRenameSession, useSendPrompt, useSessions,
} from '@/api/hooks'
import { RenameDialog } from '@/components/Dashboard'
import { PermanentDeleteDialog } from '@/components/PermanentDeleteDialog'
import { useGlobalHelpKey } from '@/components/useGlobalHelpKey'
import { useSessionListState } from '@/features/sessions/useSessionListState'
import type { SessionListColumnProps } from '@/features/sessions/SessionListColumn'
import { nextSessionAfterClose } from '@/session/nextAfterArchive'
import { SESSIONS_PATH, sessionPath } from '@/session/sessionNavState'
import { useDrawerName, useOpenDrawer, type DrawerName } from './drawerParam'
import { InteractionProvider } from './InteractionContext'
import { readKioskConfig } from './kiosk'
import { navDefForPath } from './navDefs'
import { useOpenSessionId } from './openSessionId'
import { useAppBanners } from './useAppBanners'
import { ViewportProvider, useViewport, type LayoutMode } from './useViewport'
import { ContextList } from './regions/ContextList'
import { DrawerHost } from './regions/DrawerHost'
import { NavBar } from './regions/NavBar'
import { NavRail } from './regions/NavRail'
import { Overlays } from './regions/Overlays'
import { PageHost } from './regions/PageHost'
import { PromptDock } from './regions/PromptDock'
import { TopBar } from './regions/TopBar'

/**
 * `AppShell` — one shell for every layout mode, replacing the `Dashboard` /
 * `DesktopShell` fork in `App.tsx` (Task 17 performs the swap).
 *
 * ## Regions are placed by a table, not by nested conditionals
 *
 * Which regions exist in which mode is `REGION_VISIBILITY` below — pure
 * predicates over a `RegionContext`, exported so `visibilityMatrix.test.tsx`
 * can assert all 28 mode × region cells directly. The JSX asks the table; it
 * never re-derives a rule. That matters because the two shells this replaces
 * each carried their OWN answer to "is there a sidebar", "is there a prompt
 * bar", "is there a header" — which is how the phone layout came to have a
 * Runner picker the desktop layout did not, and the desktop layout a
 * `PendingApprovalSection` the phone never showed.
 *
 * ## What the shell owns, and why
 *
 * The Sessions list state (`useSessionListState`) is held HERE rather than in
 * `ContextList`, because `PromptDock` drives the same cursor: ↑/↓ and Alt+digit
 * typed at the prompt move the highlight the list column renders. One handle,
 * two regions — both old shells did the same and it is the reason the list and
 * the prompt never disagreed within a shell.
 *
 * The destructive-action dialogs (rename, permanent delete) are here for the
 * same reason they were in both old shells: the row that asks for them lives in
 * `app.list`, the confirmation is app-modal, and the advance-to-neighbour that
 * follows a delete is a NAVIGATION, which is the shell's business. They portal
 * to the overlay root, so they render inside `app.overlays` by construction —
 * `Overlays` does not need to know they exist.
 */

/** Every region the shell can place. `RegionId` is derived from it, so the
 *  matrix, the tests and the type cannot list different regions. */
export const REGION_IDS = [
  'app.banners', 'app.rail', 'app.list', 'app.main', 'app.dock', 'app.drawer', 'app.overlays',
] as const

export type RegionId = (typeof REGION_IDS)[number]

/** Everything the visibility rules are allowed to depend on. */
export interface RegionContext {
  mode: LayoutMode
  /** `navDefForPath(pathname)?.hasList` — never a hardcoded list of sections. */
  hasList: boolean
  /** The route has an `:id` — a detail view is open. */
  itemOpen: boolean
  /** The current section is Sessions (DEC-10 gates the dock on it). */
  inSessions: boolean
  /** The open drawer, or null. An unrecognised `?drawer=` reads as null. */
  drawer: DrawerName | null
}

/**
 * The matrix from umbrella §4.2, as data.
 *
 * Kiosk is `mode !== 'kiosk'` four times over rather than a `kioskRegions` set,
 * because the SHAPE of the rule is per region: the banners region stays and is
 * FILTERED by tone (Task 19, inside the stack), while the rail, list, dock and
 * drawer are absent outright. Collapsing those into one "kiosk hides these"
 * list would lose that distinction the first time someone read it.
 */
export const REGION_VISIBILITY: Record<RegionId, (c: RegionContext) => boolean> = {
  // Kiosk FILTERS the stack to `tone === 'danger'` (Task 19); it does not hide
  // the region, so a wall display can still shout that the daemon is down.
  'app.banners': () => true,
  'app.rail': (c) => c.mode !== 'kiosk',
  // Desktop is master-detail — the list stays beside an open item. Phone and
  // tablet give the one band to the item instead.
  'app.list': (c) => c.mode !== 'kiosk' && c.hasList && (c.mode === 'desktop' || !c.itemOpen),
  'app.main': () => true,
  'app.dock': (c) => c.mode !== 'kiosk' && c.inSessions,
  'app.drawer': (c) => c.mode !== 'kiosk' && c.drawer !== null,
  'app.overlays': () => true,
}

export function AppShell() {
  // Read once at mount: a wall display changes its kiosk URL by reloading, and
  // re-reading on every render would let a mid-session `?kiosk=1` flip the whole
  // interaction model under a user's hands.
  const [kiosk] = React.useState(readKioskConfig)
  return (
    <ViewportProvider kiosk={kiosk}>
      <InteractionProvider interactive={!kiosk.enabled}>
        {/* `Overlays` throws without this provider (it hosts `DeployOverlay`),
            and `DeployPanel` — mounted from the Settings page — must share the
            same controller instance, so it wraps the whole shell. */}
        <DeployControllerProvider>
          <AppShellInner />
        </DeployControllerProvider>
      </InteractionProvider>
    </ViewportProvider>
  )
}

function AppShellInner() {
  const { mode } = useViewport()
  const { pathname } = useLocation()
  // Layout-route `useParams()` sees the child route's params (react-router
  // shares one params object across a matched branch) — the same read
  // `useOpenSessionId` makes from `PageHost`, which is mounted the same way.
  const { id } = useParams()
  const drawer = useDrawerName()
  const openDrawer = useOpenDrawer()

  const def = navDefForPath(pathname)
  const region: RegionContext = {
    mode,
    hasList: def?.hasList ?? false,
    itemOpen: id !== undefined,
    inSessions: def?.id === 'sessions',
    drawer,
  }
  const shows = (r: RegionId) => REGION_VISIBILITY[r](region)

  const workspace = useSessionsWorkspace(openDrawer)

  // `?` opens the help drawer. Both old shells gated this on their Sessions tab
  // because neither could render a drawer anywhere else; `DrawerHost` is a
  // region now, so the key works app-wide — except in kiosk, which has no
  // drawer region to open into.
  useGlobalHelpKey({ enabled: mode !== 'kiosk', onShowHelp: () => openDrawer('help') })

  const phone = mode === 'phone'
  const listVisible = shows('app.list')
  // Phone and tablet hand the single content band to EITHER the list or the
  // page (§4.1 "list | main"); desktop shows both. The page is hidden rather
  // than the region, so the dock inside it survives — see `PageHost`.
  const pageHidden = listVisible && mode !== 'desktop'

  return (
    <div className="flex-1 min-h-0 flex flex-col overflow-hidden bg-canvas text-ink">
      {shows('app.banners') && <BannerStack items={workspace.banners} className="shrink-0 px-2 pt-2" />}
      {shows('app.rail') && phone && <TopBar />}

      <div className="flex min-h-0 flex-1">
        {shows('app.rail') && !phone && <NavRail />}

        {/* Desktop is master-detail: the list sits BESIDE the page. Everywhere
            else the band is a column, so the list stacks above the dock — which
            lives inside `app.main` and must stay under the content, never
            beside it. */}
        <div
          className={cn(
            'flex min-w-0 min-h-0 flex-1',
            mode === 'desktop' ? 'flex-row' : 'flex-col',
          )}
        >
          {listVisible && <ContextList sessions={workspace.list} />}
          <PageHost
            pageHidden={pageHidden}
            dock={shows('app.dock') ? <PromptDock {...workspace.dock} /> : undefined}
          />
        </div>

        {/* At ≥1280 this is an inline column beside the page; below it, an
            overlay `Sheet` that portals out of the layout entirely. `DrawerHost`
            owns that switch (`useViewport().drawerPushes`), so the shell places
            it in the band either way. */}
        {shows('app.drawer') && <DrawerHost />}
      </div>

      {shows('app.rail') && phone && <NavBar />}

      {shows('app.overlays') && <Overlays />}
      {workspace.dialogs}
    </div>
  )
}

/**
 * The Sessions workspace's state and handlers — the half of the old shells that
 * was not layout.
 *
 * It is a hook rather than inline state so `AppShellInner` reads as the layout
 * it is. Nothing here is mode-aware: what the shell shows is the matrix's job,
 * and a second opinion about it in here is exactly the split that let the two
 * old shells drift.
 */
function useSessionsWorkspace(openDrawer: (name: DrawerName) => void) {
  const navigate = useNavigate()
  const { data: sessions = [], isLoading: sessionsLoading } = useSessions()
  const { data: accounts = [] } = useAccounts()
  const { data: config } = useConfig()

  const list = useSessionListState(sessions)
  const openSessionId = useOpenSessionId()

  const closeSession = useCloseSession()
  const renameSession = useRenameSession()
  const permanentDelete = usePermanentDeleteSession()
  const sendPrompt = useSendPrompt()

  const [listMode, setListMode] = React.useState(false)
  const [promptFocusKey, setPromptFocusKey] = React.useState(0)
  const [renameTarget, setRenameTarget] = React.useState<{ id: string; name: string } | null>(null)
  const [deleteTarget, setDeleteTarget] = React.useState<{ id: string; name: string } | null>(null)
  // `undefined` = no banner; `null` = limited with no known reset time.
  const [allLimitedReset, setAllLimitedReset] = React.useState<string | null | undefined>(undefined)

  const banners = useAppBanners({
    allLimitedReset,
    onDismissAllLimited: () => setAllLimitedReset(undefined),
  })

  const { orderedSessions, allOrderedSessions } = list.ordering

  /**
   * Where the detail view goes when the OPEN session leaves the live list
   * (§4.4). `replace`, because the session that is gone is not a place Back
   * should return to.
   */
  const advanceFrom = React.useCallback(
    (removedId: string) => {
      if (removedId !== openSessionId) return
      const next = nextSessionAfterClose(
        orderedSessions.map((s) => s.id),
        allOrderedSessions.map((s) => s.id),
        removedId,
      )
      navigate(next ? sessionPath(next) : SESSIONS_PATH, { replace: true })
    },
    [openSessionId, orderedSessions, allOrderedSessions, navigate],
  )

  const nameOf = (id: string) =>
    sessions.find((s) => s.id === id)?.name ?? id.slice(0, 8)

  const listProps: SessionListColumnProps = {
    state: list,
    accounts,
    isLoading: sessionsLoading,
    selectedId: openSessionId,
    newSelected: openSessionId === null,
    adoptionEnabled: config?.adoptionEnabled,
    listMode,
    onNew: () => navigate(SESSIONS_PATH),
    onOpen: (id) => navigate(sessionPath(id)),
    onRename: (id) => setRenameTarget({ id, name: nameOf(id) }),
    onClose: (id) => {
      closeSession.mutate(id)
      advanceFrom(id)
    },
    // Permanent delete is destructive, so it goes through a confirm dialog and
    // the mutation runs only on confirm — both old shells did the same.
    onDelete: (id) => setDeleteTarget({ id, name: nameOf(id) }),
    onQuickResume: (id, text) => sendPrompt.mutate({ id, text: text || 'continue' }),
    onExitList: () => {
      setListMode(false)
      setPromptFocusKey((n) => n + 1)
    },
  }

  const dockProps = {
    list,
    accounts,
    onEnterList: () => setListMode(true),
    onShowHelp: () => openDrawer('help'),
    onAllLimited: (earliestReset: string | null) => setAllLimitedReset(earliestReset),
    autoFocusKey: promptFocusKey,
  }

  const dialogs = (
    <>
      {renameTarget && (
        <RenameDialog
          sessionId={renameTarget.id}
          currentName={renameTarget.name}
          onClose={() => setRenameTarget(null)}
          isPending={renameSession.isPending}
          onSubmit={(id, name) =>
            renameSession.mutate({ id, name }, { onSuccess: () => setRenameTarget(null) })
          }
        />
      )}
      {deleteTarget && (
        <PermanentDeleteDialog
          sessionName={deleteTarget.name}
          onClose={() => setDeleteTarget(null)}
          isPending={permanentDelete.isPending}
          onConfirm={() => {
            const { id } = deleteTarget
            permanentDelete.mutate(id, {
              onSuccess: () => {
                setDeleteTarget(null)
                advanceFrom(id)
              },
              onError: () => setDeleteTarget(null),
            })
          }}
        />
      )}
    </>
  )

  return { banners, list: listProps, dock: dockProps, dialogs }
}
