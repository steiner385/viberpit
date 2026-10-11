import { useCallback, useEffect, useRef, useState, type RefObject } from 'react'
import { prefersReducedMotion } from '@/features/sessions/brailleSpinner'

/** Distance from the bottom (px) that still counts as "at the bottom". */
export const NEAR_BOTTOM_PX = 80
/**
 * Upper bound on how long a smooth scroll-to-bottom suppresses user-scroll
 * detection. A smooth scroll normally ends at the bottom (which clears the
 * suppression) or is cut short by a user gesture (which also clears it); this
 * is only the backstop for one that never reports either.
 */
const SMOOTH_GUARD_MS = 1000

interface Options {
  /** The overflow-y scroller. */
  scrollRef: RefObject<HTMLElement | null>
  /** A child whose height tracks the scrollable content; resizing it re-pins while stuck. */
  contentRef: RefObject<HTMLElement | null>
  /**
   * Any value that changes when the user sends a message. A change jumps to the
   * bottom and re-enables sticking, even when they had scrolled up to read.
   * Its initial value is ignored — mounting already lands at the bottom.
   */
  followSignal?: unknown
}

export interface StickToBottom {
  /** True while the view is following content down. Read it, never write it. */
  pinnedRef: RefObject<boolean>
  /** Drives the Scroll-to-bottom button (state, so React re-renders when it flips). */
  showScrollButton: boolean
  /**
   * The scroller's onScroll handler. Returns false when the event was one of our
   * own scrolls (and so says nothing about the user); true for a user scroll.
   */
  handleScroll: () => boolean
  /** Jump to the bottom and re-enable sticking. `smooth` animates unless reduced motion is on. */
  scrollToBottom: (opts?: { smooth?: boolean }) => void
  /** Set scrollTop without the resulting scroll event reading as a user gesture. */
  scrollProgrammatically: (el: HTMLElement, top: number) => void
  /** Flag this frame's scroll events as ours — for scrolls made by someone else's code (e.g. a virtualizer). */
  markProgrammaticScroll: () => void
}

/**
 * Stick-to-bottom for a chat-style scroller: follow new content down while the
 * user is at the bottom, stop the moment they scroll up, and show a
 * Scroll-to-bottom affordance until they come back.
 *
 *  - Only a real user gesture can unstick. Every scroll we make is flagged for
 *    the frame it lands in, because the browser delivers the scroll event
 *    asynchronously — after more content may have grown below — and it would
 *    otherwise read as "the user scrolled up".
 *  - A ResizeObserver on the content re-pins on every height change, not just
 *    when new messages arrive: markdown, fonts, images and expanding cards all
 *    lay out *after* React commits.
 *  - Mounting starts stuck, so opening a view (or switching to another
 *    conversation, which remounts it) lands at the bottom.
 */
export function useStickToBottom({ scrollRef, contentRef, followSignal }: Options): StickToBottom {
  const pinnedRef = useRef(true)
  const [showScrollButton, setShowScrollButton] = useState(false)
  // True while a scroll we initiated is in flight. Cleared on the next frame
  // (not on the scroll event) so it still self-clears when the change is a
  // no-op and fires no scroll event.
  const programmaticRef = useRef(false)
  // True while a smooth scroll-to-bottom is animating: its intermediate scroll
  // events are above the bottom by construction and must not unstick.
  const smoothRef = useRef(false)
  const smoothTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const endSmooth = useCallback(() => {
    smoothRef.current = false
    if (smoothTimerRef.current != null) {
      clearTimeout(smoothTimerRef.current)
      smoothTimerRef.current = null
    }
  }, [])

  const markProgrammaticScroll = useCallback(() => {
    programmaticRef.current = true
    requestAnimationFrame(() => { programmaticRef.current = false })
  }, [])

  const scrollProgrammatically = useCallback((el: HTMLElement, top: number) => {
    markProgrammaticScroll()
    el.scrollTop = top
  }, [markProgrammaticScroll])

  const handleScroll = useCallback(() => {
    const el = scrollRef.current
    if (!el || programmaticRef.current) return false
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight <= NEAR_BOTTOM_PX
    if (smoothRef.current) {
      if (!nearBottom) return false
      endSmooth()
    }
    if (nearBottom !== pinnedRef.current) setShowScrollButton(!nearBottom)
    pinnedRef.current = nearBottom
    return true
  }, [scrollRef, endSmooth])

  const scrollToBottom = useCallback((opts?: { smooth?: boolean }) => {
    const el = scrollRef.current
    if (!el) return
    pinnedRef.current = true
    setShowScrollButton(false)
    endSmooth()
    if (opts?.smooth && !prefersReducedMotion() && typeof el.scrollTo === 'function') {
      smoothRef.current = true
      smoothTimerRef.current = setTimeout(endSmooth, SMOOTH_GUARD_MS)
      el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
    } else {
      scrollProgrammatically(el, el.scrollHeight)
    }
  }, [scrollRef, endSmooth, scrollProgrammatically])

  // A user gesture during a smooth scroll cancels it (the browser stops the
  // animation); hand scroll detection straight back so that gesture can unstick.
  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    const events = ['wheel', 'touchstart', 'pointerdown', 'keydown'] as const
    for (const e of events) el.addEventListener(e, endSmooth, { passive: true })
    return () => {
      for (const e of events) el.removeEventListener(e, endSmooth)
      endSmooth()
    }
  }, [scrollRef, endSmooth])

  // Follow content all the way down — but only while stuck. Fires post-layout on
  // every height change of the content (including the initial observe).
  useEffect(() => {
    const content = contentRef.current
    if (!content) return
    const ro = new ResizeObserver(() => {
      if (!pinnedRef.current) return
      const el = scrollRef.current
      if (el) scrollProgrammatically(el, el.scrollHeight)
    })
    ro.observe(content)
    return () => ro.disconnect()
  }, [contentRef, scrollRef, scrollProgrammatically])

  // A send re-sticks: the user wants to see the message they just sent and its reply.
  const lastSignalRef = useRef(followSignal)
  useEffect(() => {
    if (Object.is(lastSignalRef.current, followSignal)) return
    lastSignalRef.current = followSignal
    scrollToBottom()
  }, [followSignal, scrollToBottom])

  return { pinnedRef, showScrollButton, handleScroll, scrollToBottom, scrollProgrammatically, markProgrammaticScroll }
}
