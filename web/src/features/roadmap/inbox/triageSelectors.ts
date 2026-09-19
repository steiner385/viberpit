/**
 * Pure selectors over the triage queue.
 *
 * `GET /api/pm/triage` is WIDENED for exactly one surface: besides the pending
 * proposals it carries the PM Triage loop's fresh auto-accepts for their undo
 * window (spec 2026-09-12 product automation policy §8), so the inbox can render
 * them with an "auto-accepted · undo" chip. Every other reader of that list means
 * "awaiting a human decision" — the nav badge, the Inbox scope's count, the
 * selection the bulk bar may act on — and counting an already-decided row there
 * says "6 need you" about 5.
 *
 * So "a proposal is still pending" gets ONE definition, here, and every consumer
 * routes through it — no call site compares `status` itself. An inline copy at
 * each site is free to drift the moment the wire gains a fourth status, and the
 * surfaces then disagree about the size of one queue.
 *
 * Since spec 2026-09-18 §6 that queue has TWO card kinds: classification
 * proposals and decision proposals, listed together. These selectors are
 * generic over the row rather than typed to `TriageProposal` for exactly that
 * reason — "awaiting a human" stays one predicate over one queue, the nav badge
 * counts both kinds through it, and neither kind gets a parallel `isPending`.
 *
 * Scope note: `InitiativePage`'s `proposal?.status === 'pending'` is deliberately
 * NOT this — that is a `MaterializationProposal`, a different domain that merely
 * shares the word.
 */

/** The only thing either kind of row must carry to be asked this question. */
interface HasStatus { status: string }

/** Is this one proposal still awaiting a human? The ONE `'pending'` literal.
 *  Takes null/undefined because a detail pane renders before its row arrives. */
export function isPending<T extends HasStatus>(p: T | null | undefined): p is T {
  return p?.status === 'pending'
}

/**
 * The rows still awaiting a human decision, in the order they came in.
 *
 * `readonly` in: the inbox's own list is a `readonly` prop, the nav badge's is
 * mutable, and a mutable parameter would shut the inbox out of its own
 * selector — the one call site this exists for.
 */
export function pendingProposals<T extends HasStatus>(rows: readonly T[]): T[] {
  return rows.filter((p): p is T => isPending(p))
}
