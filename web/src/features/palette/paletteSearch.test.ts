import { afterEach, describe, expect, it, vi } from 'vitest'
import { prepareSearch, searchPrepared, type SearchOps } from './paletteSearch'

const item = (label: string, keywords: string[] = []) => ({ label, keywords })
const labels = (xs: readonly { label: string }[]) => xs.map((x) => x.label)
const run = (items: { label: string; keywords?: readonly string[] }[], q: string, ops?: SearchOps) =>
  labels(searchPrepared(prepareSearch(items), q, ops))

describe('paletteSearch', () => {
  afterEach(() => vi.restoreAllMocks())

  it('ranks prefix, then word start, then substring', () => {
    const items = [item('Unrelated'), item('Barn door'), item('The bar'), item('Bar stool'), item('Rebar')]
    expect(run(items, 'bar')).toEqual(['Barn door', 'Bar stool', 'The bar', 'Rebar'])
  })

  it('treats punctuation as a word boundary (repo/branch names)', () => {
    expect(run([item('steiner385/viberpit'), item('xviberpit')], 'viber')).toEqual(['steiner385/viberpit', 'xviberpit'])
    expect(run([item('feat/desktop-parity')], 'parity')).toEqual(['feat/desktop-parity'])
  })

  it('is case-insensitive in both directions', () => {
    expect(run([item('MCP servers')], 'mcp')).toEqual(['MCP servers'])
    expect(run([item('hooks')], 'HOOKS')).toEqual(['hooks'])
  })

  it('is stable: equal ranks keep their input order', () => {
    const items = [item('alpha one'), item('alpha two'), item('alpha three')]
    expect(run(items, 'alpha')).toEqual(['alpha one', 'alpha two', 'alpha three'])
    expect(run([...items].reverse(), 'alpha')).toEqual(['alpha three', 'alpha two', 'alpha one'])
  })

  it('matches keywords, ranked by the best field', () => {
    const items = [item('Appearance', ['theme', 'dark']), item('Themes gallery')]
    // "Themes gallery" is a label prefix; Appearance a keyword prefix — both rank 0, input order holds.
    expect(run(items, 'them')).toEqual(['Appearance', 'Themes gallery'])
    expect(run(items, 'dark')).toEqual(['Appearance'])
  })

  it('every whitespace-separated term must match; the empty query returns everything in order', () => {
    const items = [item('Fix login bug', ['viberpit']), item('Fix logout'), item('Login page')]
    expect(run(items, 'fix login')).toEqual(['Fix login bug'])
    expect(run(items, 'login viberpit')).toEqual(['Fix login bug'])
    expect(run(items, '   ')).toEqual(['Fix login bug', 'Fix logout', 'Login page'])
  })

  it('drops non-matches', () => {
    expect(run([item('abc')], 'zzz')).toEqual([])
  })

  it('1,000 entries: one lowercase per query, no regex, and a fixed scan budget', () => {
    const items = Array.from({ length: 1000 }, (_, i) =>
      item(`Session ${i} working on feature-${i % 37}`, [`owner/repo-${i % 11}`, `branch-${i}`, 'Idle']))
    const prepared = prepareSearch(items)
    for (const q of ['e', 'feature-3', 'repo', 'zzz', 'session 99 idle']) {
      // Spies live only around the call, so the assertions' own regexes are not counted.
      const lower = vi.spyOn(String.prototype, 'toLowerCase')
      const regex = [
        vi.spyOn(String.prototype, 'match'), vi.spyOn(String.prototype, 'search'),
        vi.spyOn(String.prototype, 'replace'), vi.spyOn(RegExp.prototype, 'exec'),
        vi.spyOn(RegExp.prototype, 'test'),
      ]
      const ops: SearchOps = { scans: 0 }
      searchPrepared(prepared, q, ops)
      const lowered = lower.mock.calls.length
      const regexCalls = regex.map((r) => r.mock.calls.length)
      vi.restoreAllMocks()
      expect(lowered, q).toBe(1)
      expect(regexCalls, q).toEqual([0, 0, 0, 0, 0])
      // Four fields per entry; a term costs at most three scans per field
      // (prefix, word start, substring) — never a walk over every occurrence.
      expect(ops.scans, q).toBeLessThanOrEqual(1000 * 4 * 3 * q.split(' ').length)
    }
  })
})
