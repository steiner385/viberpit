import type { Turn as TurnType, RotationMarker } from '@/session/reducer'

/**
 * A single virtualizable row. Virtualizers operate on a flat index space, so we
 * flatten the (turns + interleaved rotation dividers) tree into one ordered list
 * with stable keys. Order: pre-turn rotations, then each turn followed by the
 * rotations anchored to it, then orphan rotations whose anchor turn is missing.
 */
export type TranscriptRow =
  | { key: string; kind: 'turn'; turn: TurnType }
  | { key: string; kind: 'rotation'; rot: RotationMarker }

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
  for (const turn of turns) {
    rows.push({ key: `turn-${turn.id}`, kind: 'turn', turn })
    ;(byTurn.get(turn.id) ?? []).forEach((r, i) =>
      rows.push({ key: `rot-${turn.id}-${i}`, kind: 'rotation', rot: r }),
    )
  }
  rotations
    .filter((r) => r.afterTurnId !== null && !knownIds.has(r.afterTurnId))
    .forEach((r, i) => rows.push({ key: `rot-orphan-${i}`, kind: 'rotation', rot: r }))
  return rows
}
