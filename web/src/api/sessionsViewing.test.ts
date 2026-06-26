import { describe, it, expect } from 'vitest'
import { setSessionsViewing, isSessionsViewing } from './sessionsViewing'

describe('sessionsViewing flag', () => {
  it('defaults to false and round-trips', () => {
    setSessionsViewing(false)
    expect(isSessionsViewing()).toBe(false)
    setSessionsViewing(true)
    expect(isSessionsViewing()).toBe(true)
  })
})
