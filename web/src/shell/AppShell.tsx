/* eslint-disable react-refresh/only-export-components -- mixed export file: the
   shell component and the region-visibility matrix it is built from. Splitting
   them would put the layout and the rule that decides what the layout contains
   in two files, which is the drift this module exists to prevent. */
import * as React from 'react'
import { Navigate, useLocation, useNavigate, useParams } from 'react-router-dom'
import { cn } from '@/lib/utils'
import { DeployControllerProvider } from '@/lib/deployController'
import { BannerStack, type BannerStackItem } from '@/ui/BannerStack'
import {
  useAccounts, useCloseSession, useConfig, usePermanentDeleteSession,
  useRenameSession, useSendPrompt, useSessions,
} from '@/api/hooks'
import { NewSessionDialogHost } from '@/features/sessions/NewSessionDialogHost'
import { RenameDialog } from '@/features/sessions/RenameDialog'
import { PermanentDeleteDialog } from '@/components/PermanentDeleteDialog'
import { useSessionListState } from '@/features/sessions/useSessionListState'
import type { SessionListColumnProps } from '@/features/sessions/SessionListColumn'
import { isPendingSessionId } from '@/lib/pendingSessions'
import { setServerSttEnabled } from '@/lib/sttCapability'
import { textEntryClaimsKey } from '@/lib/detachKeyGuard'
import { nextSessionAfterClose } from '@/session/nextAfterArchive'
import { SESSIONS_PATH, sessionPath } from '@/session/sessionNavState'
import { useDialogName } from './dialogParam'
import { useDrawerName, useOpenDrawer, type DrawerName } from './drawerParam'
import { InteractionProvider } from './InteractionContext'
import { readKioskConfig } from './kiosk'
import { NAV_BY_ID, navDefForPath } from './navDefs'
import { useOpenSessionId } from './openSessionId'
import { SessionGestureProvider, useSessionGestureHandle } from './sessionGestures'
import { overlayIsOpen } from './useOverlayState'
import { useShortcuts } from './useShortcuts'
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
 * because the SHAPE of the rule is per region: the banners region stays and its
 * CONTENTS are filtered by tone (`bannersForMode`, applied where the shell hands
 * the items to `BannerStack`), while the rail, list, dock and drawer are absent
 * outright. Collapsing those into one "kiosk hides these"
 * list would lose that distinction the first time someone read it.
 */
export const REGION_VISIBILITY: Record<RegionId, (c: RegionContext) => boolean> = {
  // Kiosk FILTERS the stack to `tone === 'danger'` (`bannersForMode` below); it
  // does not hide the region, so a wall display can still shout that the daemon
  // is down. With no danger banner live the region renders with zero children
  // and `BannerStack`'s `[&:empty]:hidden` collapses it.
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

/**
 * The `D` cell of §4.2's matrix, as a function: kiosk keeps `app.banners`
 * MOUNTED and narrows what goes in it to `tone === 'danger'`.
 *
 * A wall display has nobody in front of it, so an advisory ("a new version is
 * available", "messages are held") is noise that occupies the top of a screen
 * read from ten feet away, possibly for days. Only the two things a passer-by
 * must act on — the daemon being down and every account being rate-limited,
 * both `danger` in `useAppBanners` — earn that space.
 *
 * An item with no `tone` is documented as "treated as 'info'" by
 * `BannerStackItem`, and `!== 'danger'` gives it exactly that treatment: a new
 * banner declared without a tone is filtered OUT of kiosk rather than defaulting
 * onto a wall display unreviewed.
 *
 * Exported so the filter can be asserted as a rule, the same way
 * `REGION_VISIBILITY` is.
 */
export function bannersForMode(items: BannerStackItem[], mode: LayoutMode): BannerStackItem[] {
  return mode === 'kiosk' ? items.filter((i) => i.tone === 'danger') : items
}

/**
 * Kiosk's type scale, applied as an inline `--scale` on `<html>`.
 *
 * `index.css` defines `--scale: 1` on `:root` and every `--fs-*` step is
 * `calc(<rem> * var(--scale))`, so overriding this ONE custom property enlarges
 * the whole type scale coherently. Nothing else is needed, and nothing else
 * should be added: a second kiosk-only size rule somewhere in the tree is how
 * two type scales come to exist.
 */
export const KIOSK_SCALE = '1.25'

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
          {/* The bridge the open session publishes its keyboard gestures through
              — see `shell/sessionGestures`. It wraps the guard so the handle
              survives the kiosk redirect rather than being recreated by it. */}
          <SessionGestureProvider>
            <KioskPageGuard>
              <AppShellInner />
            </KioskPageGuard>
          </SessionGestureProvider>
        </DeployControllerProvider>
      </InteractionProvider>
    </ViewportProvider>
  )
}

/**
 * Kiosk may only ever be ON a kiosk-safe section.
 *
 * `NavDef.showInKiosk` is the "verified read-only" claim (its docstring carries
 * the criterion), and `shell/kiosk.ts` filters `?pages=` through it — but a wall
 * display is LAUNCHED at a URL, and nothing stopped that URL being
 * `/sessions/abc?kiosk=1` or `/archived?kiosk=1`. Kiosk mode there is the worst
 * of both worlds: the rail is gone so nobody can navigate away, `Dialog` refuses
 * to open so a destructive button goes silently inert, and the ungated action
 * buttons — Archive, Unarchive, a pending permission card — stay live for any
 * passer-by. So the flag gates the ROUTE too, not just the attract cycle.
 *
 * The redirect target is the first configured kiosk page; `parsePages`
 * guarantees `pages` is non-empty and every id in it is kiosk-safe, so this
 * cannot loop. The search string is carried over so `?kiosk=1` (and `?cycle=`,
 * `?theme=`) survive a reload of the redirected URL.
 *
 * This is also the first production reader of the `kiosk` payload
 * `ViewportProvider` has been carrying.
 */
function KioskPageGuard({ children }: { children: React.ReactNode }) {
  const { kiosk } = useViewport()
  const { pathname, search } = useLocation()
  if (!kiosk?.enabled) return <>{children}</>
  if (navDefForPath(pathname)?.showInKiosk) return <>{children}</>
  return <Navigate to={{ pathname: NAV_BY_ID[kiosk.pages[0]].path, search }} replace />
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

  // Kiosk enlarges the whole type scale by overriding `--scale` on `<html>`.
  // It goes on the document element rather than on the shell's root `<div>`
  // because overlays PORTAL out of that div into `#overlays` (a sibling of
  // `#root`) — a dialog or sheet scoped to the shell subtree would keep desktop
  // type while the page behind it grew. Removed on unmount so a test, or a
  // future in-app exit from kiosk, does not leave the document permanently
  // scaled.
  React.useEffect(() => {
    if (mode !== 'kiosk') return
    const root = document.documentElement
    root.style.setProperty('--scale', KIOSK_SCALE)
    // A block body, not a concise one: `removeProperty` RETURNS the old value
    // (a string), and an effect cleanup must return void.
    return () => {
      root.style.removeProperty('--scale')
    }
  }, [mode])

  // ── the app's one keydown listener ──────────────────────────────────────
  //
  // Five listeners used to do this (`useGlobalHelpKey`, `useGlobalListNav`,
  // `useInSessionKeys`, and an overflow-menu Escape in each of `SessionCard` and
  // `SessionActions`), each answering "may this key fire here?" its own way.
  // `shell/shortcuts.ts` answers it once, per row, and this is the only place
  // that asks. What stays OUT of it is component behaviour: `ui/Menu`'s roving
  // focus, `ui/Tabs`' arrows, `Composer`'s history recall, `DispatchBar`'s
  // arrows, a focused row's single-key actions. Each of those `preventDefault`s
  // the key it handles, which is precisely how it wins over this listener.
  //
  // The gesture ref, not shell state: `hasOpenAction` and the socket-detaching
  // `onDetach` belong to the session runtime, which `PageHost` mounts BELOW the
  // shell. `MainPaneSession` publishes them through `sessionGestures`; nothing
  // here re-renders when they change, because the listener reads them at
  // keydown time.
  const gestures = useSessionGestureHandle()
  useShortcuts(
    (e) => {
      const active = document.activeElement
      return {
        // `overlayIsOpen()`, not `useOverlayState()`: this runs in an event
        // handler, outside render.
        overlayOpen: overlayIsOpen(),
        hasOpenAction: gestures.current?.hasOpenAction ?? false,
        // "A session is open" means a session's gestures are registered — not
        // that the URL has an `:id`. A route can match before the pane mounts,
        // and a detach row that fires with nothing to detach would swallow
        // Escape app-wide.
        sessionOpen: gestures.current !== null,
        inSessions: region.inSessions,
        interactive: mode !== 'kiosk',
        active,
        yieldsToTextEntry: textEntryClaimsKey(e, active),
      }
    },
    {
      // Two rows, one drawer: `?` yields to a focused text entry (it is a
      // character there), the ⌘/Ctrl chord does not. The guard is the
      // difference, so the registry carries two rows and they share an effect.
      'app.help': () => openDrawer('help'),
      'app.assistant': () => openDrawer('assistant'),
      'compose.help': () => openDrawer('help'),
      'compose.nav': (e) => workspace.navigateList(e.key === 'ArrowUp' ? 'up' : 'down'),
      'insession.detach': () => gestures.current?.onDetach(),
      'insession.forceDetach': () => gestures.current?.onDetach(),
    },
  )

  const phone = mode === 'phone'
  const listVisible = shows('app.list')
  // Phone and tablet hand the single content band to EITHER the list or the
  // page (§4.1 "list | main"); desktop shows both. The page is hidden rather
  // than the region, so the dock inside it survives — see `PageHost`.
  const pageHidden = listVisible && mode !== 'desktop'

  return (
    <div className="flex-1 min-h-0 flex flex-col overflow-hidden bg-canvas text-ink">
      {shows('app.banners') && (
        <BannerStack items={bannersForMode(workspace.banners, mode)} className="shrink-0 px-2 pt-2" />
      )}
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

  // Publish the dictation-engine capability for useDictationField (see lib/sttCapability.ts).
  React.useEffect(() => { setServerSttEnabled(!!config?.sttEnabled) }, [config?.sttEnabled])

  // `sessionsLoading` is passed, not inferred from `sessions.length`: an empty
  // array means "none" and "not loaded yet" alike, and only the first may
  // sanitize a stored project filter away.
  const list = useSessionListState(sessions, { sessionsLoading })
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

  // The all-limited banner's three ways out, all ported from the two old shells
  // (each carried its own copy; this is the surviving one). Task 17 moved the
  // state here but not these rules, so without them the banner could only be
  // dismissed by hand.
  //
  // 1. An account became selectable again — a non-null `rank` means the ranker
  //    would pick it, so the banner is stale. A STANDING condition: while it
  //    holds, the banner may not come back.
  if (allLimitedReset !== undefined && accounts.some((a) => a.rank !== null)) {
    setAllLimitedReset(undefined)
  }
  // 2. The user OPENED the New Session dialog: they are acting on the banner, so
  //    it stops shouting. `Dashboard` did this inside `openNewDialog`, a click
  //    handler; the dialog is a `<Link>` now, so the trigger is the param going
  //    open — an EDGE, not a standing condition, which is why it is written as
  //    the documented "adjust state when a prop changes" pair rather than folded
  //    into the test above. Folded in, it would clear the banner on every render
  //    while the dialog is open, and the 409 that RAISES the banner arrives while
  //    it still is: the banner would be set and wiped in the same commit, and the
  //    user would never learn why nothing started.
  const newDialogOpen = useDialogName() === 'new'
  const [dialogWasOpen, setDialogWasOpen] = React.useState(newDialogOpen)
  if (dialogWasOpen !== newDialogOpen) {
    setDialogWasOpen(newDialogOpen)
    if (newDialogOpen) setAllLimitedReset(undefined)
  }
  // 3. The reset time passed. `setTimeout` rather than a render-time compare so
  //    `Date.now()` is not read during render (react-hooks/purity).
  React.useEffect(() => {
    if (allLimitedReset === undefined || allLimitedReset === null) return
    const id = setTimeout(
      () => setAllLimitedReset(undefined),
      Math.max(0, new Date(allLimitedReset).getTime() - Date.now()),
    )
    return () => clearTimeout(id)
  }, [allLimitedReset])

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
    // Same rule as the dock's `openSession` (one predicate, two entry points —
    // exactly as `Dashboard` and `DesktopShell` each guarded their own): an
    // optimistic placeholder has no server session, so a click on one must not
    // navigate to an id the daemon has never heard of.
    onOpen: (id) => { if (!isPendingSessionId(id)) navigate(sessionPath(id)) },
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
      {/* `?dialog=new` — DEC-5, open in every layout mode. The host reads the
          param itself; the shell only supplies what is app-level about it. */}
      <NewSessionDialogHost accounts={accounts} onAllLimited={setAllLimitedReset} />
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
          names={[deleteTarget.name]}
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

  return { banners, list: listProps, dock: dockProps, dialogs, navigateList: list.nav.navigate }
}
