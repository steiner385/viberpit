/**
 * The lifecycle of a prompt between "the user hit Enter" and "the agent's turn
 * started" — the window that used to render as nothing at all.
 *
 * A prompt can sit in that window for 30 s–2 min, almost all of it inside the
 * edge's `_ensure_live` (waking a detached session before the text can reach the
 * driver at all). Collapsing that into a blank transcript left the user unable to
 * tell a received prompt from a dropped one.
 *
 * ONE module owns the vocabulary, its ordering, and its labels. The status
 * vocabulary next door (statusTier.ts) has a long comment about what happened the
 * last time this kind of list lived in two places and drifted; this is that lesson
 * applied up front.
 */

export type PromptStage =
  /** Held in the browser: socket closed or a deploy drain is pending. Flushes on reconnect. */
  | 'buffered'
  /** The frame left the browser. Nothing has confirmed it yet. */
  | 'sent'
  /** The edge acked receipt — the prompt is definitively on the server. */
  | 'received'
  /** The edge acked receipt AND has to wake a detached session first. The long one. */
  | 'resuming'
  /** Handed to the driver. The agent's turn starts next (or queues behind a live one). */
  | 'delivered'
  /** No ack inside PROMPT_ACK_TIMEOUT_MS. We cannot say it landed. */
  | 'stalled'

/**
 * Monotonic ordering for FORWARD progress. Acks and the SDK's own echo race each
 * other over the same socket, so a late `received` must never demote a bubble that
 * already reached `delivered`.
 *
 * A prompt can still legitimately move BACKWARDS — a `promptRejected` returns it to
 * the browser's hold queue for a later retry. That is not an ack, so it does not go
 * through `advancesStage`; it is set outright (reducer `_promptHeld`).
 */
const RANK: Record<PromptStage, number> = {
  buffered: 0, sent: 1, received: 2, resuming: 3, delivered: 4, stalled: 5,
}

/** True when `next` is a forward move from `cur`. Equal stages are not advances. */
export function advancesStage(cur: PromptStage, next: PromptStage): boolean {
  return RANK[next] > RANK[cur]
}

/**
 * The line shown under a pending bubble. Deliberately names the SERVER-side step
 * rather than a generic "sending" for every stage: "did my prompt reach the
 * server?" is the actual question being answered, and "Waking session…" is what
 * makes a 90-second wait legible instead of alarming.
 */
export const STAGE_LABEL: Record<PromptStage, string> = {
  buffered: 'Held — waiting to reconnect…',
  sent: 'Sending…',
  received: 'Received',
  resuming: 'Received — waking session…',
  delivered: 'Delivered — starting…',
  stalled: 'No response from the server',
}

/** Compact form for the session-list pill, where there is no room for a sentence. */
export const STAGE_PILL_LABEL: Record<PromptStage, string> = {
  buffered: 'Prompt held',
  sent: 'Sending…',
  received: 'Sent — starting…',
  resuming: 'Sent — waking…',
  delivered: 'Sent — starting…',
  stalled: 'Prompt unacknowledged',
}

/** Stages that mean the prompt is still on its way (i.e. keep showing a spinner). */
export function isInFlight(stage: PromptStage): boolean {
  return stage !== 'stalled'
}

/** Only a prompt we put ON THE WIRE and heard nothing about can stall. A held
 *  prompt is explained already, and every later stage has been acked. */
export function canStall(stage: PromptStage): boolean {
  return stage === 'sent'
}

/**
 * How long to wait for the edge's first ack before admitting we do not know.
 * Generous relative to a healthy ack (single-digit ms on a tailnet) — this fires
 * only when something is genuinely wrong, never on a slow session resume, which
 * acks FIRST and then takes its time.
 */
export const PROMPT_ACK_TIMEOUT_MS = 8000
