// @vitest-environment node
import { describe, it, expect } from 'vitest'
import type { Account, CopilotAddState } from '../types'

describe('Copilot types', () => {
  it('Account with provider: copilot compiles and is assignable', () => {
    const account: Account = {
      name: 'copilot-test',
      status: 'ok',
      provider: 'copilot',
      usage: null,
      liveSessions: { active: 0, idle: 0 },
      rank: null,
      rankReason: null,
    }
    expect(account.provider).toBe('copilot')
  })

  it('Account without provider is optional and backward-compatible', () => {
    const account: Account = {
      name: 'anthropic-test',
      status: 'ok',
      usage: null,
      liveSessions: { active: 0, idle: 0 },
      rank: null,
      rankReason: null,
    }
    expect(account.provider).toBeUndefined()
  })

  it('CopilotAddState with all state values is valid', () => {
    const pending: CopilotAddState = {
      state: 'pending',
      userCode: null,
      verificationUri: null,
    }
    expect(pending.state).toBe('pending')

    const awaiting: CopilotAddState = {
      state: 'awaiting_authorization',
      userCode: 'ABC123',
      verificationUri: 'https://example.com/auth',
    }
    expect(awaiting.state).toBe('awaiting_authorization')
    expect(awaiting.userCode).toBe('ABC123')
    expect(awaiting.verificationUri).toBe('https://example.com/auth')

    const authorized: CopilotAddState = {
      state: 'authorized',
      userCode: null,
      verificationUri: null,
    }
    expect(authorized.state).toBe('authorized')

    const failed: CopilotAddState = {
      state: 'failed',
      userCode: null,
      verificationUri: null,
    }
    expect(failed.state).toBe('failed')
  })
})
