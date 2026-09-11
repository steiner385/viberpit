/**
 * The navigation contract for opening a session: WHERE it lives (the paths
 * below) and WHAT is carried with it (the router `state` below).
 *
 * ## The paths
 *
 * The app spelled the same destination two ways: `App.tsx` navigated to
 * `/session/:id` (singular) while the shell's route table, `navDefs` and
 * `useOpenSessionId` all speak `/sessions/:id` (plural). Two spellings of one
 * destination is how a link silently starts landing on the catch-all, so both
 * the route PATTERN and the builder that produces a concrete path live here,
 * derived from the section path in `navDefs` — a detail route that drifted out
 * from under its own section would break `navDefForPath`, and with it the
 * session-runtime gate in `PageHost`. Deriving it makes that impossible rather
 * than merely unlikely.
 *
 * The singular `/session/:id` and `/review/:id` spellings survive only as
 * redirects in `shell/routes.tsx`, for bookmarks already in the wild — as do
 * the pre-scope-switch `/archived` and `/archived/:id` spellings (see
 * `LEGACY_ARCHIVED_PATH` below), from when Archived was its own nav section
 * rather than a scope of this one.
 *
 * ## The state
 *
 * When a session is created from the composer the user's text is carried to the
 * session route in `navigate(..., { state })` so the transcript can show it
 * immediately, before the SDK echoes it back (`useSessionRuntime`'s
 * `_seedUserPrompt`). It is deliberately NOT a search param: it is one-shot,
 * can be long, and must not survive a refresh or a shared link.
 *
 * The key lived as a bare string literal at the write site and as an inline
 * `as { initialPrompt?: string }` cast at the read site. Two halves of one
 * contract, neither checked against the other — rename one and the prompt
 * silently stops seeding. Both halves live here now, so there is one owner.
 *
 * `readInitialPrompt` narrows from `unknown` without a cast: router state is
 * genuinely arbitrary (a deep link arrives with `state === null`, and a
 * back/forward entry can carry state written by an older build), so it is
 * validated rather than asserted.
 */
import { matchPath } from 'react-router-dom'

import { NAV_BY_ID } from '@/shell/navDefs'

/** `/sessions` — the session list. */
export const SESSIONS_PATH = NAV_BY_ID.sessions.path

/**
 * `/sessions/archived` — the archived SCOPE of the sessions list.
 *
 * Archived was its own nav section until the scope switch landed. It is a
 * scope now, so its paths hang off `SESSIONS_PATH`: `navDefForPath` resolves
 * them to `sessions`, which is what keeps the list column beside the
 * read-only transcript on desktop. The constant keeps its name — every caller
 * (`useArchiveAdvance`, the automations page, the archived reader's Back link)
 * was already routing through it and needs no edit.
 */
export const ARCHIVED_PATH = `${SESSIONS_PATH}/archived`

/**
 * Route patterns for the session detail routes. The param is `id` (not
 * `sessionId`) because `useOpenSessionId` reads `useParams().id`.
 */
export const SESSION_ROUTE = `${SESSIONS_PATH}/:id`
export const SUBAGENT_ROUTE = `${SESSIONS_PATH}/:id/agents/:agentId`

/**
 * The archived transcript route. Its param is `archivedId`, NOT `id`, and that
 * is the entire mechanism keeping a live runtime off a dead session:
 * `useOpenSessionId` reads `useParams().id`, so an archived transcript simply
 * has no session id to find. Rename this param and `PageHost` will mount a
 * `SessionRuntimeProvider` over an archived transcript with nothing to fail.
 */
export const ARCHIVED_SESSION_ROUTE = `${ARCHIVED_PATH}/:archivedId`

/**
 * `/sessions/background` — the background SCOPE of the sessions list: quiet
 * maintenance sessions (`session/backgroundSessions.ts`). Unlike Archived, a
 * background session is a LIVE session the list merely keeps out of the way,
 * so its detail routes keep the live param name `id` and get a runtime — the
 * scope prefix exists only so the list column stays in Background while one
 * of its rows is open, instead of flipping to Active with nothing highlighted.
 */
export const BACKGROUND_PATH = `${SESSIONS_PATH}/background`
export const BACKGROUND_SESSION_ROUTE = `${BACKGROUND_PATH}/:id`
export const BACKGROUND_SUBAGENT_ROUTE = `${BACKGROUND_PATH}/:id/agents/:agentId`

/** Pre-scope-switch spellings. Bookmarks only — nothing in the app builds these. */
export const LEGACY_ARCHIVED_PATH = '/archived'
export const LEGACY_ARCHIVED_SESSION_ROUTE = '/archived/:sessionId'

/** The section path a LIVE session's routes hang off in *scope*. Archived is
 *  not a scope a live session can be opened in, so it maps to the live base. */
function liveBase(scope: ListScope | undefined): string {
  return scope === 'background' ? BACKGROUND_PATH : SESSIONS_PATH
}

/** The route of an open session, inside the list scope it was opened from. */
export function sessionPath(id: string, scope?: ListScope): string {
  return `${liveBase(scope)}/${id}`
}

/** The route of one sub-agent inside an open session, same scope rule. */
export function subAgentPath(id: string, agentId: string, scope?: ListScope): string {
  return `${liveBase(scope)}/${id}/agents/${agentId}`
}

/** The route of an archived session's read-only transcript (DEC-6). */
export function archivedSessionPath(id: string): string {
  return `${ARCHIVED_PATH}/${id}`
}

/**
 * Which half of the sessions list is on screen. Declared here, beside the paths
 * it is derived from, so `ScopeSegment`, `SessionListColumn` and `AppShell` all
 * name one type instead of each restating the union.
 */
export type ListScope = 'live' | 'background' | 'archived'

function under(pathname: string, base: string): boolean {
  return pathname === base || pathname.startsWith(`${base}/`)
}

/**
 * Which scope of the sessions list a route is in.
 *
 * Scope is derived from the URL and nowhere else — not a `?scope=` param (this
 * shell has a history of overlay params leaking onto unrelated URLs) and not
 * localStorage (a bookmark or the Back button must land on the scope it names).
 * This is the ONLY derivation: every component that needs the scope takes it as
 * a prop from the shell rather than re-testing the pathname itself.
 */
export function listScopeForPath(pathname: string): ListScope {
  if (under(pathname, ARCHIVED_PATH)) return 'archived'
  if (under(pathname, BACKGROUND_PATH)) return 'background'
  return 'live'
}

/** The two page shapes the shell treats as a Reader (page-migration §2.2). */
export type ReaderRoute = 'archived' | 'subagent'

/**
 * Whether the route is a Reader page, and which — the archived transcript or a
 * sub-agent drill-down. The shell hides `app.dock` behind a Reader (there is no
 * composer to sit above it) and this is the ONE derivation of that fact, beside
 * `listScopeForPath` and the route constants it reads. A Reader is recognised
 * by its ROUTE, never by an `OpenItem` kind: the sub-agent route must keep
 * resolving to `{ kind: 'session' }` so `useOpenSessionId` mounts the live
 * runtime the page reads.
 */
export function readerRouteForPath(pathname: string): ReaderRoute | null {
  if (matchPath(ARCHIVED_SESSION_ROUTE, pathname)) return 'archived'
  if (matchPath(SUBAGENT_ROUTE, pathname) || matchPath(BACKGROUND_SUBAGENT_ROUTE, pathname)) return 'subagent'
  return null
}

/** Router state understood by the session route. */
export interface SessionOpenState {
  initialPrompt?: string
}

/**
 * The `navigate()` options for opening a session, or `undefined` when there is
 * no prompt to seed — `navigate(path, undefined)` is the plain-navigation case,
 * so callers can pass the result through unconditionally.
 */
export function sessionOpenState(initialPrompt?: string): { state: SessionOpenState } | undefined {
  return initialPrompt ? { state: { initialPrompt } } : undefined
}

/** The seeded prompt carried by `useLocation().state`, or `undefined`. */
export function readInitialPrompt(state: unknown): string | undefined {
  if (typeof state !== 'object' || state === null) return undefined
  if (!('initialPrompt' in state)) return undefined
  const value = state.initialPrompt
  return typeof value === 'string' && value !== '' ? value : undefined
}
