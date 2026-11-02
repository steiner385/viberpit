import { describe, expect, it } from 'vitest'
import {
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
  it('marks a comment whose anchor no longer matches the diff as (outdated)', () => {
    const fresh = c('a.ts', 'new', 2, 2, ['x'], 'ok')
    const stale = c('a.ts', 'old', 4, 5, ['y', 'z'], 'moved')
    expect(composeReviewPrompt([fresh, stale], new Set([stale.id]))).toBe([
      'Review comments on the current changes (2):',
      '',
      '1. `a.ts` line 2:',
      '   > x',
      '   ok',
      '',
      '2. `a.ts` line 4–5 (removed) (outdated):',
      '   > y',
      '   > z',
      '   moved',
      '',
      'Address each comment, then summarize what you changed.',
    ].join('\n'))
  })

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

describe('newCommentId', () => {
  it('is unique', () => {
    expect(newCommentId()).not.toBe(newCommentId())
  })
})
