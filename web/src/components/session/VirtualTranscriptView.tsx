import { useRef, useMemo, useLayoutEffect } from 'react'
import { useVirtualizer } from '@tanstack/react-virtual'
import type { Turn as TurnType, RotationMarker } from '@/session/reducer'
import type { ToolDetailMode } from '@/session/groupBlocks'
import { Turn } from './Turn'
import { RotationDivider } from './RotationDivider'
import { flattenRows } from './transcriptRows'

interface Props {
  turns: TurnType[]
  rotations?: RotationMarker[]
  permissionMode: string
  toolDetailMode: ToolDetailMode
  streamingBlockId?: string | null
  onDecision: (permId: string, decision: 'allow' | 'deny' | 'always') => void
  onAnswer: (questionId: string, selected: string[]) => void
}

/**
 * SPIKE — virtualized transcript. Renders only the rows near the viewport using
 * @tanstack/react-virtual with dynamic measurement (handles collapsible blocks
 * and streaming growth). Stick-to-bottom is implemented by pinning scrollTop to
 * scrollHeight whenever the user is already at the bottom and the content grows.
 */
export function VirtualTranscriptView({
  turns, rotations = [], permissionMode, toolDetailMode, streamingBlockId = null, onDecision, onAnswer,
}: Props) {
  const parentRef = useRef<HTMLDivElement>(null)
  const atBottomRef = useRef(true)

  const rows = useMemo(() => flattenRows(turns, rotations), [turns, rotations])
  const lastTurnId = turns.length ? turns[turns.length - 1].id : null

  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 80,
    overscan: 6,
    getItemKey: (i) => rows[i].key,
  })

  const totalSize = virtualizer.getTotalSize()

  // Stick-to-bottom: when the user is already at the bottom, re-pin after every
  // layout (new rows, or the last row growing during streaming/expand).
  useLayoutEffect(() => {
    if (!atBottomRef.current) return
    const el = parentRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [rows.length, totalSize])

  function onScroll() {
    const el = parentRef.current
    if (!el) return
    atBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight <= 80
  }

  const items = virtualizer.getVirtualItems()

  return (
    <div
      ref={parentRef}
      onScroll={onScroll}
      className="flex-1 overflow-y-auto"
      data-testid="vscroll"
    >
      <div style={{ height: totalSize, position: 'relative', width: '100%' }}>
        {items.map((vi) => {
          const row = rows[vi.index]
          return (
            <div
              key={vi.key}
              data-index={vi.index}
              ref={virtualizer.measureElement}
              style={{ position: 'absolute', top: 0, left: 0, width: '100%', transform: `translateY(${vi.start}px)` }}
            >
              {row.kind === 'turn' ? (
                <Turn
                  turn={row.turn}
                  permissionMode={permissionMode}
                  isLastTurn={row.turn.id === lastTurnId}
                  toolDetailMode={toolDetailMode}
                  streamingBlockId={row.turn.id === lastTurnId ? streamingBlockId : null}
                  onDecision={onDecision}
                  onAnswer={onAnswer}
                />
              ) : (
                <RotationDivider from={row.rot.from} to={row.rot.to} reason={row.rot.reason} />
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
