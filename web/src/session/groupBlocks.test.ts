// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { groupBlocks } from './groupBlocks'
import type { Block } from './reducer'

const text = (id: string): Block => ({ kind: 'text', id, text: 'hi' })
const tool = (id: string): Block => ({
  kind: 'tool', id, toolId: id, name: 'Read', toolKind: 'read', input: { file_path: `/f/${id}` },
})

describe('groupBlocks', () => {
  it('returns [] for empty input', () => {
    expect(groupBlocks([])).toEqual([])
  })

  it('passes a lone non-tool block through as a block item', () => {
    const items = groupBlocks([text('t1')])
    expect(items).toEqual([{ kind: 'block', block: text('t1') }])
  })

  it('coalesces consecutive tool blocks into one toolGroup', () => {
    const items = groupBlocks([tool('a'), tool('b'), tool('c')])
    expect(items).toHaveLength(1)
    expect(items[0]).toMatchObject({ kind: 'toolGroup' })
    expect(items[0].kind === 'toolGroup' && items[0].tools.map((t) => t.id)).toEqual(['a', 'b', 'c'])
  })

  it('splits tool runs around non-tool blocks, preserving order', () => {
    const items = groupBlocks([tool('a'), text('t1'), tool('b'), tool('c')])
    expect(items.map((i) => i.kind)).toEqual(['toolGroup', 'block', 'toolGroup'])
    expect(items[0].kind === 'toolGroup' && items[0].tools.map((t) => t.id)).toEqual(['a'])
    expect(items[2].kind === 'toolGroup' && items[2].tools.map((t) => t.id)).toEqual(['b', 'c'])
  })
})
