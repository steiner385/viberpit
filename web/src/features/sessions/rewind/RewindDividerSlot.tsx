import { lazy, Suspense } from 'react'
import type { RewindMarker } from '@/session/reducer'
import { useRewindActions } from './rewindContext'
import { loadRewindFlow } from './useRewind'

const RewindDivider = lazy(() => loadRewindFlow().then((m) => ({ default: m.RewindDivider })))

/** The transcript's rewind row (D-9): the lazy divider, with Undo where the session can rewind. */
export function RewindDividerSlot({ marker }: { marker: RewindMarker }) {
  const undo = useRewindActions()?.undo ?? null
  return <Suspense fallback={null}><RewindDivider marker={marker} undo={undo} /></Suspense>
}
