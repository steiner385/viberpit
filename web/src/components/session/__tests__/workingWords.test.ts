import { describe, it, expect } from 'vitest'
import { WORKING_WORDS, pickWorkingWord } from '../workingWords'

describe('workingWords', () => {
  it('has a non-empty list', () => {
    expect(WORKING_WORDS.length).toBeGreaterThan(0)
  })

  it('all entries end in "ing"', () => {
    for (const word of WORKING_WORDS) {
      expect(word, `"${word}" should end in "ing"`).toMatch(/ing$/)
    }
  })

  it('pickWorkingWord returns a member of the list', () => {
    // Run many times to reduce flakiness risk
    for (let i = 0; i < 50; i++) {
      const picked = pickWorkingWord()
      expect(WORKING_WORDS).toContain(picked)
    }
  })
})
