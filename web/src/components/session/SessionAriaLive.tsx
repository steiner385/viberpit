/**
 * SessionAriaLive — sr-only aria-live region that mirrors the latest assistant
 * text for screen readers. Owns its own ref + effect (the region is an external
 * system; text is written imperatively, so there's no setState and no extra
 * render). Suppressed while replaying; announces "History loaded" once on
 * replayComplete.
 *
 * Kept as a standalone component so the session runtime doesn't have to expose a
 * ref (which would make every runtime field access read as a ref during render).
 */

import { useEffect, useRef } from 'react'
import type { Turn } from '@/session/reducer'

export function SessionAriaLive({ turns, replaying }: { turns: Turn[]; replaying: boolean }) {
  const liveRef = useRef<HTMLDivElement>(null)
  const prevReplayingRef = useRef(true)

  useEffect(() => {
    const el = liveRef.current
    if (!el) return
    if (replaying) {
      prevReplayingRef.current = true
      return
    }
    if (prevReplayingRef.current) {
      prevReplayingRef.current = false
      el.textContent = 'History loaded'
      return
    }
    const lastAssistantTurn = [...turns].reverse().find((t) => t.role === 'assistant')
    if (!lastAssistantTurn) return
    const lastTextBlock = [...lastAssistantTurn.blocks].reverse().find((b) => b.kind === 'text')
    if (lastTextBlock && lastTextBlock.kind === 'text') {
      // Mirror last ~200 chars (enough for a screen reader without flooding)
      el.textContent = lastTextBlock.text.slice(-200)
    }
  }, [turns, replaying])

  return (
    <div
      ref={liveRef}
      aria-live="polite"
      aria-atomic="false"
      aria-label="Session output"
      className="sr-only"
    />
  )
}
