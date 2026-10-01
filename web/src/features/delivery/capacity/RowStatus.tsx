/**
 * Inline save feedback for an immediate-write control (spec 2026-09-04 §7 —
 * "immediate rows [get] `role=\"status\"` feedback"): "Saving…" while the
 * mutation is pending, "Saved" for 3 s after it resolves, "Failed: <message>"
 * while it is in an error state. One component so the push-level radio, the
 * governor mode select, each cap-mode switch and each rank button pair render
 * the same feedback shape rather than four hand-rolled copies.
 *
 * The control itself needs no separate "revert on failure" logic: every
 * control this sits beside is rendered from server data (`readout.pushLevel`,
 * `governor.mode`, `product.capacityMode`), never from local optimistic
 * state, so a failed mutate leaves it showing the value the server actually
 * holds — this component only has to say that the write failed.
 */
import { useEffect, useState } from 'react'
import { extractMutationErrorMessage } from '@/api/queryClient'

export type SaveStatus = 'idle' | 'pending' | 'success' | 'error'

export interface RowStatusProps {
  status: SaveStatus
  error?: unknown
}

/** Any TanStack Query mutation result exposes at least this much — the shape
 *  every call site spreads directly into `<RowStatus status=… error=… />`
 *  (`{ status: mutation.status, error: mutation.error }`) rather than through
 *  a second exported helper: this file exports only the component (plus pure
 *  types, which Fast Refresh already ignores), so it never mixes a plain
 *  value export in with the component the way an exported non-PascalCase
 *  function would. */
export interface MutationLike {
  status: SaveStatus
  error?: unknown
}

const SAVED_DISPLAY_MS = 3_000

export function RowStatus({ status, error }: RowStatusProps) {
  // "Saved" is shown for exactly 3s after a success, then nothing. Every
  // `setState` below runs inside a `setTimeout` callback — including the
  // "become true" one, deferred 0ms — never as a direct statement in the
  // effect body: the same "catch-up" technique `hooks/useNow.ts` documents,
  // because a setState called synchronously and unconditionally at the top
  // of an effect is exactly what `react-hooks/set-state-in-effect` forbids,
  // and there is no external system here to justify one anyway.
  const [showSaved, setShowSaved] = useState(false)
  useEffect(() => {
    if (status !== 'success') {
      const off = setTimeout(() => setShowSaved(false), 0)
      return () => clearTimeout(off)
    }
    const on = setTimeout(() => setShowSaved(true), 0)
    const off = setTimeout(() => setShowSaved(false), SAVED_DISPLAY_MS)
    return () => { clearTimeout(on); clearTimeout(off) }
  }, [status])

  // The `role="status"` span is ALWAYS mounted, empty or not (#2032
  // deferred-minor): a live region that pops into existence already carrying
  // its text is exactly the case NVDA/VoiceOver most often miss, because
  // nothing was there yet for the AT to have registered as a live region —
  // mounting it once and only ever mutating its text content is what makes
  // the announcement reliable.
  let text = ''
  let tone = 'text-ink-muted'
  if (status === 'pending') {
    text = 'Saving…'
  } else if (status === 'error') {
    text = `Failed: ${extractMutationErrorMessage(error)}`
    tone = 'text-danger'
  } else if (showSaved) {
    text = 'Saved'
    tone = 'text-success'
  }
  // `aria-live` spelled out although `role="status"` implies it: the always-
  // mounted empty span reads as an orphan live region to an auditor otherwise
  // (2026-10-01 Delivery critique, P3: "15 empty role=status without aria-live").
  return <span role="status" aria-live="polite" className={`text-xs ${tone}`}>{text}</span>
}
