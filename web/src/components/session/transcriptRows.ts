import type { Turn as TurnType, RotationMarker } from '@/session/reducer'

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
 * transcript rows plus the two head markers. Markers are ordinary rows so they
 * share the virtualizer's coordinate system (no scroll-margin bookkeeping).
 */
export type RenderRow =
  | TranscriptRow
  | { key: string; kind: 'loading-older' }
  | { key: string; kind: 'begin' }

export interface RenderRowOptions {
  /** True while an older-page fetch is in flight — shows the loading marker. */
  loadingOlder?: boolean
  /** Earliest turn index known to the server; 0 means we're at the very start. */
  earliestTurn?: number
}

/**
 * Compose the full ordered render list: head markers first, then the flattened
 * transcript. This is the single source of truth for which rows exist; the
 * component owns only how each row renders and how the list scrolls.
 */
export function buildRenderRows(
  turns: TurnType[],
  rotations: RotationMarker[],
  { loadingOlder = false, earliestTurn }: RenderRowOptions = {},
): RenderRow[] {
  const head: RenderRow[] = []
  if (loadingOlder) head.push({ key: 'loading-older', kind: 'loading-older' })
  // The beginning-of-session marker is only meaningful in the live view when the
  // server confirms we're at the very start (earliestTurn === 0). Read-only
  // views omit earliestTurn, so it never shows there.
  if (turns.length > 0 && earliestTurn === 0) head.push({ key: 'begin', kind: 'begin' })
  return [...head, ...flattenRows(turns, rotations)]
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
