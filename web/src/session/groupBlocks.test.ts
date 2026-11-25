// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { groupBlocks } from './groupBlocks'
import { initialTranscript, transcriptReducer } from './reducer'
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

describe('groupBlocks — share_artifact', () => {
  const artifact = (id: string): Block => ({
    kind: 'tool', id, toolId: id, name: 'mcp__viberpit__share_artifact', toolKind: 'app_action',
    input: { kind: 'url', value: 'https://x.y/pr/1', label: 'PR' },
  })

  it('lifts a share_artifact call out of the tool run as its own artifact item', () => {
    const items = groupBlocks([tool('a'), artifact('s'), tool('b')])
    expect(items.map((i) => i.kind)).toEqual(['toolGroup', 'artifact', 'toolGroup'])
    expect(items[1].kind === 'artifact' && items[1].block.id).toBe('s')
  })

  it('never folds an artifact into a toolGroup, even at the tail of a run', () => {
    const items = groupBlocks([tool('a'), tool('b'), artifact('s')])
    expect(items.map((i) => i.kind)).toEqual(['toolGroup', 'artifact'])
    expect(items[0].kind === 'toolGroup' && items[0].tools.map((t) => t.id)).toEqual(['a', 'b'])
  })
})

describe('groupBlocks — CLI user-facing tools', () => {
  const sendFile = (id: string): Block => ({
    kind: 'tool', id, toolId: id, name: 'SendUserFile', toolKind: 'unknown',
    input: { files: ['/home/x/repo/.rendertest/shot.png'], caption: 'Settings page', status: 'normal' },
  })

  it('lifts each user-facing tool out of the tool run as its own artifact item', () => {
    for (const name of ['SendUserFile', 'SendUserMessage', 'ReportFindings', 'PushNotification']) {
      const call: Block = { kind: 'tool', id: 'u', toolId: 'u', name, toolKind: 'unknown', input: {} }
      const items = groupBlocks([tool('a'), call, tool('b')])
      expect(items.map((i) => i.kind)).toEqual(['toolGroup', 'artifact', 'toolGroup'])
    }
  })

  it('leaves a look-alike name in the tool run', () => {
    const other: Block = { kind: 'tool', id: 'x', toolId: 'x', name: 'mcp__foo__SendUserFile', toolKind: 'mcp', input: {} }
    expect(groupBlocks([tool('a'), other]).map((i) => i.kind)).toEqual(['toolGroup'])
  })

  it('promotes a main-thread call; a sub-agent call stays inside its Task sub-thread', () => {
    let s = initialTranscript
    s = transcriptReducer(s, { type: 'turnStart', turnId: 't1' })
    s = transcriptReducer(s, { type: 'toolCall', turnId: 't1', toolId: 'main', name: 'SendUserFile', toolKind: 'unknown', input: (sendFile('main') as { input: Record<string, unknown> }).input })
    s = transcriptReducer(s, { type: 'toolCall', turnId: 'sa1', toolId: 'sub', name: 'SendUserFile', toolKind: 'unknown', input: {}, parentToolUseId: 'task1' })
    const main = groupBlocks(s.turns[0].blocks)
    expect(main.map((i) => i.kind)).toEqual(['artifact'])
    expect(main[0].kind === 'artifact' && main[0].block.id).toBe('main')
    expect(s.turns.flatMap((t) => t.blocks).some((b) => b.id === 'sub')).toBe(false)
    expect(s.subAgents.task1.turns[0].blocks[0]).toMatchObject({ kind: 'tool', id: 'sub' })
  })
})
