// @vitest-environment node
/**
 * A turn id is NOT unique across roles.
 *
 * Claude mints a fresh id for every user echo (`claude_driver.py`), so one id
 * has always meant one turn meant one role. Codex does not: its app-server
 * reuses ONE native turn id for the user's prompt AND the agent's reply, so a
 * plain id lookup hands the agent's text to the USER's turn — where `Turn`
 * picks its renderer from `turn.role` and draws agent prose in a `UserBubble`,
 * pixel-identical to something the human typed.
 *
 * These lock the reducer's half of the fix: agent output never lands in a turn
 * whose role is `user`, whichever order the two arrive in.
 */

import { describe, it, expect } from 'vitest'
import { transcriptReducer, initialTranscript } from './reducer'
import type { TranscriptState, Turn } from './reducer'

function reduce(events: unknown[], initial = initialTranscript): TranscriptState {
  return events.reduce<TranscriptState>(
    (s, ev) => transcriptReducer(s, ev as Parameters<typeof transcriptReducer>[1]),
    initial,
  )
}

const texts = (t: Turn) => t.blocks.flatMap((b) => (b.kind === 'text' ? [b.text] : []))

describe('turn-id collision between the user prompt and the agent reply', () => {
  // The exact Codex shape: user, turnStart, textDelta — all on one turn id.
  const CODEX = [
    { type: 'user', turnId: 'native-1', text: 'are we completely done?' },
    { type: 'turnStart', turnId: 'native-1' },
    { type: 'textDelta', turnId: 'native-1', text: 'Not yet — the fix is implemented.' },
  ]

  it('files the agent reply in an ASSISTANT turn, not the user turn', () => {
    const s = reduce(CODEX)

    expect(s.turns).toHaveLength(2)
    expect(s.turns[0].role).toBe('user')
    expect(texts(s.turns[0])).toEqual(['are we completely done?'])
    expect(s.turns[1].role).toBe('assistant')
    expect(texts(s.turns[1])).toEqual(['Not yet — the fix is implemented.'])
  })

  it('gives the two turns distinct ids so neither can clobber the other', () => {
    const s = reduce(CODEX)
    expect(s.turns[0].id).not.toBe(s.turns[1].id)
  })

  it('keeps agent tool calls out of the user turn as well', () => {
    const s = reduce([
      ...CODEX,
      { type: 'toolCall', turnId: 'native-1', toolId: 'x1', name: 'Bash', toolKind: 'bash', input: {} },
    ])
    expect(s.turns[0].blocks.every((b) => b.kind !== 'tool')).toBe(true)
    expect(s.turns[1].blocks.some((b) => b.kind === 'tool')).toBe(true)
  })

  it('routes later deltas on the same id back to the SAME assistant turn', () => {
    const s = reduce([
      ...CODEX,
      { type: 'toolCall', turnId: 'native-1', toolId: 'x1', name: 'Bash', toolKind: 'bash', input: {} },
      { type: 'textDelta', turnId: 'native-1', text: 'Now it is.' },
    ])
    // One user turn + one assistant turn — no third turn fabricated per event.
    expect(s.turns).toHaveLength(2)
    expect(texts(s.turns[1])).toEqual(['Not yet — the fix is implemented.', 'Now it is.'])
  })

  it('does not let a follow-up prompt on the same id erase the agent reply', () => {
    // Codex drains a queued prompt into the turn that is already running, so a
    // second `user` event arrives for a turn id that already holds agent output.
    // The user upsert must rebuild only the USER turn.
    const s = reduce([
      ...CODEX,
      { type: 'user', turnId: 'native-1', text: 'continue' },
    ])
    const assistant = s.turns.filter((t) => t.role === 'assistant')
    expect(assistant).toHaveLength(1)
    expect(texts(assistant[0])).toEqual(['Not yet — the fix is implemented.'])
  })

  it('handles the reverse order — agent turn first, user echo after', () => {
    const s = reduce([
      { type: 'turnStart', turnId: 'native-2' },
      { type: 'textDelta', turnId: 'native-2', text: 'Working on it.' },
      { type: 'user', turnId: 'native-2', text: 'go' },
    ])
    const assistant = s.turns.filter((t) => t.role === 'assistant')
    const user = s.turns.filter((t) => t.role === 'user')
    expect(assistant).toHaveLength(1)
    expect(texts(assistant[0])).toEqual(['Working on it.'])
    expect(user).toHaveLength(1)
    expect(texts(user[0])).toEqual(['go'])
  })

  it('leaves the Claude shape (distinct ids per role) exactly as it was', () => {
    const s = reduce([
      { type: 'user', turnId: 'u-1', text: 'hi' },
      { type: 'turnStart', turnId: 'a-1' },
      { type: 'textDelta', turnId: 'a-1', text: 'hello' },
    ])
    expect(s.turns.map((t) => [t.id, t.role])).toEqual([
      ['u-1', 'user'],
      ['a-1', 'assistant'],
    ])
  })
})
