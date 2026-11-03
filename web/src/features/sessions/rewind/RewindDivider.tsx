/**
 * Where a rewind (D-9) cut the transcript: `Rewound — N files restored`, with Undo
 * while the session can rewind (`undo`, from the live session's `RewindContext`). The
 * reducer drops it on the next prompt or the undo. Lazy (`rewindFlow`, through
 * `RewindDividerSlot`): it only ever follows a rewind, whose dialog loaded that chunk.
 */
import { useState } from 'react'
import type { RewindMarker } from '@/session/reducer'

export default function RewindDivider({ marker, undo }: {
  marker: RewindMarker; undo: ((rewindId: string) => Promise<void>) | null
}) {
  const [undoing, setUndoing] = useState(false)
  const n = marker.filesRestored.length
  return (
    <div className="flex items-center gap-2 my-2 px-2 text-xs text-ink-muted">
      <div className="flex-1 border-t border-edge" />
      <span>Rewound — {n} {n === 1 ? 'file' : 'files'} restored</span>
      {undo && (
        <button
          type="button"
          disabled={undoing}
          onClick={() => { setUndoing(true); void undo(marker.rewindId).finally(() => setUndoing(false)) }}
          className="rounded px-1.5 py-0.5 font-medium text-accent hover:bg-edge disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          Undo rewind
        </button>
      )}
      <div className="flex-1 border-t border-edge" />
    </div>
  )
}
