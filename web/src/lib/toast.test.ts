import { describe, it, expect, beforeEach, vi } from 'vitest'
import { toast, dismissToast, getToasts, subscribe, __resetToasts } from './toast'

describe('toast store', () => {
  beforeEach(() => { __resetToasts(); vi.useRealTimers() })

  it('adds a toast and notifies subscribers', () => {
    const seen: number[] = []
    subscribe(() => seen.push(getToasts().length))
    const id = toast({ message: 'hi' })
    expect(getToasts()).toHaveLength(1)
    expect(getToasts()[0]).toMatchObject({ id, message: 'hi', variant: 'info' })
    expect(seen).toEqual([1])
  })

  it('dismiss removes by id', () => {
    const id = toast({ message: 'a' })
    toast({ message: 'b' })
    dismissToast(id)
    expect(getToasts().map((t) => t.message)).toEqual(['b'])
  })

  it('auto-dismisses after the default duration', () => {
    vi.useFakeTimers()
    toast({ message: 'temp' })
    expect(getToasts()).toHaveLength(1)
    vi.advanceTimersByTime(5000)
    expect(getToasts()).toHaveLength(0)
  })

  it('keeps a snapshot reference stable between changes', () => {
    toast({ message: 'x' })
    expect(getToasts()).toBe(getToasts())
  })
})
