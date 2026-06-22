import { describe, it, expect, afterEach } from 'vitest'
import { renderHook } from '@testing-library/react'
import { useNeedsYouTitle } from '../useNeedsYouTitle'
import type { Session } from '@/api/types'

function s(id: string, status: string): Session {
  return { id, status } as unknown as Session
}

afterEach(() => { document.title = 'ccswitch' })

describe('useNeedsYouTitle', () => {
  it('sets a count when sessions need you', () => {
    renderHook(() => useNeedsYouTitle([s('a', 'waiting_input'), s('b', 'in_progress'), s('c', 'ready_review')]))
    expect(document.title).toBe('(2) ccswitch — needs you')
  })

  it('resets to plain title when none need you', () => {
    renderHook(() => useNeedsYouTitle([s('a', 'in_progress'), s('b', 'done')]))
    expect(document.title).toBe('ccswitch')
  })
})
