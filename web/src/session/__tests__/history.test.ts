// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { deriveHistory } from '../history'
import { OPTIMISTIC_USER_ID, type Turn } from '../reducer'

const userTurn = (id: string, text: string): Turn => ({
  id, role: 'user', blocks: [{ kind: 'text', id: `${id}:u`, text }],
})
const asstTurn = (id: string, text: string): Turn => ({
  id, role: 'assistant', blocks: [{ kind: 'text', id: `${id}:a`, text }],
})

describe('deriveHistory', () => {
  it('returns user prompts oldest→newest, ignoring assistant turns', () => {
    const turns = [userTurn('t1', 'first'), asstTurn('t1a', 'reply'), userTurn('t2', 'second')]
    expect(deriveHistory(turns)).toEqual(['first', 'second'])
  })

  it('excludes the optimistic seed turn', () => {
    const turns = [userTurn('t1', 'first'), { ...userTurn(OPTIMISTIC_USER_ID, 'in flight') }]
    expect(deriveHistory(turns)).toEqual(['first'])
  })

  it('skips empty-text user turns and keeps legitimate duplicates', () => {
    const turns = [userTurn('t1', 'dup'), userTurn('t2', ''), userTurn('t3', 'dup')]
    expect(deriveHistory(turns)).toEqual(['dup', 'dup'])
  })

  it('returns [] for no user turns', () => {
    expect(deriveHistory([asstTurn('a', 'x')])).toEqual([])
  })
})
