import { useRef, useEffect } from 'react'
import type { Turn as TurnType } from '@/session/reducer'
import { Turn } from './Turn'

interface Props {
  turns: TurnType[]
  permissionMode: string
  onDecision: (permId: string, decision: 'allow' | 'deny' | 'always') => void
  onAnswer: (questionId: string, selected: string[]) => void
}

export function TranscriptView({ turns, permissionMode, onDecision, onAnswer }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const endRef = useRef<HTMLDivElement>(null)
  // Track whether the user is at/near the bottom so we auto-scroll only when appropriate.
  const isNearBottomRef = useRef(true)

  // Update isNearBottom on scroll
  function handleScroll() {
    const el = containerRef.current
    if (!el) return
    const threshold = 80 // px from bottom counts as "near bottom"
    isNearBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight <= threshold
  }

  // Auto-scroll the container (not the sentinel node) when turns change
  useEffect(() => {
    if (!isNearBottomRef.current) return
    const el = containerRef.current
    if (el) {
      el.scrollTop = el.scrollHeight
    }
  }, [turns])

  return (
    <div
      ref={containerRef}
      onScroll={handleScroll}
      className="flex-1 overflow-y-auto"
    >
      {turns.map((turn) => (
        <Turn
          key={turn.id}
          turn={turn}
          permissionMode={permissionMode}
          onDecision={onDecision}
          onAnswer={onAnswer}
        />
      ))}
      {/* Bottom sentinel for future IntersectionObserver use */}
      <div ref={endRef} className="h-1" aria-hidden="true" />
    </div>
  )
}
