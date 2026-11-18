// `primaryRepoOf` is THE default-repo answer for every reader of a product's
// repos: the active primary, else the first active repo, never a retired one.
import { describe, it, expect } from 'vitest'
import { primaryRepoOf, type ProjectRepoInfo } from './pm'

const repo = (r: string, extra: Partial<ProjectRepoInfo> = {}): ProjectRepoInfo => ({
  id: r, repo: r, isPrimary: false, backfillDone: true, lastSyncedAt: null, ...extra,
})

describe('primaryRepoOf', () => {
  it('returns the active primary', () => {
    expect(primaryRepoOf({ repos: [repo('o/a'), repo('o/b', { isPrimary: true })] })).toBe('o/b')
  })

  it('skips a retired repo even when it still carries the primary flag', () => {
    const repos = [
      repo('o/old', { isPrimary: true, retiredAt: '2026-11-18T00:00:00Z' }),
      repo('o/new'),
    ]
    expect(primaryRepoOf({ repos })).toBe('o/new')
  })

  it('falls back to the first ACTIVE repo, not repos[0]', () => {
    const repos = [repo('o/old', { retiredAt: '2026-11-18T00:00:00Z' }), repo('o/b'), repo('o/c')]
    expect(primaryRepoOf({ repos })).toBe('o/b')
  })

  it('is null with no active repo, or no project', () => {
    expect(primaryRepoOf({ repos: [repo('o/old', { retiredAt: '2026-11-18T00:00:00Z' })] })).toBeNull()
    expect(primaryRepoOf({ repos: [] })).toBeNull()
    expect(primaryRepoOf(undefined)).toBeNull()
  })
})
