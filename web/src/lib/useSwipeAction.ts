import { useCallback, useRef, useState } from 'react'
import { settleSwipe, shouldLockHorizontal, type SwipeSide } from './swipeMath'

export const SWIPE_REVEAL_PX = 64
export const SWIPE_COMMIT_FRACTION = 0.45

export interface UseSwipeActionOptions {
  /** When false, the gesture is inert (desktop). */
  enabled: boolean
  /** Fired when a drag commits an edge action. */
  onCommit: (side: SwipeSide) => void
  revealPx?: number
  commitFraction?: number
}

export interface SwipeApi {
  /** Live horizontal offset while dragging (px). 0 when at rest. */
  dx: number
  /** Side currently held open (revealed), or null. */
  openSide: SwipeSide | null
  dragging: boolean
  onPointerDown: (e: React.PointerEvent<HTMLElement>) => void
  /** Snap closed (e.g. after the revealed button is tapped). */
  reset: () => void
  /** True (once) if the last pointer interaction was a real horizontal drag,
   *  so the trailing click should be ignored rather than opening the session. */
  consumeDrag: () => boolean
}

export function useSwipeAction({
  enabled,
  onCommit,
  revealPx = SWIPE_REVEAL_PX,
  commitFraction = SWIPE_COMMIT_FRACTION,
}: UseSwipeActionOptions): SwipeApi {
  const [dx, setDx] = useState(0)
  const [openSide, setOpenSide] = useState<SwipeSide | null>(null)
  const [dragging, setDragging] = useState(false)
  const g = useRef({ startX: 0, startY: 0, width: 0, locked: false, active: false, didDrag: false })

  const reset = useCallback(() => { setDx(0); setOpenSide(null) }, [])

  const consumeDrag = useCallback(() => {
    const v = g.current.didDrag
    g.current.didDrag = false
    return v
  }, [])

  const onPointerDown = useCallback((e: React.PointerEvent<HTMLElement>) => {
    if (!enabled) return
    const s = g.current
    // Ignore a re-entrant pointerdown while a gesture is already in flight (a
    // second finger, or a synthetic re-trigger): re-arming here would register a
    // second window-listener triplet whose cleanup reference is lost, leaking the
    // first set for the life of the page. The active gesture owns the pointer
    // until its own pointerup/pointercancel/vertical-abort runs cleanup.
    if (s.active) return
    s.startX = e.clientX
    s.startY = e.clientY
    s.width = e.currentTarget.offsetWidth
    s.locked = false
    s.active = true
    s.didDrag = false

    const cleanup = () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
      window.removeEventListener('pointercancel', onCancel)
    }
    function onMove(ev: PointerEvent) {
      if (!s.active) return
      const ddx = ev.clientX - s.startX
      const ddy = ev.clientY - s.startY
      if (!s.locked) {
        if (shouldLockHorizontal(ddx, ddy)) {
          s.locked = true
          setDragging(true)
        } else if (shouldLockHorizontal(ddy, ddx)) {
          // Vertical intent — yield to the list's native scroll, abandon gesture.
          s.active = false
          cleanup()
          return
        } else {
          return // still ambiguous; wait for more travel
        }
      }
      s.didDrag = true
      setDx(ddx)
    }
    function onUp(ev: PointerEvent) {
      if (!s.active) { cleanup(); return }
      s.active = false
      setDragging(false)
      const finalDx = ev.clientX - s.startX
      const { outcome, side } = settleSwipe({ dx: finalDx, width: s.width, revealPx, commitFraction })
      if (outcome === 'commit' && side) { setDx(0); setOpenSide(null); onCommit(side) }
      else if (outcome === 'open' && side) { setDx(0); setOpenSide(side) }
      else { setDx(0); setOpenSide(null) }
      cleanup()
    }
    function onCancel() {
      s.active = false
      setDragging(false)
      setDx(0)
      cleanup()
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
    window.addEventListener('pointercancel', onCancel)
  }, [enabled, onCommit, revealPx, commitFraction])

  return { dx, openSide, dragging, onPointerDown, reset, consumeDrag }
}
