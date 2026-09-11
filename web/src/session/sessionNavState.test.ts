// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { readerRouteForPath, listScopeForPath } from './sessionNavState'

describe('readerRouteForPath', () => {
  it.each([
    ['/sessions/archived/abc', 'archived'],
    ['/sessions/abc/agents/toolu_1', 'subagent'],
    ['/sessions/background/abc/agents/toolu_1', 'subagent'],
  ] as const)('%s → %s', (path, expected) => {
    expect(readerRouteForPath(path)).toBe(expected)
  })

  it.each([
    '/sessions',
    '/sessions/abc',
    '/sessions/archived',          // the archived LIST is not a reader
    '/sessions/background/abc',
    '/pipeline',
    '/archived/abc',               // legacy path: redirected before it ever renders
  ])('%s → null', (path) => {
    expect(readerRouteForPath(path)).toBeNull()
  })

  it('agrees with listScopeForPath about which scope a reader is in', () => {
    expect(listScopeForPath('/sessions/archived/abc')).toBe('archived')
    expect(listScopeForPath('/sessions/background/abc/agents/x')).toBe('background')
  })
})
