/**
 * The post-turn summary card's inputs (D-11), as pure functions over the
 * transcript: which turns make up the last reply, which files that reply
 * changed (and by how much), and how the card names the outcome.
 *
 * The outcome labels are statusTier's — `statusPresentation` / `TIER_LABEL` —
 * so the card and the status pill can never disagree on a word. The one label
 * that is not a status is the mid-turn "Asked you" note, ported from the pinned
 * panel this card replaced. Nothing here says "Done": `done` means the TURN is
 * finished, not the objective.
 */
import type { Turn } from './reducer'
import { statusPresentation, TIER_LABEL, type StatusPresentation } from './statusTier'
import { toolInputToFileDiff, fileChanges } from '@/features/sessions/diff/toolDiff'

/** Every assistant turn since the last user turn — one logical reply, however many
 *  segments it streamed as. Empty when the transcript ends on a prompt. */
export function lastLogicalTurn(turns: readonly Turn[]): Turn[] {
  let start = turns.length
  while (start > 0 && turns[start - 1].role === 'assistant') start--
  return turns.slice(start)
}

/** A path the reply changed. `plus`/`minus` only when every change to it was counted. */
export interface ChangedFile {
  path: string
  plus?: number
  minus?: number
}

/**
 * The distinct paths the given turns' edit/write tool calls touched, in first-touch
 * order. A Claude call names `input.file_path` and carries its text, so
 * `toolInputToFileDiff` counts it; a Codex file change names `changes: [{path}]`
 * with no text, so its paths stay uncounted. A call whose result came back failed
 * changed nothing and is skipped.
 */
export function changedFiles(turns: readonly Turn[]): ChangedFile[] {
  const byPath = new Map<string, { plus: number; minus: number; counted: boolean }>()
  const add = (path: string, counts: { additions: number; deletions: number } | null) => {
    const cur = byPath.get(path) ?? { plus: 0, minus: 0, counted: true }
    if (counts) {
      cur.plus += counts.additions
      cur.minus += counts.deletions
    } else {
      cur.counted = false
    }
    byPath.set(path, cur)
  }
  for (const turn of turns) {
    for (const block of turn.blocks) {
      if (block.kind !== 'tool' || (block.toolKind !== 'edit' && block.toolKind !== 'write')) continue
      if (block.result && !block.result.ok) continue
      const { input } = block
      if (typeof input.file_path === 'string' && input.file_path) {
        add(input.file_path, toolInputToFileDiff(input))
      } else {
        for (const change of fileChanges(input)) add(change.path, null)
      }
    }
  }
  return [...byPath].map(([path, c]) => (c.counted ? { path, plus: c.plus, minus: c.minus } : { path }))
}

/** The summed `+N −M` over `files`; null when any path is uncounted, so the card
 *  shows the file count alone rather than a total that silently omits a file. */
export function diffTotals(files: readonly ChangedFile[]): { additions: number; deletions: number } | null {
  let additions = 0
  let deletions = 0
  for (const f of files) {
    if (f.plus === undefined || f.minus === undefined) return null
    additions += f.plus
    deletions += f.minus
  }
  return { additions, deletions }
}

/** How the card heads itself. `asked` is the mid-turn note; `outcome` a finished turn. */
export interface SummaryOutcome {
  mode: 'outcome' | 'asked'
  /** The rich status this outcome reads (`handed_back` for the mid-turn note). */
  status: string
  label: string
  presentation: StatusPresentation
  /** The session's own hand-back sentence (handed_back, or the mid-turn ask). */
  reason: string | null
  /** The server's "likely complete" reading of a hand-back. */
  suspected: string | null
}

/** Statuses that end a turn with a summary. `waiting_*` and `ready_review` own the
 *  moment with their own cards; `idle`, `queued` and the rest have no turn to sum up. */
const OUTCOME_STATUSES = new Set(['complete', 'ended', 'handed_back', 'done', 'needs_attention', 'limited'])

/**
 * The card's heading for a session, or null for no card.
 *
 * - `busy` with a hand-back reason: the "Asked you" note (#2647 — the session
 *   called hand_back and kept working; the ask is live now). Never "Likely
 *   complete", which judges a finished turn.
 * - `complete` / `ended`: the tier label, "Complete".
 * - `handed_back`: "Handed back" + the reason, and the "likely complete" hint.
 *   Both are read only here: a resumed session keeps the stale row values, the
 *   same gate the list card applies.
 * - `done`, `needs_attention`, `limited`: their pill label.
 */
export function summaryOutcome(
  status: string,
  handBackReason: string | null | undefined,
  suspectedCompleteReason: string | null | undefined,
  busy: boolean,
): SummaryOutcome | null {
  const reason = handBackReason?.trim() || null
  if (busy) {
    if (!reason) return null
    return {
      mode: 'asked', status: 'handed_back', label: 'Asked you',
      presentation: statusPresentation('handed_back'), reason, suspected: null,
    }
  }
  if (!OUTCOME_STATUSES.has(status)) return null
  const presentation = statusPresentation(status)
  const handedBack = status === 'handed_back'
  return {
    mode: 'outcome',
    status,
    label: presentation.tier === 'complete' ? TIER_LABEL.complete : presentation.label,
    presentation,
    reason: handedBack ? reason : null,
    suspected: handedBack ? suspectedCompleteReason?.trim() || null : null,
  }
}
