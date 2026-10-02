// @vitest-environment node
/** Background shell/Monitor task lifecycle (#2635): live-only state keyed by the
 *  launching call's tool-use id, preserved across replayReset. */
import { describe, it, expect } from 'vitest'
import { transcriptReducer, initialTranscript } from './reducer'
import type { TranscriptState } from './reducer'
import type { SessionEvent } from './events'

type Action = Parameters<typeof transcriptReducer>[1]
const reduce = (events: Action[], initial: TranscriptState = initialTranscript) =>
  events.reduce((s, ev) => transcriptReducer(s, ev), initial)

const TID = 'toolu_B'
const started: SessionEvent = { type: 'backgroundTaskStarted', toolUseId: TID, taskId: 'bg1', description: 'pytest -q', taskType: 'local_bash', backgrounded: true, ts: 1000 }
const killed: SessionEvent = { type: 'backgroundTaskEnded', toolUseId: TID, taskId: 'bg1', status: 'stopped', ts: 3000 }
const stoppedAgain: SessionEvent = { type: 'backgroundTaskEnded', toolUseId: TID, taskId: 'bg1', status: 'completed', ts: 3001 }

describe('background task lifecycle', () => {
  it('started records a running task', () => {
    expect(reduce([started]).backgroundTasks[TID]).toEqual({
      taskId: 'bg1', description: 'pytest -q', taskType: 'local_bash', backgrounded: true,
      status: 'running', startedAt: 1000,
    })
  })
  it('ended is idempotent and never regresses the first terminal status', () => {
    const s = reduce([started, killed, stoppedAgain])
    expect(s.backgroundTasks[TID]).toMatchObject({ status: 'stopped', endedAt: 3000 })
  })
  it('ended before started still records the task', () => {
    expect(reduce([killed]).backgroundTasks[TID]).toMatchObject({ taskId: 'bg1', status: 'stopped', endedAt: 3000, backgrounded: false })
  })
  it('replayReset preserves live task state (the replay never re-emits task notices)', () => {
    const s = reduce([{ type: 'turnStart', turnId: 't1' }, { type: 'replayComplete' }, started, { type: 'replayReset' }])
    expect(s.backgroundTasks[TID]?.status).toBe('running')
  })
})
