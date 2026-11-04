import type { Turn as TurnType, RotationMarker, RewindMarker } from '@/session/reducer'
import { statusTier } from '@/session/statusTier'
import { changedFiles, lastLogicalTurn, summaryOutcome, type ChangedFile, type SummaryOutcome } from '@/session/turnSummary'

/**
 * A single virtualizable row. Virtualizers operate on a flat index space, so we
 * flatten the (turns + interleaved rotation dividers + model-switch dividers)
 * tree into one ordered list with stable keys.
 *
 * Order, per turn: an optional model-switch divider (when this turn's model
 * differs from the previous model-bearing turn), then the turn, then the
 * rotation dividers anchored to it. Pre-turn rotations lead; orphan rotations
 * (anchor turn missing) trail.
 *
 * `model-switch` carries the raw model id in `toModel`; the renderer resolves it
 * to a display name via modelName() so this stays a pure, dependency-light
 * function (the real source of truth for transcript row composition).
 */
export type TranscriptRow =
  | { key: string; kind: 'turn'; turn: TurnType }
  | { key: string; kind: 'rotation'; rot: RotationMarker }
  | { key: string; kind: 'model-switch'; toModel: string }

/**
 * A row as actually rendered by the transcript scroll container: the semantic
 * transcript rows plus the head markers and the tail rows (the summary card, the
 * rewind divider). Markers are ordinary rows so they share the virtualizer's
 * coordinate system (no scroll-margin bookkeeping).
 */
export type RenderRow =
  | TranscriptRow
  | { key: string; kind: 'loading-older' }
  | { key: string; kind: 'begin' }
  | { key: string; kind: 'rewind'; marker: RewindMarker }
  | { key: string; kind: 'summary'; outcome: SummaryOutcome; files: ChangedFile[] }

/** The session facts the post-turn summary row (D-11) is decided from. */
export interface SummaryRowInputs {
  status: string
  handBackReason?: string | null
  suspectedCompleteReason?: string | null
}

export interface RenderRowOptions {
  /** True while an older-page fetch is in flight — shows the loading marker. */
  loadingOlder?: boolean
  /** Earliest turn index known to the server; 0 means we're at the very start. */
  earliestTurn?: number
  /** The latest rewind (D-9): its divider trails the transcript, where the cut turns were. */
  rewind?: RewindMarker | null
  /** The live session's status facts: the post-turn summary card (D-11) trails the
   *  last turn. Absent in read-only views, which never show it. */
  summary?: SummaryRowInputs | null
}

/**
 * Compose the full ordered render list: head markers first, then the flattened
 * transcript. This is the single source of truth for which rows exist; the
 * component owns only how each row renders and how the list scrolls.
 */
export function buildRenderRows(
  turns: TurnType[],
  rotations: RotationMarker[],
  { loadingOlder = false, earliestTurn, rewind, summary }: RenderRowOptions = {},
): RenderRow[] {
  const head: RenderRow[] = []
  if (loadingOlder) head.push({ key: 'loading-older', kind: 'loading-older' })
  // The beginning-of-session marker is only meaningful in the live view when the
  // server confirms we're at the very start (earliestTurn === 0). Read-only
  // views omit earliestTurn, so it never shows there.
  if (turns.length > 0 && earliestTurn === 0) head.push({ key: 'begin', kind: 'begin' })
  const rows: RenderRow[] = [...head, ...flattenRows(turns, rotations)]
  const summaryRow = summary ? buildSummaryRow(turns, summary) : null
  if (summaryRow) rows.push(summaryRow)
  if (rewind) rows.push({ key: `rewind-${rewind.rewindId}`, kind: 'rewind', marker: rewind })
  return rows
}

/**
 * The post-turn summary row: after the last turn, before the rewind divider.
 * Shown once the session is not busy and its last logical turn (every assistant
 * turn since the last prompt) has ended — none while the reply has not started.
 * While busy it is only the mid-turn "Asked you" note, which shows regardless.
 */
function buildSummaryRow(turns: TurnType[], s: SummaryRowInputs): RenderRow | null {
  if (turns.length === 0) return null
  const busy = statusTier(s.status) === 'busy'
  const outcome = summaryOutcome(s.status, s.handBackReason, s.suspectedCompleteReason, busy)
  if (!outcome) return null
  // Constant: the row is always the tail's, and a per-turn key would drop its
  // measured size (and remount the card) on every new turn.
  const key = 'summary'
  if (outcome.mode === 'asked') return { key, kind: 'summary', outcome, files: [] }
  const reply = lastLogicalTurn(turns)
  if (reply.length === 0) return null
  return { key, kind: 'summary', outcome, files: changedFiles(reply) }
}

export function flattenRows(turns: TurnType[], rotations: RotationMarker[]): TranscriptRow[] {
  const byTurn = new Map<string, RotationMarker[]>()
  const top: RotationMarker[] = []
  for (const r of rotations) {
    if (r.afterTurnId === null) {
      top.push(r)
      continue
    }
    const list = byTurn.get(r.afterTurnId) ?? []
    list.push(r)
    byTurn.set(r.afterTurnId, list)
  }
  const knownIds = new Set(turns.map((t) => t.id))

  const rows: TranscriptRow[] = []
  top.forEach((r, i) => rows.push({ key: `rot-top-${i}`, kind: 'rotation', rot: r }))
  turns.forEach((turn, idx) => {
    // Model-switch divider: show when this turn's model differs from the most
    // recent earlier turn that carried a model. Only model-bearing turns switch;
    // the first model-bearing turn has nothing to differ from, so shows none.
    let prevModel: string | null = null
    for (let i = idx - 1; i >= 0; i--) {
      if (turns[i].model) {
        prevModel = turns[i].model ?? null
        break
      }
    }
    if (turn.model && prevModel && turn.model !== prevModel) {
      rows.push({ key: `model-${turn.id}`, kind: 'model-switch', toModel: turn.model })
    }
    rows.push({ key: `turn-${turn.id}`, kind: 'turn', turn })
    ;(byTurn.get(turn.id) ?? []).forEach((r, i) =>
      rows.push({ key: `rot-${turn.id}-${i}`, kind: 'rotation', rot: r }),
    )
  })
  rotations
    .filter((r) => r.afterTurnId !== null && !knownIds.has(r.afterTurnId))
    .forEach((r, i) => rows.push({ key: `rot-orphan-${i}`, kind: 'rotation', rot: r }))
  return rows
}

/**
 * The nearest row strictly before (`prev`) or after (`next`) row `from` that is
 * one of the user's OWN prompts — a user turn with no `origin`. A turn with an
 * origin is a nudge nobody typed (autopilot, completion, the assistant), so the
 * Alt+↑ / Alt+↓ prompt jump skips it. Null past either end.
 */
export function ownPromptRowIndex(rows: readonly RenderRow[], from: number, dir: 'prev' | 'next'): number | null {
  const step = dir === 'next' ? 1 : -1
  for (let i = from + step; i >= 0 && i < rows.length; i += step) {
    const row = rows[i]
    if (row.kind === 'turn' && row.turn.role === 'user' && !row.turn.origin) return i
  }
  return null
}
