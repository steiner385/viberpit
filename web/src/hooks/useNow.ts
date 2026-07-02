import { useEffect, useState } from 'react'
import { usePageVisible } from './usePageVisible'

/**
 * Current Date.now() ms, advanced every `intervalMs` while the tab is visible
 * and `active`, and frozen while hidden or inactive (composes usePageVisible —
 * no second visibility listener). Returns 0 until the first post-mount tick so
 * render stays pure (no Date.now() during render).
 */
export function useNow(intervalMs = 1000, active = true): number {
  const visible = usePageVisible()
  const [now, setNow] = useState(0)
  useEffect(() => {
    if (!visible || !active) return
    // Schedule catch-up as a microtask-safe timer callback (avoids synchronous
    // setState-in-effect lint error while still firing before the first interval).
    const catchUpId = setTimeout(() => setNow(Date.now()), 0)
    const id = setInterval(() => setNow(Date.now()), intervalMs)
    return () => {
      clearTimeout(catchUpId)
      clearInterval(id)
    }
  }, [visible, active, intervalMs])
  return now
}
