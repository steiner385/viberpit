import * as React from 'react'
import { getOverlayRoot } from '@/ui/portal'
import { useRequireDeployController } from '@/lib/deployController'
import { DeployOverlay } from '@/components/DeployOverlay'

/** The region name, exported so tests and `AppShell` cannot spell it two ways. */
export const OVERLAY_REGION = 'app.overlays'

/**
 * How many `Overlays` instances currently hold the mark on the portal root.
 *
 * `AppShell` mounts exactly one, but a route transition can briefly overlap two
 * (an outgoing shell mid-unmount while the incoming one has already mounted),
 * and React 18 StrictMode mounts→unmounts→remounts every effect in dev. An
 * unconditional `removeAttribute` on cleanup would strip the mark while another
 * live instance still needs it. Same shape, and the same reason, as
 * `shell/documentTitle`'s base-title stack.
 */
let marks = 0

/**
 * `app.overlays` — the one portal root, plus the app-level overlay components.
 *
 * ## The region IS the portal root; it does not wrap one
 *
 * `ui/portal.ts` already owns the `#overlays` node (`index.html` ships it,
 * outside `#root`, so `inert` on `#root` cannot inert an open overlay). This
 * region does not create a second container and does not nest one inside it —
 * it STAMPS `data-region="app.overlays"` onto whatever `getOverlayRoot()`
 * returns.
 *
 * That is the difference between a claim and a guarantee. A wrapper `<div
 * data-region="app.overlays">` would be a region that overlays happen to be
 * near: `Dialog`, `Sheet` and `Menu` all portal to `getOverlayRoot()` directly,
 * so their panels would land as SIBLINGS of the marker, not inside it, and
 * `within(region).getByRole('dialog')` would find nothing. Marking the portal
 * root instead makes "every overlay in the app is inside `app.overlays`" true
 * by construction, for overlays this file has never heard of — including
 * `app.drawer`'s sheet, which nests inside this region exactly as it should.
 *
 * It also makes "render the region twice, get one overlay root" free rather
 * than defended: there is only ever one node to mark.
 *
 * ## `DeployControllerProvider` is a hard dependency, and says so
 *
 * `DeployOverlay` is at `App.tsx:220`, inside `DeployControllerProvider` —
 * which wraps the whole of `App`. Moving it here moves that requirement here:
 * `AppShell` must mount `Overlays` inside the provider. The provider cannot
 * move down into this region, because `DeployPanel` calls `start()` from the
 * Settings page and both must share one controller instance.
 *
 * The dependency is asserted, not assumed. `useDeployController()` degrades to
 * an inert `visible: false` controller outside the provider, so a
 * mis-nested `Overlays` would render nothing, forever, in silence — a deploy
 * would run with no progress overlay and no console output. `useRequireDeploy
 * Controller()` turns that into a mount-time throw instead (Task 16's wiring
 * fails its first test run rather than in production during an upgrade).
 *
 * ## What this region does NOT render
 *
 * **`Toaster` stays in `main.tsx`.** It sits at `main.tsx:25`, inside
 * `<BrowserRouter>` and — verified — OUTSIDE `<ErrorBoundary>`, which closes on
 * line 24. Pulling it in here would move it inside the boundary, so a crash
 * anywhere in the app tree would unmount the toaster along with the app: the
 * one surface left to report the failure would be taken down by it. It stays
 * where it is; being outside the boundary is the point of where it is.
 *
 * **`CommandResultOverlay` is not mounted here, and cannot be.** It is
 * session-scoped: it reads `commandResult` / `commands` /
 * `dismissCommandResult` off the session runtime, and
 * `useSessionRuntimeContext()` throws outside `SessionRuntimeProvider` — which
 * Task 12 deliberately scoped to `PageHost` (audit F2.3; `DesktopShell` used to
 * wrap the whole shell in it and remounted every region on each session
 * switch). A sibling region of `PageHost` is by definition outside that
 * provider, so hoisting the overlay here would mean either re-widening the
 * provider — undoing F2.3 — or reading a runtime that is never there and
 * silently never showing `/help` and `/context` results again.
 *
 * React ownership stays with the page; DOM ownership is this region's, and that
 * is the half that was actually in question. Because the overlay is a `Dialog`,
 * it portals to `getOverlayRoot()` like every other overlay and therefore
 * renders INSIDE `[data-region="app.overlays"]` wherever it is mounted from.
 * `Overlays.test.tsx` pins exactly that.
 *
 * ## Visibility is not this region's business
 *
 * Nothing here reads `useViewport().mode`. Kiosk refusing dialogs is Task 19's
 * change inside `ui/Dialog.tsx` — one rule, at the one place every dialog goes
 * through — and whether the region mounts at all is Task 16's matrix. A local
 * kiosk check would be a third owner of the same rule. (`PromptDock` and
 * `DrawerHost` refuse the same temptation for the same reason.)
 */
export function Overlays() {
  useRequireDeployController()

  React.useEffect(() => {
    const root = getOverlayRoot()
    marks += 1
    root.setAttribute('data-region', OVERLAY_REGION)
    return () => {
      marks -= 1
      // Only the last instance out unmarks the shared node.
      if (marks === 0) root.removeAttribute('data-region')
    }
  }, [])

  // Contributes nothing to layout: the overlay portals itself into the root
  // marked above, so `AppShell` can place this region anywhere in its tree.
  return <DeployOverlay />
}
