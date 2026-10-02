// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { selectBackgroundWork } from '../selectors'
import type { Turn, BackgroundTaskState } from '../reducer'
import type { BackgroundWork } from '@/api/types'

function bashTurn(toolId: string, input: Record<string, unknown>, ts = 5000, name = 'Bash'): Turn {
  return { id: 't1', role: 'assistant', blocks: [{ kind: 'tool', id: 'b', toolId, name, toolKind: 'unknown', input, ts, result: { ok: true, payload: {} } }] }
}
const live = (over: Partial<BackgroundTaskState> = {}): BackgroundTaskState =>
  ({ taskId: 'bg1', description: 'pytest -q', taskType: 'local_bash', backgrounded: true, status: 'running', startedAt: 1000, ...over })
const snap: BackgroundWork = { items: [{ toolUseId: 'tu1', kind: 'task', name: 'Bash', description: 'pytest -q', startedAt: '2026-10-02T11:00:00Z' }], wakeup: true, wakeupAt: '2026-10-02T11:10:00Z' }

describe('selectBackgroundWork', () => {
  it('renders snapshot items as running rows and parses the wakeup', () => {
    const v = selectBackgroundWork([], {}, snap)
    expect(v.rows).toEqual([{ toolUseId: 'tu1', kind: 'shell', description: 'pytest -q', status: 'running', startedAt: Date.parse('2026-10-02T11:00:00Z'), live: false }])
    expect(v.wakeup).toBe(true)
    expect(v.wakeupAt).toBe(Date.parse('2026-10-02T11:10:00Z'))
  })
  it('drops a snapshot item the live stream saw end', () => {
    const v = selectBackgroundWork([], { tu1: live({ status: 'completed', endedAt: 2000 }) }, snap)
    expect(v.rows).toEqual([])
  })
  it('marks a snapshot item live when the stream knows it', () => {
    expect(selectBackgroundWork([], { tu1: live() }, snap).rows[0]).toMatchObject({ live: true, startedAt: 1000 })
  })
  it('adds a live task missing from the snapshot and reads kind/description from the block', () => {
    const turns = [bashTurn('tu2', { command: 'gh pr checks 1', description: 'watch CI' }, 7000, 'Monitor')]
    const v = selectBackgroundWork(turns, { tu2: live({ description: '', backgrounded: false }) }, null)
    expect(v.rows).toEqual([{ toolUseId: 'tu2', kind: 'monitor', description: 'watch CI', status: 'running', startedAt: 1000, live: true }])
  })
  it('hides a live task that was not launched in the background', () => {
    const turns = [bashTurn('tu3', { command: 'ls' })]
    expect(selectBackgroundWork(turns, { tu3: live({ backgrounded: false }) }, null).rows).toEqual([])
  })
  it('shows a live run_in_background Bash even when the notice said not backgrounded', () => {
    const turns = [bashTurn('tu4', { command: 'sleep 9', run_in_background: true })]
    expect(selectBackgroundWork(turns, { tu4: live({ backgrounded: false, description: '' }) }, null).rows[0]).toMatchObject({ kind: 'shell', description: 'sleep 9' })
  })
  it('maps task types and snapshot kinds', () => {
    const v = selectBackgroundWork([], { w: live({ taskType: 'local_workflow' }) }, { items: [{ toolUseId: 'c', kind: 'terminal', name: 'BackgroundTerminal', description: 'npm test', startedAt: null }], wakeup: false, wakeupAt: null })
    expect(v.rows.map((r) => r.kind).sort()).toEqual(['terminal', 'workflow'])
  })
  it('sorts newest first', () => {
    const v = selectBackgroundWork([], { a: live({ startedAt: 1 }), b: live({ startedAt: 9 }) }, null)
    expect(v.rows.map((r) => r.toolUseId)).toEqual(['b', 'a'])
  })
})
