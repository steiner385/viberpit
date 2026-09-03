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
 * redirects in `shell/routes.tsx`, for bookmarks already in the wild.
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
/** `/archived` — the archived list. */
export const ARCHIVED_PATH = NAV_BY_ID.archived.path

/**
 * Route patterns for the session detail routes. The param is `id` (not
 * `sessionId`) because `useOpenSessionId` reads `useParams().id`; `/archived/:id`
 * reuses the same name deliberately, which is what makes `PageHost`'s
 * section-based gate load-bearing instead of defensive.
 */
export const SESSION_ROUTE = `${SESSIONS_PATH}/:id`
export const SUBAGENT_ROUTE = `${SESSIONS_PATH}/:id/agents/:agentId`
export const ARCHIVED_SESSION_ROUTE = `${ARCHIVED_PATH}/:id`

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
