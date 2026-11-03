/**
 * SessionAriaLive — sr-only aria-live region that mirrors the latest assistant
 * text for screen readers. Owns its own ref + effect (the region is an external
 * system; text is written imperatively, so there's no setState and no extra
 * render). Suppressed while replaying; announces "History loaded" once on
 * replayComplete. A hand-off — the mid-turn "Asked you" or a "Handed back" —
 * is announced once, as "<label> — <reason>", whenever that pair changes; never
 * on mount (the post-turn summary card in the transcript is a virtualized row,
 * which would re-announce on every remount, so the announcement lives here).
 *
 * Kept as a standalone component so the session runtime doesn't have to expose a
 * ref (which would make every runtime field access read as a ref during render).
 */

import { useEffect, useRef } from 'react'
import type { Turn } from '@/session/reducer'

/** A hand-off to announce: the summary card's label and the session's reason. */
export interface HandOffNotice {
  label: string
  reason: string
}

export function SessionAriaLive({ turns, replaying, handOff = null }: {
  turns: Turn[]
  replaying: boolean
  handOff?: HandOffNotice | null
}) {
  const liveRef = useRef<HTMLDivElement>(null)
  const prevReplayingRef = useRef(true)
  const handOffText = handOff ? `${handOff.label} — ${handOff.reason}` : null
  // The pair as it stood at mount: what the session already said is not news.
  const announcedRef = useRef(handOffText)

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

  // After the output effect, so a hand-off that changes in the same commit as the
  // turns is the line that is read.
  useEffect(() => {
    const el = liveRef.current
    if (!el || handOffText === announcedRef.current) return
    announcedRef.current = handOffText
    if (handOffText) el.textContent = handOffText
  }, [handOffText])

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
