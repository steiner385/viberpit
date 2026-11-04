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
 * Rewind & retry's prompts: the LATEST user turn, mapped from each of its own replies
 * that carries an error block. A reply is the prompt's own only when it shares the
 * engine turn (`engineTurnId ?? id`) of the first reply after that prompt — which keeps
 * a steered `#after:` segment and drops an autonomous turn (a background task
 * notification has no prompt: retrying it would cut the previous prompt's good reply,
 * and the resend expires the undo). A fabricated turn sits wherever the tail was, so it
 * is never mapped and never breaks the run. Older prompts are left out: a retry there
 * would cut everything since.
 */
export function failedPrompts(turns: readonly Turn[]): Map<string, Turn> {
  const out = new Map<string, Turn>()
  let prompt: Turn | null = null
  let key: string | null = null
  for (const t of turns) {
    if (t.role === 'user') {
      prompt = t
      key = null
      out.clear()
      continue
    }
    if (t.fabricated || prompt === null) continue
    const k = t.engineTurnId ?? t.id
    // A targeted prompt (an engine gave a steering prompt a uuid) whose first reply
    // continues an earlier engine turn: a retry there would cut that earlier reply.
    // Untargeted steering prompts stay mapped; retryablePrompt refuses them anyway.
    if (key === null && t.engineTurnId != null && t.engineTurnId !== t.id
        && rewindTargetFor(prompt) !== null) { prompt = null; continue }
    if (key === null) key = k
    else if (k !== key) { prompt = null; continue }
    if (t.blocks.some((b) => b.kind === 'error')) out.set(t.id, prompt)
  }
  return out
}

/** Can Rewind & retry resend `prompt` exactly as it was sent? A typed (not a nudge's),
 *  targeted prompt with text and no images — a resend carries the text only. */
export function retryablePrompt(prompt: Turn): boolean {
  return rewindTargetFor(prompt) !== null && !prompt.origin
    && !prompt.blocks.some((b) => b.kind === 'image')
    && prompt.blocks.some((b) => b.kind === 'text' && b.text.trim() !== '')
}

/** `/rewind`'s picker: the prompts with a target, newest first, at most 20. */
export function recentRewindTargets(turns: readonly Turn[]): Turn[] {
  return turns.filter((t) => rewindTargetFor(t) !== null).reverse().slice(0, 20)
}
