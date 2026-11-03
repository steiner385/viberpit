// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { flattenRows, buildRenderRows, ownPromptRowIndex } from './transcriptRows'
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

describe('ownPromptRowIndex — the Alt+↑ / Alt+↓ target', () => {
  const user = (id: string, origin?: TurnType['origin']): TurnType =>
    ({ id, role: 'user', blocks: [], ...(origin ? { origin } : {}) })
  // begin marker, own, reply, nudge, reply, divider, own, reply
  const rows = buildRenderRows(
    [user('p1'), turn('a1', 'sonnet'), user('n1', { kind: 'completion' }), turn('a2', 'opus'), user('p2'), turn('a3')],
    [],
    { earliestTurn: 0 },
  )

  it('finds the nearest own prompt strictly before / after the given row', () => {
    // 0 begin · 1 p1 · 2 a1 · 3 n1 · 4 model-switch · 5 a2 · 6 p2 · 7 a3
    expect(rows.map((r) => r.kind)).toEqual(['begin', 'turn', 'turn', 'turn', 'model-switch', 'turn', 'turn', 'turn'])
    expect(ownPromptRowIndex(rows, 7, 'prev')).toBe(6)
    expect(ownPromptRowIndex(rows, 6, 'prev')).toBe(1)
    expect(ownPromptRowIndex(rows, 1, 'next')).toBe(6)
    expect(ownPromptRowIndex(rows, 0, 'next')).toBe(1)
  })

  it('skips nudges and every non-turn row', () => {
    expect(ownPromptRowIndex(rows, 5, 'prev')).toBe(1)
    expect(ownPromptRowIndex(rows, 2, 'next')).toBe(6)
  })

  it('returns null past either end', () => {
    expect(ownPromptRowIndex(rows, 1, 'prev')).toBeNull()
    expect(ownPromptRowIndex(rows, 6, 'next')).toBeNull()
    expect(ownPromptRowIndex([], 0, 'next')).toBeNull()
  })
})
