import { describe, it, expect } from 'vitest'
import { flattenRows, buildRenderRows } from './transcriptRows'
import type { Turn as TurnType, RotationMarker } from '@/session/reducer'

const turn = (id: string, model?: string | null): TurnType => ({ id, role: 'assistant', blocks: [], model })
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

  it('emits a model-switch row before a turn whose model differs from the previous model-bearing turn', () => {
    const rows = flattenRows([turn('t1', 'claude-opus-4-8'), turn('t2', 'claude-haiku-4-5')], [])
    expect(rows.map((r) => r.kind)).toEqual(['turn', 'model-switch', 'turn'])
    const sw = rows[1]
    expect(sw.kind === 'model-switch' && sw.toModel).toBe('claude-haiku-4-5')
    expect(rows[1].key).toBe('model-t2')
  })

  it('emits no model-switch row when the model is unchanged', () => {
    const rows = flattenRows([turn('t1', 'claude-opus-4-8'), turn('t2', 'claude-opus-4-8')], [])
    expect(rows.some((r) => r.kind === 'model-switch')).toBe(false)
  })

  it('emits no model-switch row on the first model-bearing turn', () => {
    // t1 has no model, t2 is the first model-bearing turn: nothing to differ from.
    const rows = flattenRows([turn('t1', null), turn('t2', 'claude-opus-4-8')], [])
    expect(rows.some((r) => r.kind === 'model-switch')).toBe(false)
  })

  it('skips over model-less turns when finding the previous model', () => {
    // t3's model differs from t1 (the last model-bearing turn); t2 has no model.
    const rows = flattenRows(
      [turn('t1', 'claude-opus-4-8'), turn('t2', null), turn('t3', 'claude-haiku-4-5')],
      [],
    )
    const switches = rows.filter((r) => r.kind === 'model-switch')
    expect(switches).toHaveLength(1)
    expect(switches[0].kind === 'model-switch' && switches[0].toModel).toBe('claude-haiku-4-5')
  })
})

describe('buildRenderRows', () => {
  it('prepends the beginning-of-session marker only when earliestTurn is 0', () => {
    const at0 = buildRenderRows([turn('t1')], [], { earliestTurn: 0 })
    expect(at0[0].kind).toBe('begin')
    const mid = buildRenderRows([turn('t1')], [], { earliestTurn: 5 })
    expect(mid.some((r) => r.kind === 'begin')).toBe(false)
  })

  it('omits the beginning-of-session marker in read-only views (no earliestTurn)', () => {
    const rows = buildRenderRows([turn('t1')], [])
    expect(rows.some((r) => r.kind === 'begin')).toBe(false)
  })

  it('omits the beginning-of-session marker when there are no turns', () => {
    const rows = buildRenderRows([], [], { earliestTurn: 0 })
    expect(rows.some((r) => r.kind === 'begin')).toBe(false)
  })

  it('prepends the loading marker ahead of the beginning marker while paging', () => {
    const rows = buildRenderRows([turn('t1')], [], { earliestTurn: 0, loadingOlder: true })
    expect(rows.map((r) => r.kind)).toEqual(['loading-older', 'begin', 'turn'])
  })

  it('appends the flattened transcript after the head markers', () => {
    const rows = buildRenderRows([turn('t1'), turn('t2')], [rot('t1')], { earliestTurn: 0 })
    expect(rows.map((r) => r.kind)).toEqual(['begin', 'turn', 'rotation', 'turn'])
  })
})
