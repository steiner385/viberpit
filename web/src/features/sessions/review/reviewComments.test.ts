import { describe, expect, it } from 'vitest'
import {
  composePlanFeedback,
  composeReviewPrompt,
  newCommentId,
  sortComments,
  type LineComment,
} from './reviewComments'

const c = (
  path: string | null,
  side: 'new' | 'old',
  start: number,
  end: number,
  quote: string[],
  body: string,
): LineComment => ({ id: `${path}-${side}-${start}`, anchor: { path, side, start, end }, quote, body })

describe('sortComments', () => {
  it('orders by path, then new before old, then start', () => {
    const list = [
      c('b.ts', 'new', 1, 1, [], 'x'),
      c('a.ts', 'old', 3, 3, [], 'x'),
      c('a.ts', 'new', 9, 9, [], 'x'),
      c('a.ts', 'new', 2, 2, [], 'x'),
    ]
    expect(sortComments(list).map((x) => x.id)).toEqual(['a.ts-new-2', 'a.ts-new-9', 'a.ts-old-3', 'b.ts-new-1'])
    expect(list[0].id).toBe('b.ts-new-1')
  })
})

describe('composeReviewPrompt', () => {
  it('renders the exact text', () => {
    const out = composeReviewPrompt([
      c('b.ts', 'new', 5, 5, ['const b = 1'], '  rename this  '),
      c('a.ts', 'old', 7, 7, ['gone()'], 'why removed?'),
      c('a.ts', 'new', 12, 14, ['1', '2', '3', '4', '5', '6', '7'], 'too long'),
    ])
    expect(out).toBe(
      [
        'Review comments on the current changes (3):',
        '',
        '1. `a.ts` line 12–14:',
        '   > 1',
        '   > 2',
        '   > 3',
        '   > 4',
        '   > 5',
        '   > …',
        '   too long',
        '',
        '2. `a.ts` line 7 (removed):',
        '   > gone()',
        '   why removed?',
        '',
        '3. `b.ts` line 5:',
        '   > const b = 1',
        '   rename this',
        '',
        'Address each comment, then summarize what you changed.',
      ].join('\n'),
    )
  })
})

describe('composePlanFeedback', () => {
  const step = (n: number, q: string, body: string) => c(null, 'new', n, n, [q], body)
  it('overall only', () => {
    expect(composePlanFeedback('  hi  ', [])).toBe('hi')
  })
  it('steps only has no leading blank line', () => {
    expect(composePlanFeedback('', [step(2, 'Do X', ' no ')])).toBe('On the plan:\n- line 2: "Do X" — no')
  })
  it('both', () => {
    expect(composePlanFeedback('Overall', [step(1, 'A', 'a'), step(3, 'C', 'c')])).toBe(
      'Overall\n\nOn the plan:\n- line 1: "A" — a\n- line 3: "C" — c',
    )
  })
})

describe('newCommentId', () => {
  it('is unique', () => {
    expect(newCommentId()).not.toBe(newCommentId())
  })
})
