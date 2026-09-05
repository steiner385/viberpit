// @vitest-environment node
import { describe, it, expect, beforeEach } from 'vitest'
import { setSessionsViewing, isSessionsViewing } from './sessionsViewing'

beforeEach(() => setSessionsViewing(false))

describe('sessionsViewing flag', () => {
  it('defaults to false and round-trips', () => {
    setSessionsViewing(false)
    expect(isSessionsViewing()).toBe(false)
    setSessionsViewing(true)
    expect(isSessionsViewing()).toBe(true)
  })
})
