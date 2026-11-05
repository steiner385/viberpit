import { describe, expect, it } from 'vitest'
import { ApiRequestError } from '@/api/client'
import { MCP_SCOPE_DISABLED_MESSAGE, isMcpScopeDisabled, mcpMutationErrorMessage, relativeAge, shortName, toneFor } from './mcpStatus'

describe('toneFor', () => {
  it('maps the vocabulary', () => {
    expect(toneFor('failed')).toBe('bad'); expect(toneFor('needs-auth')).toBe('warn')
    expect(toneFor('pending')).toBe('pending'); expect(toneFor('connected')).toBe('ok')
    expect(toneFor('disabled')).toBe('neutral'); expect(toneFor('connecting')).toBe('neutral')
    expect(toneFor(null)).toBe('unknown')
  })
})

it('shortName strips plugin prefix', () => {
  expect(shortName('plugin:railway:railway')).toBe('railway')
  expect(shortName('github')).toBe('github')
})

it('relativeAge reuses the shared relative-time formatter', () => {
  const now = Date.parse('2026-10-09T12:05:00Z')
  expect(relativeAge('2026-10-09T12:02:00Z', now)).toBe('3m ago')
})

describe('mcpMutationErrorMessage', () => {
  it('maps the kill switch code to text and passes other refusals through', () => {
    const off = new ApiRequestError(404, { error: 'mcp_scope_disabled' })
    expect(isMcpScopeDisabled(off)).toBe(true)
    expect(mcpMutationErrorMessage(off, 'fallback')).toBe(MCP_SCOPE_DISABLED_MESSAGE)
    const refused = new ApiRequestError(409, { detail: { code: 'MCP_TOOL_UNKNOWN', message: 'no such tool' } })
    expect(isMcpScopeDisabled(refused)).toBe(false)
    expect(mcpMutationErrorMessage(refused, 'fallback')).toBe('no such tool')
  })
})
