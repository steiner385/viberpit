import { describe, it, expect } from 'vitest'
import { flattenRows } from './transcriptRows'
import type { Turn as TurnType, RotationMarker } from '@/session/reducer'

const turn = (id: string): TurnType => ({ id, role: 'assistant', blocks: [] })
const rot = (afterTurnId: string | null): RotationMarker => ({ from: 'a', to: 'b', afterTurnId })

describe('flattenRows', () => {
  it('orders pre-turn rotations, then each turn followed by its anchored rotations', () => {
    const rows = flattenRows([turn('t1'), turn('t2')], [rot(null), rot('t1')])
    expect(rows.map((r) => r.kind)).toEqual(['rotation', 'turn', 'rotation', 'turn'])
    expect(rows.map((r) => r.key)).toEqual(['rot-top-0', 'turn-t1', 'rot-t1-0', 'turn-t2'])
  })

  it('surfaces orphan rotations (anchor turn missing) at the end', () => {
    const rows = flattenRows([turn('t1')], [rot('ghost')])
    expect(rows.map((r) => r.kind)).toEqual(['turn', 'rotation'])
    expect(rows[1].key).toBe('rot-orphan-0')
  })

  it('produces one row per turn when there are no rotations', () => {
    const rows = flattenRows([turn('t1'), turn('t2'), turn('t3')], [])
    expect(rows).toHaveLength(3)
    expect(rows.every((r) => r.kind === 'turn')).toBe(true)
  })
})
