import { renderHook, act } from '@testing-library/react'
import { describe, it, expect, beforeEach } from 'vitest'
import { storageKey } from '@/lib/storageKey'
import { useSessionDraft } from '../useSessionDraft'

const KEY = storageKey('draft.s1')
const LEGACY_KEY = 'ccswitch:draft:s1'

beforeEach(() => localStorage.clear())

describe('useSessionDraft', () => {
  it('reads an existing draft on mount', () => {
    localStorage.setItem(KEY, JSON.stringify({ text: 'hi', attachments: [{ id: 'a', mime: 'image/png', name: 's.png' }] }))
    const { result } = renderHook(() => useSessionDraft('s1'))
    expect(result.current.initial.text).toBe('hi')
    expect(result.current.initial.attachments[0].id).toBe('a')
  })

  it('save() round-trips and clear() removes the key', () => {
    const { result } = renderHook(() => useSessionDraft('s1'))
    act(() => result.current.save({ text: 'draft', attachments: [] }))
    expect(JSON.parse(localStorage.getItem(KEY)!).text).toBe('draft')
    act(() => result.current.clear())
    expect(localStorage.getItem(KEY)).toBeNull()
  })

  it('save() of an empty draft removes the key', () => {
    localStorage.setItem(KEY, JSON.stringify({ text: 'old', attachments: [] }))
    const { result } = renderHook(() => useSessionDraft('s1'))
    act(() => result.current.save({ text: '', attachments: [] }))
    expect(localStorage.getItem(KEY)).toBeNull()
  })

  it('no sessionId is a no-op (empty initial, no writes)', () => {
    const { result } = renderHook(() => useSessionDraft(undefined))
    expect(result.current.initial).toEqual({ text: '', attachments: [] })
    act(() => result.current.save({ text: 'x', attachments: [] }))
    expect(localStorage.length).toBe(0)
  })

  it('migrates the legacy ccswitch:draft:<id> key once, per session id', () => {
    localStorage.setItem(LEGACY_KEY, JSON.stringify({ text: 'legacy draft', attachments: [] }))
    const { result } = renderHook(() => useSessionDraft('s1'))
    expect(result.current.initial.text).toBe('legacy draft')
    expect(JSON.parse(localStorage.getItem(KEY)!).text).toBe('legacy draft')
    expect(localStorage.getItem(LEGACY_KEY)).toBeNull()
  })
})
