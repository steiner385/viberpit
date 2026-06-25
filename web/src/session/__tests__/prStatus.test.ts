import { describe, it, expect } from 'vitest'
import { anyChecksFailing } from '../prStatus'
import type { PullRequestInfo } from '@/api/types'

const pr = (over: Partial<PullRequestInfo> = {}): PullRequestInfo => ({
  number: 1, url: 'https://github.com/o/r/pull/1',
  state: 'open', checks: 'passing', title: 'PR', deployed: false, ...over,
})

describe('anyChecksFailing', () => {
  it('is false for nullish / empty lists', () => {
    expect(anyChecksFailing(undefined)).toBe(false)
    expect(anyChecksFailing(null)).toBe(false)
    expect(anyChecksFailing([])).toBe(false)
  })

  it('is false when every PR is passing/pending/none', () => {
    expect(anyChecksFailing([pr({ checks: 'passing' }), pr({ checks: 'pending' }), pr({ checks: 'none' })])).toBe(false)
  })

  it('is true when at least one PR is failing', () => {
    expect(anyChecksFailing([pr({ checks: 'passing' }), pr({ checks: 'failing' })])).toBe(true)
  })

  it('is true for a draft PR with failing checks (still actionable)', () => {
    expect(anyChecksFailing([pr({ state: 'draft', checks: 'failing' })])).toBe(true)
  })

  it('ignores a closed PR with frozen failing checks (terminal, no action)', () => {
    expect(anyChecksFailing([pr({ state: 'closed', checks: 'failing' })])).toBe(false)
  })

  it('ignores a merged PR with frozen failing checks (terminal, no action)', () => {
    expect(anyChecksFailing([pr({ state: 'merged', checks: 'failing' })])).toBe(false)
  })
})
