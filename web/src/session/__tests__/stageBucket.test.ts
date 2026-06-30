// web/src/session/__tests__/stageBucket.test.ts
import { describe, it, expect } from 'vitest'
import { stageBucketOf, STAGE_BUCKETS } from '@/session/stageBucket'
import type { PipelinePr, StageModel } from '@/api/types'

const model: StageModel = {
  repo: 'o/r',
  nodes: [
    { id: 'ci', label: 'CI', kind: 'ci', order: 0, source: 'derived' },
    { id: 'ready', label: 'Ready', kind: 'gate', order: 1, source: 'derived' },
    { id: 'queue', label: 'Queue', kind: 'queue', order: 2, source: 'derived' },
    { id: 'deploy:prod', label: 'prod', kind: 'deploy', order: 3, source: 'derived' },
    { id: 'merged', label: 'Merged', kind: 'merged', order: 4, source: 'derived' },
  ],
}
const pr = (o: Partial<PipelinePr>): PipelinePr => ({
  number: 1, url: 'u', state: 'open', checks: 'pending', title: 't', deployed: false,
  session: null, node_states: [], ...o,
} as unknown as PipelinePr)

describe('stageBucketOf', () => {
  it('uses the active node kind (gate→ready)', () => {
    expect(stageBucketOf(pr({ node_states: [
      { id: 'ci', state: 'done' }, { id: 'ready', state: 'active' }, { id: 'queue', state: 'pending' },
    ] }), model)).toBe('ready')
  })
  it('deploy node → deploy bucket', () => {
    expect(stageBucketOf(pr({ node_states: [
      { id: 'queue', state: 'done' }, { id: 'deploy:prod', state: 'active' },
    ] }), model)).toBe('deploy')
  })
  it('no active node → last done node (fully merged → merged)', () => {
    expect(stageBucketOf(pr({ node_states: [
      { id: 'ci', state: 'done' }, { id: 'merged', state: 'done' },
    ] }), model)).toBe('merged')
  })
  it('falls back to flat stage when no model/node_states', () => {
    expect(stageBucketOf(pr({ node_states: [], stage: 'queue' } as Partial<PipelinePr>), undefined)).toBe('queue')
    expect(stageBucketOf(pr({ node_states: [], stage: 'parked' } as Partial<PipelinePr>), undefined)).toBe('ci')
  })
  it('unknown → null', () => {
    expect(stageBucketOf(pr({ node_states: [], stage: '' } as Partial<PipelinePr>), undefined)).toBeNull()
  })
  it('exposes the fixed bucket order', () => {
    expect(STAGE_BUCKETS).toEqual(['ci', 'ready', 'queue', 'deploy', 'merged'])
  })
})
