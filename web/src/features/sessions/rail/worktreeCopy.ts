/**
 * What the Environment card's worktree Discard and Repair (D-19) say — THE one table
 * of their copy, on the lazy rail chunk.
 *
 * The codes are the server's (`server/sessions/worktree_controls.py`, plus the
 * person-only guard, `SESSION_MOVING` and `daemon_outdated`); a code missing here
 * falls back to the server's own message, then to the action's fallback.
 */
import { ApiRequestError, apiErrorCode } from '@/api/client'
import { sessionMovingMessage } from '@/api/sessionMove'

/** Binding (controller ruling, 2026-11-06): ignored files are never snapshotted. */
export const IGNORED_FILES_LOST =
  'Files git ignores (such as .env and build output) are not saved — they are deleted with the worktree.'

/** The 404 a discard or repair answers once the session has ended (or never existed). */
export const SESSION_ENDED = 'This session has already ended, so there is nothing left to change.'

export const DISCARD_FAILED_FALLBACK = "Couldn't discard the worktree"
export const REPAIR_FAILED_FALLBACK = "Couldn't repair the worktree"

export const WORKTREE_ERROR_MESSAGES: Readonly<Record<string, string>> = {
  SESSION_CONTROL_HUMAN_ONLY: 'Only a person can discard or repair a worktree',
  WORKTREE_CONFIRM_REQUIRED: 'Confirm the discard first: it ends the session and removes its worktree',
  WORKTREE_SESSION_BUSY: 'The session is busy — wait for the turn to finish, answer any open card, and let background tasks end',
  WORKTREE_NOT_MANAGED: "This session doesn't run in a worktree ViberPit created, so there is nothing to discard or repair",
  WORKTREE_MISSING: 'The worktree is already gone from this host — repair it, or archive the session',
  WORKTREE_SHARED: 'Another session runs in this worktree, so it cannot be removed',
  WORKTREE_SNAPSHOT_FAILED: "The worktree's changes couldn't be saved to a recovery ref, so nothing was discarded",
  WORKTREE_DISCARD_UNSAFE: "Git couldn't tell whether removing the worktree would lose commits, so nothing was discarded — try again, or push the branch first",
  WORKTREE_DISCARD_FAILED: "The session couldn't be ended — its changes are saved in the recovery refs below",
  WORKTREE_REMOVE_FAILED: "The session was archived, but its worktree couldn't be removed — its changes are saved in the recovery refs below",
  WORKTREE_PRESENT: 'The worktree is present — there is nothing to repair',
  WORKTREE_NOT_EMPTY: "The worktree's folder holds files that aren't a git worktree — repair never touches them",
  WORKTREE_REPAIR_FAILED: "The worktree couldn't be recreated: its branch isn't in this host's clone, or the session belongs to another host",
  SESSION_MOVING: 'The session is moving to another host — try again once it has landed',
  daemon_outdated: 'The session runner needs an update before it can do this — try after the next rollout',
}

/** `{detail: {code, message, recoveryRefs?, restoreCommand?}}` off a failed request. */
export function worktreeError(err: unknown, fallback: string): { text: string; refs: string[]; restore: string | null } {
  const detail = err instanceof ApiRequestError ? err.body?.detail : null
  const rec = detail && typeof detail === 'object' ? (detail as Record<string, unknown>) : {}
  const code = apiErrorCode(err)
  const ended = err instanceof ApiRequestError && err.status === 404
  const text = (ended ? SESSION_ENDED : null)
    ?? sessionMovingMessage(err)
    ?? (code !== null ? WORKTREE_ERROR_MESSAGES[code] : undefined)
    ?? (typeof rec.message === 'string' ? rec.message : null)
    ?? fallback
  const refs = Array.isArray(rec.recoveryRefs) ? rec.recoveryRefs.filter((r): r is string => typeof r === 'string') : []
  return { text, refs, restore: typeof rec.restoreCommand === 'string' ? rec.restoreCommand : null }
}

/** "3 uncommitted files" / "1 unpushed commit" / "couldn't be read". */
export function countLine(n: number | null, one: string, many: string): string {
  if (n === null) return `${many}: couldn't be read`
  return `${n} ${n === 1 ? one : many}`
}
