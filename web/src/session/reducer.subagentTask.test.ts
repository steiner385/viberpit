// @vitest-environment node
/**
 * Sub-agent task lifecycle (subAgentStarted / Progress / Ended) and sub-agent
 * permission/question cards duplicated into the sub-thread (spec 2026-09-11 §5.2).
 */
import { describe, it, expect } from 'vitest'
import { transcriptReducer, initialTranscript } from './reducer'
import type { TranscriptState } from './reducer'
import type { SessionEvent } from './events'

type Action = Parameters<typeof transcriptReducer>[1]
function reduce(events: Action[], initial: TranscriptState = initialTranscript): TranscriptState {
  return events.reduce((s, ev) => transcriptReducer(s, ev), initial)
}

const TID = 'toolu_A'
const started: SessionEvent = { type: 'subAgentStarted', parentToolUseId: TID, taskId: 'task1', description: 'sleep probe', agentType: 'Explore', backgrounded: false, depth: 1, ts: 1000 }
const progress: SessionEvent = { type: 'subAgentProgress', parentToolUseId: TID, taskId: 'task1', activity: 'Running Bash', totalTokens: 19591, toolUses: 1, durationMs: 4098, lastTool: 'Bash', ts: 2000 }
const ended: SessionEvent = { type: 'subAgentEnded', parentToolUseId: TID, taskId: 'task1', status: 'stopped', summary: '', ts: 3000 }
const endedRich: SessionEvent = { type: 'subAgentEnded', parentToolUseId: TID, taskId: 'task1', status: 'stopped', summary: 'sleep probe', totalTokens: 5, toolUses: 1, durationMs: 3, ts: 3001 }

describe('sub-agent task lifecycle', () => {
  it('started creates the thread with a running task', () => {
    const s = reduce([started])
    const t = s.subAgents[TID]
    expect(t.turns).toEqual([])
    expect(t.task).toMatchObject({ taskId: 'task1', description: 'sleep probe', agentType: 'Explore', status: 'running', startedAt: 1000, depth: 1 })
  })

  it('progress updates activity, usage and last tool', () => {
    const s = reduce([started, progress])
    expect(s.subAgents[TID].task).toMatchObject({ status: 'running', activity: 'Running Bash', totalTokens: 19591, toolUses: 1, durationMs: 4098, lastTool: 'Bash' })
  })

  it('progress before started still creates a running task with unknown identity', () => {
    const s = reduce([progress])
    expect(s.subAgents[TID].task).toMatchObject({ taskId: 'task1', status: 'running', activity: 'Running Bash', agentType: 'agent' })
  })

  it('ended is idempotent: a later ended keeps the terminal status and takes richer data', () => {
    const s1 = reduce([started, progress, ended])
    expect(s1.subAgents[TID].task).toMatchObject({ status: 'stopped', endedAt: 3000, summary: '' })
    const s2 = transcriptReducer(s1, endedRich)
    expect(s2.subAgents[TID].task).toMatchObject({ status: 'stopped', summary: 'sleep probe', totalTokens: 5, endedAt: 3000 })
    // a stray progress after the end never regresses the status
    const s3 = transcriptReducer(s2, progress)
    expect(s3.subAgents[TID].task?.status).toBe('stopped')
  })

  it('replayReset clears task state with the thread and carries it in the stale buffer', () => {
    const live = reduce([{ type: 'turnStart', turnId: 't1' }, { type: 'textDelta', turnId: 't1', text: 'x' }, { type: 'replayComplete' }, started])
    const s = transcriptReducer(live, { type: 'replayReset' })
    expect(s.subAgents).toEqual({})
    expect(s.staleSubAgents[TID].task?.status).toBe('running')
  })
})

describe('sub-agent cards in the sub-thread', () => {
  const perm: SessionEvent = { type: 'permissionRequest', permId: 'p1', turnId: 't1', tool: 'Write', toolKind: 'write', input: {}, plan: false, parentToolUseId: TID }

  it('a permission with parentToolUseId lands in BOTH the main turn and the sub-thread', () => {
    const s = reduce([{ type: 'turnStart', turnId: 't1' }, started, perm])
    expect(s.turns[0].blocks.some((b) => b.kind === 'permission' && b.permId === 'p1')).toBe(true)
    const sub = s.subAgents[TID].turns
    expect(sub).toHaveLength(1)
    expect(sub[0].blocks.some((b) => b.kind === 'permission' && b.permId === 'p1')).toBe(true)
  })

  it('a permission without parentToolUseId touches no sub-thread', () => {
    const s = reduce([{ type: 'turnStart', turnId: 't1' }, started, { ...perm, parentToolUseId: null }])
    expect(s.subAgents[TID].turns).toEqual([])
  })

  it('permissionResolved and the optimistic _resolvePermission clear both copies', () => {
    const s = reduce([{ type: 'turnStart', turnId: 't1' }, started, perm])
    for (const action of [{ type: 'permissionResolved', permId: 'p1', turnId: 't1', allowed: true } as Action, { type: '_resolvePermission', permId: 'p1' } as Action]) {
      const r = transcriptReducer(s, action)
      expect(r.turns[0].blocks.some((b) => b.kind === 'permission')).toBe(false)
      expect(r.subAgents[TID].turns[0].blocks.some((b) => b.kind === 'permission')).toBe(false)
    }
  })

  it('a question with parentToolUseId is duplicated and resolved in place in both', () => {
    const q: SessionEvent = { type: 'question', questionId: 'q1', turnId: 't1', header: 'Pick', question: 'Which?', options: [], multiSelect: false, parentToolUseId: TID }
    const s = reduce([{ type: 'turnStart', turnId: 't1' }, { type: 'replayComplete' }, started, q])
    const subBlock = s.subAgents[TID].turns[0].blocks.find((b) => b.kind === 'question')
    expect(subBlock).toMatchObject({ questionId: 'q1', live: true })
    const r = transcriptReducer(s, { type: 'questionResolved', questionId: 'q1', turnId: 't1', selected: ['A'] })
    expect(r.turns[0].blocks.find((b) => b.kind === 'question')).toMatchObject({ answered: ['A'], live: false })
    expect(r.subAgents[TID].turns[0].blocks.find((b) => b.kind === 'question')).toMatchObject({ answered: ['A'], live: false })
  })

  it('a mid-turn reattach re-emit upgrades live:false to live:true in both copies', () => {
    const q: SessionEvent = { type: 'question', questionId: 'q2', turnId: 't1', header: 'Pick', question: 'Which?', options: [], multiSelect: false, parentToolUseId: TID }
    // No replayComplete yet — both the main-thread and mirrored copies are created live:false.
    const s1 = reduce([{ type: 'turnStart', turnId: 't1' }, started, q])
    expect(s1.turns[0].blocks.find((b) => b.kind === 'question')).toMatchObject({ live: false })
    expect(s1.subAgents[TID].turns[0].blocks.find((b) => b.kind === 'question')).toMatchObject({ live: false })
    // replayComplete settles the session, then the server re-emits the still-pending
    // question (a mid-turn reattach's ring resupply) — this must upgrade BOTH copies.
    const s2 = transcriptReducer(s1, { type: 'replayComplete' })
    const s3 = transcriptReducer(s2, q)
    expect(s3.turns[0].blocks.find((b) => b.kind === 'question')).toMatchObject({ live: true })
    expect(s3.subAgents[TID].turns[0].blocks.find((b) => b.kind === 'question')).toMatchObject({ live: true })
  })
})
