// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { lastLogicalTurn, changedFiles, diffTotals, summaryOutcome } from '../turnSummary'
import type { Block, Turn } from '../reducer'
import type { ToolKind } from '../events'

const user = (id: string): Turn => ({ id, role: 'user', blocks: [{ kind: 'text', id: `${id}:t`, text: 'hi' }] })
const asst = (id: string, blocks: Block[] = []): Turn => ({ id, role: 'assistant', blocks })
let n = 0
const tool = (toolKind: ToolKind, input: Record<string, unknown>, ok?: boolean): Block => {
  n += 1
  return {
    kind: 'tool', id: `b${n}`, toolId: `tool${n}`, name: toolKind, toolKind, input,
    ...(ok === undefined ? {} : { result: { ok, payload: {} } }),
  }
}
const edit = (file_path: string, old_string: string, new_string: string) => tool('edit', { file_path, old_string, new_string }, true)
const write = (file_path: string, content: string) => tool('write', { file_path, content }, true)
const codex = (...paths: string[]) => tool('edit', { changes: paths.map((path) => ({ path, kind: 'update' })) }, true)

describe('lastLogicalTurn', () => {
  it('is every assistant turn since the last user turn', () => {
    const turns = [user('u1'), asst('a1'), user('u2'), asst('a2'), asst('a3')]
    expect(lastLogicalTurn(turns).map((t) => t.id)).toEqual(['a2', 'a3'])
  })

  it('is empty when the transcript ends on a user turn (the reply has not started)', () => {
    expect(lastLogicalTurn([user('u1'), asst('a1'), user('u2')])).toEqual([])
  })

  it('is every turn when there is no user turn at all', () => {
    expect(lastLogicalTurn([asst('a1'), asst('a2')]).map((t) => t.id)).toEqual(['a1', 'a2'])
    expect(lastLogicalTurn([])).toEqual([])
  })
})

describe('changedFiles', () => {
  it('collects edit and write paths with their line counts (buildLines)', () => {
    const files = changedFiles([asst('a1', [edit('/r/a.ts', 'x', 'y\nz'), write('/r/b.ts', 'one\ntwo\nthree')])])
    expect(files).toEqual([
      { path: '/r/a.ts', plus: 2, minus: 1 },
      { path: '/r/b.ts', plus: 3, minus: 0 },
    ])
  })

  it('dedupes a path edited twice and sums its counts', () => {
    const files = changedFiles([
      asst('a1', [edit('/r/a.ts', 'x', 'y')]),
      asst('a2', [edit('/r/a.ts', 'p\nq', 'r')]),
    ])
    expect(files).toEqual([{ path: '/r/a.ts', plus: 2, minus: 3 }])
  })

  it('reads Codex file changes as paths with no counts', () => {
    expect(changedFiles([asst('a1', [codex('/r/c.ts', '/r/d.ts')])])).toEqual([
      { path: '/r/c.ts' }, { path: '/r/d.ts' },
    ])
  })

  it('leaves a path uncounted once any of its changes is uncounted', () => {
    const files = changedFiles([asst('a1', [edit('/r/a.ts', 'x', 'y'), codex('/r/a.ts')])])
    expect(files).toEqual([{ path: '/r/a.ts' }])
  })

  it('ignores reads, shell commands, and an edit that failed', () => {
    const files = changedFiles([asst('a1', [
      tool('read', { file_path: '/r/read.ts' }, true),
      tool('bash', { command: 'rm -rf build' }, true),
      tool('edit', { file_path: '/r/denied.ts', old_string: 'a', new_string: 'b' }, false),
    ])])
    expect(files).toEqual([])
  })

  it('reads a NotebookEdit\'s notebook_path as an uncounted path', () => {
    const nb = tool('edit', { notebook_path: '/r/n.ipynb', cell_id: 'c1', new_source: 'x = 1' }, true)
    expect(changedFiles([asst('a1', [nb])])).toEqual([{ path: '/r/n.ipynb' }])
  })

  it('sums a MultiEdit\'s edits[] the way a single Edit is counted', () => {
    const multi = tool('edit', {
      file_path: '/r/m.ts',
      edits: [{ old_string: 'a', new_string: 'b\nc' }, { old_string: 'd\ne\nf', new_string: 'g' }],
    }, true)
    expect(changedFiles([asst('a1', [multi])])).toEqual([{ path: '/r/m.ts', plus: 3, minus: 4 }])
  })

  it('counts an edit whose result has not arrived yet', () => {
    expect(changedFiles([asst('a1', [tool('edit', { file_path: '/r/a.ts', old_string: 'a', new_string: 'b' })])]))
      .toEqual([{ path: '/r/a.ts', plus: 1, minus: 1 }])
  })
})

describe('diffTotals', () => {
  it('sums the counts when every path is counted', () => {
    expect(diffTotals([{ path: 'a', plus: 2, minus: 1 }, { path: 'b', plus: 3, minus: 0 }]))
      .toEqual({ additions: 5, deletions: 1 })
  })

  it('is null when any path is uncounted (a Codex change), so only the file count shows', () => {
    expect(diffTotals([{ path: 'a', plus: 2, minus: 1 }, { path: 'b' }])).toBeNull()
  })
})

describe('summaryOutcome', () => {
  // Labels come from statusTier's presentation table; nothing here spells "Done".
  it.each([
    ['complete', 'Complete'],
    ['ended', 'Complete'],
    ['handed_back', 'Handed back'],
    ['done', 'Your turn'],
    ['needs_attention', 'Turn failed'],
    ['limited', 'Usage limit'],
  ])('labels %s as %s', (status, label) => {
    const o = summaryOutcome(status, null, null, false)
    expect(o?.label).toBe(label)
    expect(o?.mode).toBe('outcome')
    expect(o?.label).not.toMatch(/\bdone\b/i)
  })

  it.each(['waiting_input', 'waiting_question', 'waiting_external', 'ready_review', 'idle', 'queued', 'pending_approval'])(
    'shows no card for %s',
    (status) => {
      expect(summaryOutcome(status, 'a reason', 'a hint', false)).toBeNull()
    },
  )

  it('carries the hand-back reason only for handed_back (a resumed session keeps a stale row value)', () => {
    expect(summaryOutcome('handed_back', '  Need a decision  ', null, false)?.reason).toBe('Need a decision')
    expect(summaryOutcome('done', 'Need a decision', null, false)?.reason).toBeNull()
  })

  it('carries "likely complete" only beside a hand-back', () => {
    expect(summaryOutcome('handed_back', 'r', ' PR #9 merged ', false)?.suspected).toBe('PR #9 merged')
    expect(summaryOutcome('done', null, 'PR #9 merged', false)?.suspected).toBeNull()
    expect(summaryOutcome('handed_back', 'r', '   ', false)?.suspected).toBeNull()
  })

  it('is the "Asked you" note while busy with a hand-back reason, never with the hint', () => {
    const o = summaryOutcome('in_progress', 'Approve the plan?', 'PR #1 open', true)
    expect(o).toMatchObject({ mode: 'asked', label: 'Asked you', reason: 'Approve the plan?', suspected: null })
  })

  it('shows nothing while busy without a reason', () => {
    expect(summaryOutcome('in_progress', null, null, true)).toBeNull()
    expect(summaryOutcome('in_progress', '   ', null, true)).toBeNull()
  })
})

describe('turnSummary layering', () => {
  it('imports nothing from features/ — session/ sits below it', async () => {
    const { readFileSync } = await import('node:fs')
    const src = readFileSync(new URL('../turnSummary.ts', import.meta.url), 'utf8')
    expect(src).not.toMatch(/from '@\/features\//)
    // The guard can fail: the pattern does match a features import.
    expect("import { x } from '@/features/sessions/diff/toolDiff'").toMatch(/from '@\/features\//)
  })
})
