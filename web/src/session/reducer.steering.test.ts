// @vitest-environment node
import { describe, expect, it } from 'vitest'
import { initialTranscript, transcriptReducer } from './reducer'
import type { SessionEvent } from './events'

const text = (s: ReturnType<typeof transcriptReducer>) => s.turns.flatMap((t) =>
  t.blocks.flatMap((b) => b.kind === 'text' ? [b.text] : []))

const start: SessionEvent[] = [
  { type: 'replayComplete' },
  { type: 'user', turnId: 'prompt-1', text: 'first' },
  { type: 'turnStart', turnId: 'native-1', turnIndex: 0 },
  { type: 'textDelta', turnId: 'native-1', text: 'before steering' },
]
const reduce = (events: Parameters<typeof transcriptReducer>[1][]) => events.reduce(transcriptReducer, initialTranscript)

describe('responses after steering within one engine turn', () => {
  it('puts subsequent output below the confirmed prompt, preserving earlier output', () => {
    const s = reduce([...start,
      { type: '_trackSentPrompt', text: 'follow up' },
      { type: 'user', turnId: 'prompt-2', text: 'follow up' },
      { type: 'textDelta', turnId: 'native-1', text: 'after steering' },
      { type: 'textDelta', turnId: 'native-1', text: ' continued' },
    ])
    expect(text(s)).toEqual(['first', 'before steering', 'follow up', 'after steering continued'])
    expect(new Set(s.turns.map((t) => t.id)).size).toBe(s.turns.length)
    expect(s.turns.filter((t) => t.ordinal !== undefined)).toHaveLength(1)
  })

  it('preserves repeated prompts from already-running drivers and saved history', () => {
    const s = reduce([...start,
      { type: 'user', turnId: 'native-1', srcUuid: 'steer-1', text: 'continue' },
      { type: 'textDelta', turnId: 'native-1', text: 'second reply' },
      { type: 'user', turnId: 'native-1', srcUuid: 'steer-2', text: 'continue' },
      { type: 'textDelta', turnId: 'native-1', text: 'third reply' },
    ])
    expect(text(s)).toEqual(['first', 'before steering', 'continue', 'second reply', 'continue', 'third reply'])
    expect(new Set(s.turns.map((t) => t.id)).size).toBe(s.turns.length)
  })

  it('does not put output below a prompt still waiting for delivery', () => {
    const s = reduce([...start,
      { type: '_trackSentPrompt', text: 'queued' },
      { type: 'textDelta', turnId: 'native-1', text: ' continued' },
    ])
    expect(text(s)).toEqual(['first', 'before steering continued', 'queued'])
  })

  it('keeps late tool results and duplicate calls attached to their original tool', () => {
    const call: SessionEvent = { type: 'toolCall', turnId: 'native-1', toolId: 'tool-1', name: 'Bash', toolKind: 'bash', input: {} }
    const s = reduce([...start, call,
      { type: 'user', turnId: 'prompt-2', text: 'follow up' },
      { type: 'textDelta', turnId: 'native-1', text: 'after steering' },
      call,
      { type: 'toolResult', toolId: 'tool-1', ok: true, payload: {} },
    ])
    const tools = s.turns.flatMap((t) => t.blocks.filter((b) => b.kind === 'tool'))
    expect(tools).toHaveLength(1)
    expect(tools[0].result?.ok).toBe(true)
    expect(text(s)).toEqual(['first', 'before steering', 'follow up', 'after steering'])
  })

  it('upgrades a re-supplied question in its original segment without duplicating it', () => {
    const question: SessionEvent = { type: 'question', turnId: 'native-1', questionId: 'q1',
      header: 'Choice', question: 'Which?', options: [], multiSelect: false }
    const s = reduce([...start, { type: 'replayReset' }, ...start.slice(1), question,
      { type: 'user', turnId: 'prompt-2', text: 'follow up' },
      { type: 'textDelta', turnId: 'native-1', text: 'after steering' },
      { type: 'replayComplete' }, question,
    ])
    const questions = s.turns.flatMap((t) => t.blocks.filter((b) => b.kind === 'question'))
    expect(questions).toHaveLength(1)
    expect(questions[0].live).toBe(true)
    expect(text(s)).toEqual(['first', 'before steering', 'follow up', 'after steering'])
  })

  it('uses the same chronology for subagent threads', () => {
    const s = reduce([...start.slice(1),
      { type: 'user', turnId: 'prompt-2', text: 'follow up' },
      { type: 'textDelta', turnId: 'native-1', text: 'after steering' },
    ].map((e) => ({ ...e, parentToolUseId: 'task-1' }) as SessionEvent))
    expect(s.turns).toEqual([])
    expect(text({ ...s, turns: s.subAgents['task-1'].turns })).toEqual([
      'first', 'before steering', 'follow up', 'after steering',
    ])
  })

  it('rebuilds the same segments on replay and overlapping history pages', () => {
    const history: SessionEvent[] = [...start.slice(1),
      { type: 'user', turnId: 'prompt-2', text: 'follow up' },
      { type: 'textDelta', turnId: 'native-1', text: 'after steering' },
      { type: 'turnEnd', turnId: 'native-1' },
    ]
    const s = reduce([{ type: 'replayReset' }, ...history, { type: 'replayComplete' },
      { type: '_prependHistory', events: history, earliestTurn: 0 }])
    expect(text(s)).toEqual(['first', 'before steering', 'follow up', 'after steering'])
    expect(new Set(s.turns.flatMap((t) => t.blocks.map((b) => b.id))).size).toBe(4)
  })

})
