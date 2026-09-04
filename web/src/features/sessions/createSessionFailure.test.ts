import { describe, it, expect } from 'vitest'
import { ApiRequestError } from '@/api/client'
import { MUTATION_ERROR_FALLBACK } from '@/api/queryClient'
import {
  describeCreateSessionFailure, REAUTH_MESSAGE, CREATE_SESSION_FALLBACK,
} from './createSessionFailure'

describe('describeCreateSessionFailure — the 409 triage', () => {
  it('branch_switch_failed keeps the server message', () => {
    const err = new ApiRequestError(409, { error: { code: 'branch_switch_failed', message: 'no feat/x' } })
    expect(describeCreateSessionFailure(err)).toEqual({ kind: 'branch', message: 'no feat/x' })
  })

  it('branch_switch_failed without a message gets a fixed line', () => {
    const err = new ApiRequestError(409, { error: { code: 'branch_switch_failed' } })
    expect(describeCreateSessionFailure(err)).toEqual({ kind: 'branch', message: 'Could not switch branch' })
  })

  it('accounts_need_reauth points at Settings → Accounts', () => {
    const err = new ApiRequestError(409, { error: { code: 'accounts_need_reauth' } })
    expect(describeCreateSessionFailure(err)).toEqual({ kind: 'reauth', message: REAUTH_MESSAGE })
  })

  it('any other 409 is "all accounts limited", carrying the reset time when the daemon gave one', () => {
    expect(describeCreateSessionFailure(
      new ApiRequestError(409, { error: { code: 'all_accounts_limited' }, earliestReset: '2026-09-04T12:00:00Z' }),
    )).toEqual({ kind: 'all-limited', earliestReset: '2026-09-04T12:00:00Z' })
    expect(describeCreateSessionFailure(new ApiRequestError(409, {})))
      .toEqual({ kind: 'all-limited', earliestReset: null })
  })

  it('a 409 whose body is not an object still reads as all-limited (no throw)', () => {
    expect(describeCreateSessionFailure(new ApiRequestError(409, null as unknown as Record<string, unknown>)))
      .toEqual({ kind: 'all-limited', earliestReset: null })
  })
})

describe('describeCreateSessionFailure — everything else', () => {
  it('uses the body message when the server sent one', () => {
    const err = new ApiRequestError(500, { detail: { message: 'daemon is draining' } })
    expect(describeCreateSessionFailure(err)).toEqual({ kind: 'other', message: 'daemon is draining' })
  })

  it('falls back to a create-specific line, not the generic mutation fallback', () => {
    const got = describeCreateSessionFailure(new ApiRequestError(500, {}))
    expect(got).toEqual({ kind: 'other', message: CREATE_SESSION_FALLBACK })
    expect(got).not.toEqual({ kind: 'other', message: MUTATION_ERROR_FALLBACK })
  })

  it('a non-API error (network) also lands on the fallback', () => {
    expect(describeCreateSessionFailure(new TypeError('Failed to fetch')))
      .toEqual({ kind: 'other', message: CREATE_SESSION_FALLBACK })
  })
})
