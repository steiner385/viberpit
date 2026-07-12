// ccswitch host wrapper for the ported plotroom pipeline island (spec §2, §6).
// focusedRepo is null until the PipelineSwitcher lands (Phase 2).
import { useCallback } from 'react'
import { useFixPr } from '@/api/hooks'
import { ApiBaseProvider } from './ApiBaseContext'
import { useDashboard } from './useDashboard'
import { PipelineView } from './PipelineView'
import './plotroom.css'

function Inner() {
  const fixPr = useFixPr()
  // Depend on the stable `fixPr.mutate` (not the whole `fixPr` object, which is a
  // fresh reference each render) so onFixPr stays reference-stable — PrRow's
  // areEqual identity-compares it, so an unstable ref would defeat memoization.
  const onFixPr = useCallback(
    (id: string, number: number) => fixPr.mutate({ id, number }),
    [fixPr.mutate], // eslint-disable-line react-hooks/exhaustive-deps -- .mutate is the stable handle; depending on `fixPr` would break the stable-ref contract above
  )
  const { state, stale } = useDashboard()
  return (
    <>
      {stale && <p className="pipeline-stale" role="status">data stale — reconnecting…</p>}
      <PipelineView state={state} focusedRepo={null} onFixPr={onFixPr} />
    </>
  )
}

export function PipelineTab() {
  return (
    <ApiBaseProvider base="/api/pipeline">
      <div className="prdash-root flex-1 min-h-0 overflow-y-auto">
        <Inner />
      </div>
    </ApiBaseProvider>
  )
}
