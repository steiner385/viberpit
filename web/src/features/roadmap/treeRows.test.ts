// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { MAP } from './__fixtures__/map'
import { flattenMap, toggleKey, capabilityKey, productKey, initiativeKey, setCollapsed } from './treeRows'

const EXPANDED = { collapsed: new Set<string>() }
const keys = (rows: ReturnType<typeof flattenMap>) => rows.map((r) => r.key)

describe('flattenMap', () => {
  it('walks product → capabilities (sorted as given) → initiatives under primary → pseudo rows', () => {
    expect(keys(flattenMap(MAP, EXPANDED, ''))).toEqual([
      productKey('kindash'),
      capabilityKey('kindash', 'billing'),
      capabilityKey('kindash', 'tasks'),
      capabilityKey('kindash', 'task-views'),
      initiativeKey('views'),
      initiativeKey('audit'),
      initiativeKey('audit-01'),
      initiativeKey('orphan'),
      'pseudo:kindash:awaiting-triage',
      productKey('viberpit'),
      capabilityKey('viberpit', 'sessions'),
    ])
  })

  it('assigns depth from the tree and marks expandable rows', () => {
    const rows = flattenMap(MAP, EXPANDED, '')
    const byKey = Object.fromEntries(rows.map((r) => [r.key, r]))
    expect(byKey[productKey('kindash')]).toMatchObject({ kind: 'product', depth: 0, hasChildren: true, expanded: true })
    expect(byKey[capabilityKey('kindash', 'tasks')]).toMatchObject({ depth: 1, hasChildren: true })
    expect(byKey[capabilityKey('kindash', 'task-views')]).toMatchObject({ depth: 2, hasChildren: true })
    expect(byKey[initiativeKey('views')]).toMatchObject({ kind: 'initiative', depth: 3 })
    // A program sits beside its capability's other rows (same depth as
    // `task-views`); its phase is nested one level deeper under it.
    expect(byKey[initiativeKey('audit')]).toMatchObject({ kind: 'initiative', depth: 2, hasChildren: true, expanded: true })
    expect(byKey[initiativeKey('audit-01')]).toMatchObject({ kind: 'initiative', depth: 3, hasChildren: false })
    expect(byKey[initiativeKey('orphan')]).toMatchObject({ kind: 'initiative', depth: 1 })
    expect(byKey['pseudo:kindash:awaiting-triage']).toMatchObject({ kind: 'pseudo', count: 2 })
    expect(byKey[capabilityKey('kindash', 'billing')]).toMatchObject({ hasChildren: false })
  })

  it('collapsing hides the subtree and nothing else', () => {
    const state = toggleKey(EXPANDED, capabilityKey('kindash', 'tasks'))
    expect(keys(flattenMap(MAP, state, ''))).toEqual([
      productKey('kindash'),
      capabilityKey('kindash', 'billing'),
      capabilityKey('kindash', 'tasks'),
      initiativeKey('orphan'),
      'pseudo:kindash:awaiting-triage',
      productKey('viberpit'),
      capabilityKey('viberpit', 'sessions'),
    ])
    expect(toggleKey(state, capabilityKey('kindash', 'tasks'))).toEqual(EXPANDED)
    expect(setCollapsed(EXPANDED, 'x', true).collapsed.has('x')).toBe(true)
    expect(setCollapsed(setCollapsed(EXPANDED, 'x', true), 'x', false).collapsed.has('x')).toBe(false)
  })

  it('a query keeps matches and their ancestors and ignores collapse', () => {
    const collapsed = toggleKey(EXPANDED, productKey('kindash'))
    expect(keys(flattenMap(MAP, collapsed, 'task view'))).toEqual([
      productKey('kindash'),
      capabilityKey('kindash', 'tasks'),
      capabilityKey('kindash', 'task-views'),
      initiativeKey('views'),
    ])
    expect(keys(flattenMap(MAP, EXPANDED, 'zzz'))).toEqual([])
  })

  it('a row force-expanded by a query reports itself expanded', () => {
    // Collapse it, then search INTO it: the fold is ignored, so `expanded` must
    // describe the rendered subtree — otherwise the row draws an "Expand"
    // chevron over children that are already on screen, and clicking it does
    // nothing (the query overrides the fold either way).
    const collapsed = toggleKey(EXPANDED, capabilityKey('kindash', 'tasks'))
    const byKey = Object.fromEntries(flattenMap(MAP, collapsed, 'task view').map((r) => [r.key, r]))
    expect(byKey[capabilityKey('kindash', 'tasks')]).toMatchObject({ expanded: true, hasChildren: true })
    expect(byKey[capabilityKey('kindash', 'task-views')]).toBeDefined()
    // A match with no rendered descendants claims neither children nor a fold.
    expect(flattenMap(MAP, EXPANDED, 'billing')).toContainEqual(
      expect.objectContaining({ key: capabilityKey('kindash', 'billing'), hasChildren: false }),
    )
  })
})

describe('flattenMap — program nesting', () => {
  it('emits a phase directly under its program at depth + 1', () => {
    const rows = flattenMap(MAP, EXPANDED, '')
    const k = keys(rows)
    expect(k.indexOf(initiativeKey('audit-01'))).toBe(k.indexOf(initiativeKey('audit')) + 1)
    const byKey = Object.fromEntries(rows.map((r) => [r.key, r]))
    expect(byKey[initiativeKey('audit')]).toMatchObject({ kind: 'initiative', hasChildren: true, expanded: true })
    expect(byKey[initiativeKey('audit-01')].depth).toBe(byKey[initiativeKey('audit')].depth + 1)
  })

  it('collapsing a program hides its phases only', () => {
    const state = toggleKey(EXPANDED, initiativeKey('audit'))
    const k = keys(flattenMap(MAP, state, ''))
    expect(k).toContain(initiativeKey('audit'))
    expect(k).not.toContain(initiativeKey('audit-01'))
    expect(k).toContain(initiativeKey('views'))
  })

  it('a search that hits a phase keeps its program row', () => {
    const k = keys(flattenMap(MAP, EXPANDED, 'restore'))
    expect(k).toEqual(expect.arrayContaining([initiativeKey('audit'), initiativeKey('audit-01')]))
  })
})
