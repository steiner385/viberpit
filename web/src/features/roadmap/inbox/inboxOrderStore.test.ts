// @vitest-environment node
// The store the Inbox column publishes its rendered order through. No DOM: the
// point of the module is that it is plain state two components can share.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { clearInboxOrder, getInboxOrder, setInboxOrder, subscribe } from './inboxOrderStore'

describe('inboxOrderStore', () => {
  beforeEach(() => { clearInboxOrder() })

  it('hands back the order that was published', () => {
    setInboxOrder(['a', 'b', 'c'])
    expect([...getInboxOrder()]).toEqual(['a', 'b', 'c'])
  })

  it('keeps a copy, so a caller mutating its array cannot rewrite the snapshot', () => {
    const ids = ['a', 'b']
    setInboxOrder(ids)
    ids.push('c')
    expect([...getInboxOrder()]).toEqual(['a', 'b'])
  })

  it('notifies subscribers on a change and stops after unsubscribe', () => {
    const seen = vi.fn()
    const off = subscribe(seen)
    setInboxOrder(['a'])
    expect(seen).toHaveBeenCalledTimes(1)
    off()
    setInboxOrder(['a', 'b'])
    expect(seen).toHaveBeenCalledTimes(1)
  })

  it('is a no-op for a shallow-equal order — a fresh array of the same ids', () => {
    // The column re-derives its id array on every render; re-publishing it must
    // not churn the snapshot `useSyncExternalStore` compares by identity.
    setInboxOrder(['a', 'b'])
    const before = getInboxOrder()
    const seen = vi.fn()
    subscribe(seen)
    setInboxOrder(['a', 'b'])
    expect(seen).not.toHaveBeenCalled()
    expect(getInboxOrder()).toBe(before)
    // A different sequence of the same ids IS a change.
    setInboxOrder(['b', 'a'])
    expect(seen).toHaveBeenCalledTimes(1)
  })

  it('clears back to empty, which is how the pane knows no column is rendering', () => {
    setInboxOrder(['a'])
    const seen = vi.fn()
    subscribe(seen)
    clearInboxOrder()
    expect([...getInboxOrder()]).toEqual([])
    expect(seen).toHaveBeenCalledTimes(1)
    // Already empty: nothing to tell anyone.
    clearInboxOrder()
    expect(seen).toHaveBeenCalledTimes(1)
  })
})
