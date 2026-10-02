// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { backgroundWorkSummary } from './backgroundWorkSummary'
import type { BackgroundWorkRow } from '@/session/selectors'

const row = (kind: BackgroundWorkRow['kind'], status: BackgroundWorkRow['status'] = 'running'): BackgroundWorkRow =>
  ({ toolUseId: kind + status, kind, description: '', status, live: true })

describe('backgroundWorkSummary', () => {
  it('counts running rows per kind and appends the wakeup countdown', () => {
    expect(backgroundWorkSummary({ rows: [row('shell'), row('shell'), row('monitor'), row('shell', 'completed')], wakeup: true, wakeupAt: 61_000 }, 1_000))
      .toBe('2 shells · 1 monitor · wakeup in 1m 0s')
  })
  it('names an untimed wakeup as armed', () => {
    expect(backgroundWorkSummary({ rows: [], wakeup: true, wakeupAt: null }, 0)).toBe('wakeup armed')
  })
  it('is null when nothing is running', () => {
    expect(backgroundWorkSummary({ rows: [row('shell', 'stopped')], wakeup: false, wakeupAt: null }, 0)).toBeNull()
  })
})
