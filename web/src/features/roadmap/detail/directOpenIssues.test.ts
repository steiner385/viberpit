import { describe, it, expect } from 'vitest'
import { MAP, cap } from '../__fixtures__/map'
import { findCapability } from '../lookup'
import { directOpenIssues } from './directOpenIssues'

const kindash = MAP.products[0]!

describe('directOpenIssues', () => {
  it('subtracts the children the rollup folded in', () => {
    const tasks = findCapability(kindash, 'tasks')!
    // 5 rolled up, 3 of them under `task-views`.
    expect(tasks.counts.openIssues).toBe(5)
    expect(directOpenIssues(tasks)).toBe(2)
  })

  it('is the rollup itself for a leaf capability', () => {
    expect(directOpenIssues(findCapability(kindash, 'task-views')!)).toBe(3)
  })

  it('never goes negative on an inconsistent count', () => {
    const odd = cap({
      slug: 'odd',
      counts: { openIssues: 1, inFlight: 0, blocked: 0, needsYou: 0 },
      children: [cap({ slug: 'child', counts: { openIssues: 4, inFlight: 0, blocked: 0, needsYou: 0 } })],
    })
    expect(directOpenIssues(odd)).toBe(0)
  })
})
