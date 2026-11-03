import type { Turn } from './reducer'
import { statusTier } from './statusTier'

/** The card-pending statuses: a permission, a plan approval, a question. */
const CARD_PENDING = new Set(['waiting_input', 'ready_review', 'waiting_question'])

/**
 * Would the server refuse a rewind now (`REWIND_BUSY`)? Exactly its test,
 * `claude_driver.rewind_busy()`: a turn starting or running (the busy tier), or a
 * permission card, plan approval or question waiting. Not the whole needs-you tier —
 * a hand-back or a pending model approval leaves the client idle, and rewinds.
 */
export function rewindBlocked(status: string): boolean {
  return statusTier(status) === 'busy' || CARD_PENDING.has(status)
}

/** A CLI transcript uuid: what a replayed user turn's id is (a live one's is a bare hex id). */
const CLI_UUID = /^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i

/**
 * The `toTurnId` a "Rewind to here" (D-9) on `turn` sends, or null when it has none.
 * Only a settled user prompt the server can resolve: a live one once the CLI echoed its
 * uuid (`promptUuid`), a replayed one by its id, which already is that uuid. An owner
 * prefix is not a prompt of this session's own.
 */
export function rewindTargetFor(turn: Turn): string | null {
  if (turn.role !== 'user' || turn.pending || turn.ownerPrefix) return null
  return turn.promptUuid || CLI_UUID.test(turn.id) ? turn.id : null
}

/**
 * Rewind & retry's prompts: each reply turn that carries an error block, mapped to the
 * latest user turn before it — the prompt that failed. A failed reply with no prompt
 * before it (the start of a paged-in window) is left out.
 */
export function failedPrompts(turns: readonly Turn[]): Map<string, Turn> {
  const out = new Map<string, Turn>()
  let prompt: Turn | null = null
  for (const t of turns) {
    if (t.role === 'user') prompt = t
    else if (prompt && t.blocks.some((b) => b.kind === 'error')) out.set(t.id, prompt)
  }
  return out
}

/** `/rewind`'s picker: the prompts with a target, newest first, at most 20. */
export function recentRewindTargets(turns: readonly Turn[]): Turn[] {
  return turns.filter((t) => rewindTargetFor(t) !== null).reverse().slice(0, 20)
}
