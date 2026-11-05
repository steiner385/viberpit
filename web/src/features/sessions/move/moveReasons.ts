// "Move to host…" (D-17): plain language for every `MOVE_*` reason code the server
// can return (`server/sessions/session_move.py`; contract in the D-17 task-1 report).
// THE one map: the dialog's refusal line, its per-host option hints and its outcome
// lines all read it. It ships in the lazy dialog chunk; the few strings the entry
// bundle needs (chip, menu) live in `@/api/sessionMove`. An unknown code falls back to the server's own message, then to
// the code itself, so a new refusal is still SHOWN rather than swallowed.
import { CODEX_STAYS_COPY } from '@/api/sessionMove'

/** The exact copy the owner ruled on (revised D1): consent cannot fix a public repo. */
export const PUBLIC_REPO_COPY = 'This repository is public — commit or stash your changes before moving'
/** D3: a running turn is never moved. */
export const MID_TURN_COPY = 'Wait for the turn to finish or Interrupt first'
/** The consent checkbox (D1) — exact copy. */
export const CARRY_DIRTY_COPY =
  'Carry uncommitted changes (they are pushed to a hidden ref on GitHub and deleted after the move)'

export const MOVE_REASON_COPY: Record<string, string> = {
  MOVE_DISABLED: 'Moving sessions is turned off on this server.',
  MOVE_NO_FLEET: 'This deployment has only one host, so there is nowhere to move to.',
  MOVE_NOT_FOUND: 'This session no longer exists.',
  MOVE_ENGINE_UNSUPPORTED: `${CODEX_STAYS_COPY}, so this session cannot move.`,
  MOVE_ASSISTANT: 'The assistant session always runs on the primary host.',
  MOVE_NATIVE_DISPLAY: "This session renders on its host's own screen, so it cannot move.",
  MOVE_IN_FLIGHT: 'A move is already running for this session.',
  MOVE_INACTIVE: 'Archived or ended sessions cannot move.',
  MOVE_NOT_STARTED: 'This session has not started yet (queued or awaiting approval).',
  MOVE_MID_TURN: `${MID_TURN_COPY}.`,
  MOVE_AWAITING_ANSWER: 'Answer the open question, permission or plan card first — it cannot be carried across a move.',
  MOVE_HAS_CHILDREN: 'This session is steering live child sessions; finish or stop them first.',
  MOVE_IS_CHILD: 'This session is steered by a live parent session, so it stays with it.',
  MOVE_NOT_MANAGED_WORKTREE: 'Only sessions in a managed worktree can move (not scratch or legacy worktrees).',
  MOVE_UNKNOWN_HOST: 'That host is not in the fleet.',
  MOVE_SAME_HOST: 'The session is already on that host.',
  MOVE_TARGET_DISABLED: 'That host is disabled.',
  MOVE_TARGET_UNHEALTHY: 'That host is not healthy right now.',
  MOVE_TARGET_ENGINE: "That host can't run this session's agent.",
  MOVE_TARGET_PLATFORM: "That host's platform can't take this session.",
  MOVE_TARGET_FULL: 'That host is at its session limit.',
  MOVE_TARGET_UNAVAILABLE: 'That host is not reachable right now.',
  MOVE_DIRTY_NEEDS_CONSENT: 'The worktree has uncommitted changes — tick the box to carry them.',
  MOVE_DIRTY_PUBLIC_REPO: PUBLIC_REPO_COPY,
  MOVE_BACKGROUND_NEEDS_CONFIRM: 'Confirm that the background tasks below may stop.',
  MOVE_SOURCE_UNREACHABLE: "The session's current host did not answer. Try again in a moment.",
  MOVE_EXPORT_FAILED: 'The worktree could not be pushed for the move.',
  MOVE_RESTAMP_FAILED: 'The move could not be recorded.',
  MOVE_IMPORT_FAILED: 'The target host could not fetch the worktree.',
  MOVE_TRANSCRIPT_STALE: "The target host could not see the session's latest transcript yet.",
  MOVE_RESUME_FAILED: 'The session did not start on the target host.',
  MOVE_LEASE_HELD: 'Another host holds this session.',
  MOVE_PATH_MISMATCH: 'The target host would put the worktree at a different path.',
  MOVE_WORKTREE_MISSING: 'The worktree was missing at a step that needed it.',
  MOVE_NOT_STAMPED: 'The move was not recorded before resuming.',
  MOVE_FINALIZE_REFUSED: 'The old worktree was not cleaned up.',
  SESSION_CONTROL_HUMAN_ONLY: 'Only a person can move a session.',
}

/** The plain-language line for a reason code. */
export function moveReasonText(code: string | null | undefined, serverMessage?: string | null): string {
  if (code && MOVE_REASON_COPY[code]) return MOVE_REASON_COPY[code]
  if (serverMessage) return serverMessage
  return code ?? 'This session cannot move right now.'
}
