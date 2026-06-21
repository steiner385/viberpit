# Deep-linkable URLs for ccswitch web

**Date:** 2026-06-21
**Status:** Approved design — pending implementation plan
**Feature area:** 001-ccswitch-web (frontend `web/` + `server/app.py`)

## Problem

The web UI has no router. Navigation is a hand-rolled state machine in `web/src/App.tsx`
(`{ type: 'dashboard' | 'session' | 'review' }`) plus a local `activeTab` state in
`web/src/components/Dashboard.tsx`. Consequences:

- No view is addressable by URL. You cannot bookmark or share a link to a specific session,
  the archived tab, or a review screen.
- Refresh always returns to the default dashboard; all navigation state is lost.

The user wants every meaningful view to have its own URL so links can be saved and shared.

## Goals

- Every meaningful view is deep-linkable via a clean path URL: dashboard tabs, live session
  view, archived review view, and dialogs/modals.
- The browser location becomes the single source of truth for view/tab/dialog state.
- Refreshing or cold-loading a deep link lands on the right view.
- The existing keyboard-first UX (focus management, `originElementId`, `focusDispatchNonce`)
  keeps working.

## Non-goals

- No change to the WebSocket/session-attach mechanism, the session transcript reducer, or any
  REST API contract.
- No persistence of ephemeral focus data in shareable URLs.
- No general refactor of the frontend beyond what routing requires.

## Decisions

- **Router:** React Router v7 (`react-router-dom`), `<BrowserRouter>`. Standard, models nested
  routes + search params cleanly, lightweight to adopt. (Alternatives rejected: TanStack Router
  — heavier adoption for this app size; hash routing — uglier links, user chose clean paths.)
- **URL style:** clean paths (no hash). Requires a backend SPA fallback.
- **Sessions tab:** explicit at `/sessions`; `/` redirects to `/sessions`.
- **Dialogs:** modeled as a `?dialog=` search param layered on the current route, so a dialog
  overlays whatever view is behind it rather than being a separate page.

## URL map

| View | URL |
|---|---|
| Root | `/` → redirect to `/sessions` |
| Dashboard · Sessions tab | `/sessions` |
| Dashboard · Accounts tab | `/accounts` |
| Dashboard · Archived tab | `/archived` |
| Live session view | `/session/:sessionId` |
| Archived review view | `/review/:sessionId` |
| Dialog (overlay on any route) | `…?dialog=<name>` e.g. `/sessions?dialog=new`, `/accounts?dialog=settings`, `/session/:id?dialog=rename` |

Unknown/invalid `:sessionId` → redirect to `/sessions` (optionally a brief "not found" state).
Unknown top-level path → SPA renders, router catch-all redirects to `/sessions`.

## Architecture

### Frontend

- **`web/src/main.tsx`** — wrap `<App>` in `<BrowserRouter>`.
- **`web/src/App.tsx`** — remove the `useState<View>` state machine. Define routes:
  - `/` → `<Navigate to="/sessions" replace />`
  - `/sessions`, `/accounts`, `/archived` → `<Dashboard>` with the active tab derived from the path.
  - `/session/:sessionId` → `<SessionView>` (reads `sessionId` from `useParams`).
  - `/review/:sessionId` → `<ReviewView>` (reads `sessionId` from `useParams`).
  - catch-all `*` → `<Navigate to="/sessions" replace />`.
- **`web/src/components/Dashboard.tsx`** — `activeTab` derived from the current route instead of
  local `useState`; tab buttons navigate (`useNavigate`/`<Link>`) instead of `setState`.
- **Navigation actions** — `SessionCard` click, dispatch-bar Enter, Back buttons, Review/Unarchive
  buttons call `useNavigate()` (or render `<Link>`), replacing the `setView(...)` calls.
- **Dialogs** — open/closed state read from the `?dialog=` search param via `useSearchParams`.
  Opening sets the param; closing removes it. Each dialog component (New Session, Settings,
  Rename, etc.) keys off the param value.

### Keyboard-first UX preservation

`originElementId` and `focusDispatchNonce` are ephemeral focus hints, not shareable state. They
move into React Router location `state` (`navigate(path, { state: { originElementId } })`):

- In-app navigations carry the origin element so returning to the dashboard restores focus to the
  originating card.
- Cold deep-links (no location state) fall back to focusing a sensible default (e.g. the dispatch
  bar or the first session card).

This keeps focus restoration working without polluting shareable URLs with focus data.

### Backend

- **`web/dist`** is served by `StaticFiles(directory=..., html=True)` mounted at `/` in
  `server/app.py`. `html=True` serves `index.html` only for the root and real files, so a refresh
  on `/session/abc` returns **404**.
- Add an **SPA fallback**: a catch-all GET route (registered after the API/WS routers, guarded by
  the same `spa_dist.is_dir()` check) that returns `web/dist/index.html` for any path not starting
  with `/api` or `/ws`. Keep the dev/test guard so a missing build doesn't break startup.

## Data flow

1. User loads `/session/abc123` (typed, bookmarked, or shared).
2. Backend catch-all serves `index.html`; the SPA boots.
3. React Router matches `/session/:sessionId`, renders `<SessionView sessionId="abc123">`.
4. `SessionView` attaches to the session over WebSocket exactly as today.
5. Navigating away (Back) calls `navigate('/sessions')`; the URL and view update together.

## Error handling

- Invalid/missing `sessionId` (no such live/archived session): redirect to `/sessions`.
- Unknown path: router catch-all redirects to `/sessions`.
- Backend: catch-all must not shadow `/api/*` or `/ws/*`; those route normally and a missing build
  still yields a clean startup (guarded).

## Testing (TDD — constitution V)

Frontend:
- Deep-linking to each URL (`/sessions`, `/accounts`, `/archived`, `/session/:id`, `/review/:id`)
  renders the correct view/tab.
- In-app navigations update the URL (card click → `/session/:id`; Back → `/sessions`).
- `?dialog=` toggles the corresponding dialog open/closed.
- Unknown `sessionId` and unknown path redirect to `/sessions`.
- Focus: in-app navigation with location state restores origin focus; cold deep-link focuses the
  default.

Backend:
- Catch-all returns `index.html` for `/session/x` and other non-API paths.
- `/api/*` and `/ws/*` still route to their handlers (not shadowed).
- Missing `web/dist` (dev/test) does not break app startup.

## Affected files

- `web/package.json` — add `react-router-dom`.
- `web/src/main.tsx` — `<BrowserRouter>`.
- `web/src/App.tsx` — routes replace the view state machine.
- `web/src/components/Dashboard.tsx` — tab from route; navigation on tab change; dialogs from
  search param.
- `web/src/components/SessionCard.tsx`, `DispatchBar.tsx`, `SessionView.tsx`, `ReviewView.tsx` —
  navigation via router instead of `setView`/callbacks where applicable.
- `server/app.py` — SPA catch-all fallback.
- Test files alongside the above.
