import { describe, expect, it } from 'vitest'
import { composePlanFeedback } from './planFeedback'
import type { LineComment } from './reviewComments'

const step = (n: number, q: string, body: string): LineComment => ({
  id: `s${n}`,
  anchor: { path: null, side: 'new', start: n, end: n },
  quote: [q],
  body,
})

describe('composePlanFeedback', () => {
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
