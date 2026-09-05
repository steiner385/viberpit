// @vitest-environment node
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

  it('pickWorkingWord(exclude) never returns the excluded word', () => {
    // So a new wait never repeats the immediately-previous wait's phrase.
    for (const exclude of WORKING_WORDS) {
      for (let i = 0; i < 30; i++) {
        const picked = pickWorkingWord(exclude)
        expect(picked).not.toBe(exclude)
        expect(WORKING_WORDS).toContain(picked)
      }
    }
  })
})
