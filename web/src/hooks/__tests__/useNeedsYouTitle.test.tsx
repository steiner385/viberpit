import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook } from '@testing-library/react'
import * as hooks from '@/api/hooks'
import { useNeedsYouTitle } from '../useNeedsYouTitle'
import type { Session } from '@/api/types'

vi.mock('@/api/hooks', () => ({ useSessions: vi.fn() }))

function session(id: string, status: Session['status']): Session {
  return {
    id, name: id, accountName: 'acct', status,
    createdAt: '', updatedAt: '',
  }
}

function mockSessions(data: Session[] | undefined) {
  vi.mocked(hooks.useSessions).mockReturnValue({ data } as ReturnType<typeof hooks.useSessions>)
}

describe('useNeedsYouTitle', () => {
  beforeEach(() => {
    document.title = 'ccswitch'
  })

  it('sets the count and "needs you" suffix when sessions are blocked', () => {
    mockSessions([
      session('a', 'waiting_input'),
      session('b', 'ready_review'),
      session('c', 'in_progress'),
      session('d', 'done'),
    ])
    renderHook(() => useNeedsYouTitle())
    expect(document.title).toBe('(2) ccswitch — needs you')
  })

  it('resets to the plain title when no session needs the user', () => {
    mockSessions([session('a', 'in_progress'), session('b', 'done'), session('c', 'idle')])
    renderHook(() => useNeedsYouTitle())
    expect(document.title).toBe('ccswitch')
  })

  it('treats error-tier sessions as NOT needs-you (only the needs-you tier counts)', () => {
    mockSessions([session('a', 'needs_attention'), session('b', 'ended')])
    renderHook(() => useNeedsYouTitle())
    expect(document.title).toBe('ccswitch')
  })

  it('handles undefined data (still loading)', () => {
    mockSessions(undefined)
    renderHook(() => useNeedsYouTitle())
    expect(document.title).toBe('ccswitch')
  })

  it('restores the plain title on unmount', () => {
    mockSessions([session('a', 'waiting_input')])
    const { unmount } = renderHook(() => useNeedsYouTitle())
    expect(document.title).toBe('(1) ccswitch — needs you')
    unmount()
    expect(document.title).toBe('ccswitch')
  })
})
