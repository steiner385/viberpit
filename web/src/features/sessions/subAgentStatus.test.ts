import { describe, it, expect } from 'vitest'
import { STATUS_TONE, statusTone } from './subAgentStatus'

describe('statusTone', () => {
  it('returns the mapped tone for every known status', () => {
    for (const status of Object.keys(STATUS_TONE) as Array<keyof typeof STATUS_TONE>) {
      expect(statusTone(status)).toBe(STATUS_TONE[status])
    }
  })

  it('falls back to bg-neutral for an unrecognized wire status, never an invisible dot', () => {
    expect(statusTone('some-future-status')).toBe('bg-neutral')
  })
})
