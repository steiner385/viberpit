// The two halves of deriving a slug from a title: what is safe to show WHILE
// typing, and what is safe to send.
import { describe, it, expect } from 'vitest'
import { sanitize, slugify } from './slug'

describe('roadmap slug', () => {
  it('keeps the separator the user is still typing past', () => {
    // Dropping it here would make the space between two words untypeable: the
    // field would swallow it and the next word would join the previous one.
    expect(sanitize('Voice Notes ')).toBe('voice-notes-')
  })

  it('trims the trailing separator off the value that is sent', () => {
    expect(slugify('Voice Notes ')).toBe('voice-notes')
  })

  it('normalises a punctuation-only title to nothing', () => {
    // The empty result is what the dialogs test for to disable Create and say
    // why, rather than posting a slug the server would reject.
    expect(slugify('!!!')).toBe('')
  })
})
