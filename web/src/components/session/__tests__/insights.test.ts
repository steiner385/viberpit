import { describe, it, expect } from 'vitest'
import { parseInsights } from '../insights'

const OPEN = '★ Insight ─────────────────────────────────────'
const CLOSE = '─────────────────────────────────────────────────'

describe('parseInsights', () => {
  it('returns whole input as a single prose segment when no insight present', () => {
    expect(parseInsights('hello **world**')).toEqual([
      { kind: 'prose', text: 'hello **world**' },
    ])
  })

  it('extracts a lone insight, stripping delimiter lines', () => {
    const input = [OPEN, 'point one', 'point two', CLOSE].join('\n')
    expect(parseInsights(input)).toEqual([
      { kind: 'insight', text: 'point one\npoint two' },
    ])
  })

  it('preserves prose before and after an insight, in order', () => {
    const input = ['before', OPEN, 'body', CLOSE, 'after'].join('\n')
    expect(parseInsights(input)).toEqual([
      { kind: 'prose', text: 'before' },
      { kind: 'insight', text: 'body' },
      { kind: 'prose', text: 'after' },
    ])
  })

  it('handles multiple insights interleaved with prose', () => {
    const input = [OPEN, 'a', CLOSE, 'mid', OPEN, 'b', CLOSE].join('\n')
    expect(parseInsights(input)).toEqual([
      { kind: 'insight', text: 'a' },
      { kind: 'prose', text: 'mid' },
      { kind: 'insight', text: 'b' },
    ])
  })

  it('treats an unterminated opener as prose and never drops text', () => {
    const input = [OPEN, 'dangling body', 'more text'].join('\n')
    const result = parseInsights(input)
    expect(result).toEqual([{ kind: 'prose', text: input }])
  })

  it('does not treat a line of ASCII hyphens as a delimiter', () => {
    const input = ['★ Insight ----------', 'body', '----------'].join('\n')
    expect(parseInsights(input)).toEqual([{ kind: 'prose', text: input }])
  })

  it('detects delimiters wrapped in markdown backticks', () => {
    // The insight-generation prompt instructs agents to emit the delimiter
    // lines wrapped in backticks, e.g. `★ Insight ─────` … `─────`.
    const input = ['`★ Insight ─────────`', 'point one', '`─────────`'].join('\n')
    expect(parseInsights(input)).toEqual([{ kind: 'insight', text: 'point one' }])
  })

  it('detects em-dash and en-dash delimiters', () => {
    const emInput = ['★ Insight —————————', 'em body', '—————————'].join('\n')
    expect(parseInsights(emInput)).toEqual([{ kind: 'insight', text: 'em body' }])

    const enInput = ['★ Insight –––––––––', 'en body', '–––––––––'].join('\n')
    expect(parseInsights(enInput)).toEqual([{ kind: 'insight', text: 'en body' }])
  })

  it('detects a backtick-wrapped opener with a bare closer (mixed)', () => {
    const input = ['`★ Insight ─────────`', 'mixed body', '─────────'].join('\n')
    expect(parseInsights(input)).toEqual([{ kind: 'insight', text: 'mixed body' }])
  })

  it('still rejects prose that merely mentions an em-dash after Insight', () => {
    // "★ Insight" followed by real sentence content (not pure decoration)
    // must not be mistaken for an opener, even with a dash-line nearby.
    const input = ['★ Insight — this is a sentence, not a delimiter.', '—————————'].join('\n')
    expect(parseInsights(input)).toEqual([{ kind: 'prose', text: input }])
  })

  it('detects an opener with body text on the same line as the dash run', () => {
    // Some replies put the first sentence right after the rule run instead of
    // on the next line. The dash run (≥3) marks it as a real callout.
    const input = ['★ Insight ───────── There are actually two mechanisms.', '─────────'].join('\n')
    expect(parseInsights(input)).toEqual([
      { kind: 'insight', text: 'There are actually two mechanisms.' },
    ])
  })

  it('prepends same-line body to the following body lines', () => {
    const input = ['★ Insight ───────── first part', 'second part', '─────────'].join('\n')
    expect(parseInsights(input)).toEqual([
      { kind: 'insight', text: 'first part\nsecond part' },
    ])
  })

  it('detects a backtick-wrapped opener with same-line body', () => {
    const input = ['`★ Insight ───────── inline body`', '─────────'].join('\n')
    expect(parseInsights(input)).toEqual([{ kind: 'insight', text: 'inline body' }])
  })

  it('requires a dash RUN, not a single dash, for a same-line-body opener', () => {
    // A single em-dash + content is indistinguishable from prose, so it stays prose.
    const input = ['★ Insight — just a passing thought', '—————————'].join('\n')
    expect(parseInsights(input)).toEqual([{ kind: 'prose', text: input }])
  })

  it('detects an inline title with the dash run at the end of the opener line', () => {
    // Real form seen in the wild: "★ Insight — <title> ─────" then body, then closer.
    const input = [
      '`★ Insight — a scaling concern with the gate ─────`',
      '- Pre-change each run was quick.',
      '- Post-change the gate can stall.',
      '`─────────────────────────────────────────────────`',
    ].join('\n')
    expect(parseInsights(input)).toEqual([
      {
        kind: 'insight',
        text: 'a scaling concern with the gate\n- Pre-change each run was quick.\n- Post-change the gate can stall.',
      },
    ])
  })
})
