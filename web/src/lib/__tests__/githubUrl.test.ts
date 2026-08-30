import { describe, it, expect } from 'vitest'
import { fileUrl, commitUrl, prUrl } from '../githubUrl'

describe('githubUrl', () => {
  it('pins a file blob at a ref, keeping tree slashes but encoding segments', () => {
    expect(fileUrl('steiner385/ccswitch', 'abc123', 'docs/sdlc/x/intent.md'))
      .toBe('https://github.com/steiner385/ccswitch/blob/abc123/docs/sdlc/x/intent.md')
  })

  it('encodes reserved characters within a path segment', () => {
    expect(fileUrl('o/r', 'deadbeef', 'docs/a b/c#d.md'))
      .toBe('https://github.com/o/r/blob/deadbeef/docs/a%20b/c%23d.md')
  })

  it('builds commit and PR urls', () => {
    expect(commitUrl('o/r', 'sha1')).toBe('https://github.com/o/r/commit/sha1')
    expect(prUrl('o/r', 42)).toBe('https://github.com/o/r/pull/42')
  })
})
