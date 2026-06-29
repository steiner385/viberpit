import { describe, it, expect } from 'vitest'
import { accountNameError } from './accountName'

describe('accountNameError', () => {
  it('treats empty input as valid (gated separately on non-empty)', () => {
    expect(accountNameError('')).toBeNull()
  })

  it('accepts plain identifiers', () => {
    expect(accountNameError('work')).toBeNull()
    expect(accountNameError('steinonline')).toBeNull()
    expect(accountNameError('tony_steinonline')).toBeNull()
    expect(accountNameError('tony.stein')).toBeNull()
    expect(accountNameError('_underscore-start')).toBeNull()
  })

  it('gives email-specific guidance when the name contains @', () => {
    const err = accountNameError('tony@steinonline.net')
    expect(err).toBeTruthy()
    expect(err).toMatch(/email/i)
  })

  it('rejects spaces and other disallowed characters', () => {
    expect(accountNameError('two words')).toBeTruthy()
    expect(accountNameError('has/slash')).toBeTruthy()
  })

  it('rejects names that do not start with a letter, number, or underscore', () => {
    expect(accountNameError('.hidden')).toBeTruthy()
    expect(accountNameError('-leading-dash')).toBeTruthy()
  })

  it('rejects "." and ".."', () => {
    expect(accountNameError('.')).toBeTruthy()
    expect(accountNameError('..')).toBeTruthy()
  })

  it('rejects names longer than 64 characters', () => {
    expect(accountNameError('a'.repeat(65))).toBeTruthy()
    expect(accountNameError('a'.repeat(64))).toBeNull()
  })
})
