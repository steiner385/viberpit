// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { isFilePathRef, parseFilePathRef, worktreeRelativePath } from './filePathRef'

describe('isFilePathRef', () => {
  it.each([
    'web/src/foo.tsx:60',
    'web/src/foo.tsx',
    'foo.py',
    './config.json',
    '/etc/hosts',
    'src/lib/bar.ts',
    'App.tsx:12:4',
    '~/notes.md',
    'foo.123',            // numeric extension accepted (e.g. split-archive names)
  ])('accepts path-like token %j', (s) => {
    expect(isFilePathRef(s)).toBe(true)
  })

  it.each([
    'npm install',        // whitespace
    'useState',           // bare identifier, no slash or extension
    '--force',            // flag
    '1/2',                // ratio: slash but no path-ish segment/extension
    '',                   // empty
    'a.b c.d',            // whitespace
    'https://example.com',// URL, not a file path
    'foo.py\n',           // trailing newline disqualifies (guards fenced-block leak fix)
    ' foo.py',            // leading whitespace disqualifies (guards fenced-block leak fix)
  ])('rejects non-path token %j', (s) => {
    expect(isFilePathRef(s)).toBe(false)
  })
})

describe('parseFilePathRef', () => {
  it.each([
    ['src/a.ts', { path: 'src/a.ts' }],
    ['src/a.ts:12', { path: 'src/a.ts', line: 12 }],
    ['src/a.ts:12:4', { path: 'src/a.ts', line: 12, col: 4 }],
    ['/repo/src/a.ts:7', { path: '/repo/src/a.ts', line: 7 }],
  ])('%j parses to %j', (s, want) => {
    expect(parseFilePathRef(s)).toEqual(want)
  })

  it.each(['npm install', 'useState', 'https://example.com/a.ts:3', ''])('%j is not a path ref', (s) => {
    expect(parseFilePathRef(s)).toBeNull()
  })

  it('a :0 line reads as no line (lines are 1-based)', () => {
    expect(parseFilePathRef('src/a.ts:0')).toEqual({ path: 'src/a.ts' })
  })
})

describe('worktreeRelativePath', () => {
  it.each([
    ['/repo/src/a.ts', '/repo', 'src/a.ts'],
    ['/repo/src/a.ts', '/repo/', 'src/a.ts'],
    ['/repo//src/./a.ts', '/repo', 'src/a.ts'],
    ['src/a.ts', '/repo', 'src/a.ts'],
    ['./src/a.ts', '/repo', 'src/a.ts'],
    ['README.md', '/repo', 'README.md'],
    ['pyproject.toml', '/repo', 'pyproject.toml'],
    ['notes.TXT', '/repo', 'notes.TXT'],
    ['uv.lock', '/repo', 'uv.lock'],
    ['/repo/data/x.unknownext', '/repo', 'data/x.unknownext'], // has a `/`: extension not needed
  ])('%j under %j is %j', (path, cwd, want) => {
    expect(worktreeRelativePath(path, cwd)).toBe(want)
  })

  it.each([
    ['/etc/hosts', '/repo'],
    ['/repo-evil/x.ts', '/repo'], // a sibling sharing the prefix is not under cwd
    ['/repo', '/repo'],
    ['/repo/../etc/hosts', '/repo'],
    ['../other/a.ts', '/repo'],
    ['src/../../a.ts', '/repo'],
    ['~/notes.md', '/repo'],
    ['src/', '/repo'],
    ['/repo/src/a.ts', ''],
    // Fix round 1: dotted identifiers, versions and host:port are not files.
    ['app.state.sessions', '/repo'],
    ['asyncio.sleep', '/repo'],
    ['self.cwd', '/repo'],
    ['1.2.3', '/repo'],
    ['v2.1', '/repo'],
    ['localhost:8080/api', '/repo'],
    ['127.0.0.1:8787/api/x', '/repo'],
    // Windows-style paths stay copy-only.
    ['src\\a.ts', '/repo'],
    ['C:/repo/a.ts', '/repo'],
    ['c:a.ts', '/repo'],
  ])('%j under %j is not a worktree file', (path, cwd) => {
    expect(worktreeRelativePath(path, cwd)).toBeNull()
  })
})
