import { act, renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { useReviewDraft } from './useReviewDraft'
import type { LineComment } from './reviewComments'

const mk = (id: string): LineComment => ({
  id,
  anchor: { path: 'a.ts', side: 'new', start: 1, end: 1 },
  quote: ['x'],
  body: 'b',
})

describe('useReviewDraft', () => {
  beforeEach(() => sessionStorage.clear())

  it('adds, updates, removes and clears', () => {
    const { result } = renderHook(() => useReviewDraft('s1'))
    act(() => result.current.add(mk('1')))
    act(() => result.current.add(mk('2')))
    expect(result.current.comments.map((x) => x.id)).toEqual(['1', '2'])
    act(() => result.current.update('1', 'new body'))
    expect(result.current.comments[0].body).toBe('new body')
    act(() => result.current.remove('1'))
    expect(result.current.comments.map((x) => x.id)).toEqual(['2'])
    act(() => result.current.clear())
    expect(result.current.comments).toEqual([])
  })

  it('persists across remount, isolated per session', () => {
    const first = renderHook(() => useReviewDraft('s1'))
    act(() => first.result.current.add(mk('1')))
    first.unmount()
    expect(renderHook(() => useReviewDraft('s1')).result.current.comments).toHaveLength(1)
    expect(renderHook(() => useReviewDraft('s2')).result.current.comments).toEqual([])
  })

  it('switches drafts when sessionId changes', () => {
    const { result, rerender } = renderHook(({ id }) => useReviewDraft(id), { initialProps: { id: 's1' } })
    act(() => result.current.add(mk('1')))
    rerender({ id: 's2' })
    expect(result.current.comments).toEqual([])
    rerender({ id: 's1' })
    expect(result.current.comments).toHaveLength(1)
  })

  it('treats corrupt storage as empty', () => {
    sessionStorage.setItem('viberpit.reviewDraft:s1', '{not json')
    const { result } = renderHook(() => useReviewDraft('s1'))
    expect(result.current.comments).toEqual([])
    sessionStorage.setItem('viberpit.reviewDraft:s1', '{"a":1}')
    expect(renderHook(() => useReviewDraft('s1')).result.current.comments).toEqual([])
  })

  it('drops malformed entries from a stored draft, keeping the well-formed ones', () => {
    const good = mk('ok')
    sessionStorage.setItem('viberpit.reviewDraft:s1', JSON.stringify([
      good,
      null,
      'text',
      { id: 'no-anchor', quote: ['x'], body: 'b' },
      { ...mk('bad-side'), anchor: { path: 'a.ts', side: 'both', start: 1, end: 1 } },
      { ...mk('bad-quote'), quote: [1] },
      { ...mk('bad-body'), body: 3 },
      { ...mk('plan'), anchor: { path: null, side: 'new', start: 2, end: 2 } },
    ]))
    const { result } = renderHook(() => useReviewDraft('s1'))
    expect(result.current.comments.map((c) => c.id)).toEqual(['ok', 'plan'])
  })

  it('writes storage only after commit, never from inside a state updater', () => {
    const { result } = renderHook(() => useReviewDraft('s1'))
    act(() => result.current.add(mk('1')))
    expect(JSON.parse(sessionStorage.getItem('viberpit.reviewDraft:s1') ?? '[]')).toHaveLength(1)
    act(() => result.current.clear())
    expect(sessionStorage.getItem('viberpit.reviewDraft:s1')).toBeNull()
  })
})
