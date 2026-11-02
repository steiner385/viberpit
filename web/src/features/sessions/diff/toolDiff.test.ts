import { describe, it, expect } from 'vitest'
import { buildLines } from '@/lib/toolContent'
import { toolInputToFileDiff } from './toolDiff'

describe('toolInputToFileDiff', () => {
  it('gives an Edit the same lines buildLines produces, as one hunk', () => {
    const input = { file_path: '/repo/src/a.ts', old_string: 'a\nb', new_string: 'a\nc\nd' }
    const diff = toolInputToFileDiff(input)!
    expect(diff.hunks).toHaveLength(1)
    const got = diff.hunks[0].lines.map((l) => ({ prefix: l.kind === 'del' ? '-' : '+', text: l.text }))
    expect(got).toEqual(buildLines(input))
    expect(diff.additions).toBe(3)
    expect(diff.deletions).toBe(2)
  })

  it('takes the path from file_path so highlighting can pick a language', () => {
    expect(toolInputToFileDiff({ file_path: '/repo/x.py', old_string: 'a', new_string: 'b' })!.path).toBe('/repo/x.py')
    expect(toolInputToFileDiff({ old_string: 'a', new_string: 'b' })!.path).toBe('')
  })

  it('never invents file line numbers — a tool call does not say where the edit landed', () => {
    const diff = toolInputToFileDiff({ old_string: 'a', new_string: 'b' })!
    for (const l of diff.hunks[0].lines) {
      expect(l.oldNo).toBeNull()
      expect(l.newNo).toBeNull()
    }
  })

  it('renders a Write as added lines', () => {
    const diff = toolInputToFileDiff({ file_path: 'n.md', content: 'x\ny' })!
    expect(diff.hunks[0].lines.map((l) => [l.kind, l.text])).toEqual([['add', 'x'], ['add', 'y']])
  })

  it('returns null for a Codex change list, which carries no text', () => {
    expect(toolInputToFileDiff({ changes: [{ path: 'a.py', kind: 'update' }] })).toBeNull()
    expect(toolInputToFileDiff({})).toBeNull()
  })
})
