import { describe, it, expect, beforeEach } from 'vitest'
import { loadSessionOrder, saveSessionOrder } from '../sessionOrder'

describe('sessionOrder persistence', () => {
  beforeEach(() => window.localStorage.clear())

  it('defaults to none/status when nothing stored', () => {
    expect(loadSessionOrder()).toEqual({ groupBy: 'none', sortBy: 'status' })
  })

  it('round-trips a saved selection', () => {
    saveSessionOrder({ groupBy: 'repo', sortBy: 'lastActive' })
    expect(loadSessionOrder()).toEqual({ groupBy: 'repo', sortBy: 'lastActive' })
  })

  it('falls back to defaults on corrupt JSON', () => {
    window.localStorage.setItem('ccswitch.sessionOrder', '{not json')
    expect(loadSessionOrder()).toEqual({ groupBy: 'none', sortBy: 'status' })
  })

  it('rejects unknown enum values', () => {
    window.localStorage.setItem('ccswitch.sessionOrder', JSON.stringify({ groupBy: 'x', sortBy: 'y' }))
    expect(loadSessionOrder()).toEqual({ groupBy: 'none', sortBy: 'status' })
  })
})
