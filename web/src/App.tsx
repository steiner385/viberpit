/**
 * App — the router, and nothing else.
 *
 * ## What used to be here
 *
 * Until Task 17 this file was the layout fork: a `useMediaQuery` read of the
 * 1024px breakpoint chose between `DesktopShell` (which navigated with
 * `useState` and never touched the URL) and a sixteen-route `<Routes>` table
 * wrapped around `Dashboard`. (Spelling that query literally here, even in a
 * comment, re-trips `useViewport.test.tsx`'s guard: it greps the source text and
 * cannot tell code from prose — which is exactly why the guard caught the shell
 * fork in the first place.) Both shells are deleted; `AppShell` is the one
 * layout and `shell/routes.tsx` is the one route table, so the fork, the table,
 * the banner stack, the five route components and the `DeployOverlay` mount
 * that lived here all moved into the shell and its regions.
 *
 * ## `createBrowserRouter`, not `<BrowserRouter>`
 *
 * The data router is what lets `buildRoutes()` nest every page under a single
 * layout route, so `AppShell` stays mounted across navigation instead of
 * remounting per page. It replaces the `<BrowserRouter>` that used to wrap
 * `<App/>` in `main.tsx` — having both is an error, so that one is gone.
 *
 * ## Where `DeployControllerProvider` is
 *
 * `AppShell` mounts it (`AppShell.tsx`), because `Overlays` requires it and
 * `DeploySection` — rendered by the Settings page, INSIDE the router — must share
 * the same controller instance. Everything that consumes the controller is
 * therefore below the router, so a second provider out here would be a second
 * controller with no consumers: dead state polling the same query. See the
 * Task 17 report.
 */
import { RouterProvider, createBrowserRouter } from 'react-router-dom'
import { AppShell } from './shell/AppShell'
import { buildRoutes } from './shell/routes'

const router = createBrowserRouter(buildRoutes(<AppShell />))

export default function App() {
  return <RouterProvider router={router} />
}
