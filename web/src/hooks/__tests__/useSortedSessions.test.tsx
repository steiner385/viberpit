import { describe, it, expect, vi } from 'vitest'
import { renderHook } from '@testing-library/react'
import * as hooks from '@/api/hooks'
import { useSortedSessions } from '../useSortedSessions'
import type { Session } from '@/api/types'

vi.mock('@/api/hooks', () => ({ useSessions: vi.fn() }))

function session(id: string, status: Session['status'], name = id): Session {
  return { id, name, accountName: 'acct', status, createdAt: '', updatedAt: '' }
}

function mockSessions(data: Session[] | undefined) {
  vi.mocked(hooks.useSessions).mockReturnValue({ data } as ReturnType<typeof hooks.useSessions>)
}

describe('useSortedSessions', () => {
  it('orders by tier: needs-you/error → your-turn → busy', () => {
    mockSessions([
      session('a', 'in_progress'),
      session('b', 'done'),
      session('c', 'waiting_input'),
      session('d', 'needs_attention'),
    ])
    const { result } = renderHook(() => useSortedSessions())
    expect(result.current.data?.map((s) => s.id)).toEqual(['c', 'd', 'b', 'a'])
  })

  it('keeps the established order across polls when no tier changes (no jitter)', () => {
    const first = [session('a', 'in_progress'), session('b', 'waiting_input')]
    mockSessions(first)
    const { result, rerender } = renderHook(() => useSortedSessions())
    expect(result.current.data?.map((s) => s.id)).toEqual(['b', 'a'])

    // Next poll: same tiers, but a non-tier field (name) changed.
    mockSessions([session('a', 'in_progress', 'renamed-a'), session('b', 'waiting_input')])
    rerender()
    const data = result.current.data!
    expect(data.map((s) => s.id)).toEqual(['b', 'a'])
    // Order preserved AND the fresh field is reflected.
    expect(data.find((s) => s.id === 'a')?.name).toBe('renamed-a')
  })

  it('re-sorts when a session changes tier', () => {
    mockSessions([session('a', 'in_progress'), session('b', 'done')])
    const { result, rerender } = renderHook(() => useSortedSessions())
    expect(result.current.data?.map((s) => s.id)).toEqual(['b', 'a'])

    // 'a' becomes blocked → it must float to the top.
    mockSessions([session('a', 'waiting_input'), session('b', 'done')])
    rerender()
    expect(result.current.data?.map((s) => s.id)).toEqual(['a', 'b'])
  })
})
