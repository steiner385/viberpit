import type { Turn } from './reducer'
import { turnMarkdown } from './turnMarkdown'

/**
 * Where a "Fork from here" (D-8) cuts the conversation — the body of
 * `POST /sessions/{id}/fork`, minus the per-click `requestId`.
 *
 * - `reply`: `atTurnId` is the assistant turn to keep, through its last entry.
 * - `message`: the child keeps the history through the reply BEFORE the user's
 *   message (`atTurnId`, or null when the message was the first) and its composer
 *   is prefilled with the message (`prefill`), ready to edit and resend.
 *
 * The server resolves the anchor from its own record (`atTurnId` is a turn id,
 * never a transcript uuid), so a turn the server cannot resolve is a 409, not a
 * wrong cut. The rules below are conservative on top of that: a target that
 * would silently drop an exchange is not offered.
 */
export interface ForkTarget {
  atTurnId: string | null
  mode: 'reply' | 'message'
  prefill?: string
}

/** The id the server keys a turn's anchor by: a steered turn's segments share
 *  the engine turn id, which is the one the anchor record holds. */
function serverTurnId(turn: Turn): string {
  return turn.engineTurnId ?? turn.id
}

function replyTarget(turn: Turn): ForkTarget | null {
  return turn.role === 'assistant' && turn.anchorUuid ? { atTurnId: serverTurnId(turn), mode: 'reply' } : null
}

/**
 * The fork target of `turn` (a main-thread turn of `turns`), or null when it has
 * none. An assistant turn needs its own anchor. A user turn needs its text and a
 * settled reply right before it — or, as the session's first message (nothing
 * before it and no older page unloaded: `earliestTurn === 0`), no reply at all.
 * An unanchored or missing previous reply (interrupted, mid-steer, a user turn
 * right before, an older page not loaded) offers nothing.
 */
export function forkTargetFor(turns: readonly Turn[], turn: Turn, earliestTurn: number): ForkTarget | null {
  if (turn.role === 'assistant') return replyTarget(turn)
  const idx = turns.findIndex((t) => t.id === turn.id)
  if (idx === -1 || turn.pending) return null
  const prefill = turnMarkdown(turn)
  if (prefill === null) return null
  if (idx === 0) return earliestTurn === 0 ? { atTurnId: null, mode: 'message', prefill } : null
  const prev = replyTarget(turns[idx - 1])
  return prev ? { atTurnId: prev.atTurnId, mode: 'message', prefill } : null
}

/** `/fork`: the most recent reply with an anchor, or null when there is none. */
export function latestReplyTarget(turns: readonly Turn[]): ForkTarget | null {
  for (let i = turns.length - 1; i >= 0; i--) {
    const target = replyTarget(turns[i])
    if (target) return target
  }
  return null
}
