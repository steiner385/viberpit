import { describe, expect, it } from 'vitest'
import { ApiRequestError, apiErrorCode } from '../client'

describe('apiErrorCode', () => {
  it('reads nested error.code / detail.code / top-level code', () => {
    expect(apiErrorCode(new ApiRequestError(409, { error: { code: 'a' } }))).toBe('a')
    expect(apiErrorCode(new ApiRequestError(422, { detail: { code: 'b' } }))).toBe('b')
    expect(apiErrorCode(new ApiRequestError(400, { code: 'c', detail: 'prose' }))).toBe('c')
  })

  it('reads a flat string error body (the MCP test endpoint)', () => {
    expect(apiErrorCode(new ApiRequestError(504, { error: 'mcp_probe_timeout' }))).toBe('mcp_probe_timeout')
    expect(apiErrorCode({ error: 'unknown_account' })).toBe('unknown_account')
  })

  it('returns null when there is no code', () => {
    expect(apiErrorCode(new ApiRequestError(500, {}))).toBeNull()
    expect(apiErrorCode(null)).toBeNull()
  })
})
