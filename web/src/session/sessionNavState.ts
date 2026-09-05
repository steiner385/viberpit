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
 * (`useArchiveAdvance`, the automations page, `ReviewView`'s back button) was
 * already routing through it and needs no edit.
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

/** Pre-scope-switch spellings. Bookmarks only — nothing in the app builds these. */
export const LEGACY_ARCHIVED_PATH = '/archived'
export const LEGACY_ARCHIVED_SESSION_ROUTE = '/archived/:sessionId'

/** The route of an open session. */
export function sessionPath(id: string): string {
  return `${SESSIONS_PATH}/${id}`
}

/** The route of one sub-agent inside an open session. */
export function subAgentPath(id: string, agentId: string): string {
  return `${SESSIONS_PATH}/${id}/agents/${agentId}`
}

/** The route of an archived session's read-only transcript (DEC-6). */
export function archivedSessionPath(id: string): string {
  return `${ARCHIVED_PATH}/${id}`
}

/**
 * Which scope of the sessions list a route is in.
 *
 * Scope is derived from the URL and nowhere else — not a `?scope=` param (this
 * shell has a history of overlay params leaking onto unrelated URLs) and not
 * localStorage (a bookmark or the Back button must land on the scope it names).
 */
export function listScopeForPath(pathname: string): 'live' | 'archived' {
  return pathname === ARCHIVED_PATH || pathname.startsWith(`${ARCHIVED_PATH}/`)
    ? 'archived'
    : 'live'
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
