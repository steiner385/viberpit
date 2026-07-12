/* eslint-disable react-refresh/only-export-components -- laneJumpTarget is a pure
   mapping colocated with (and unit-tested against) PipelineTab; a separate module
   for one helper would be over-splitting. The Tab isn't a fast-refresh boundary. */
// ccswitch host wrapper for the ported plotroom pipeline island (spec §2, §5, §6).
// Composes the ported chrome — PipelineSwitcher, HealthHeader, LegendPanel, the
// notification bell, kiosk auto-cycle — around the ported PipelineView.
import { useCallback, useMemo, useRef, useState } from 'react'
import { useFixPr } from '@/api/hooks'
import { ApiBaseProvider } from './ApiBaseContext'
import { useDashboard } from './useDashboard'
import { PipelineView } from './PipelineView'
import { HealthHeader } from './HealthHeader'
import { LegendPanel } from './LegendPanel'
import { PipelineSwitcher, useFocusedPipeline } from './PipelineSwitcher'
import { readKioskConfig } from './kiosk'
import { useKioskCycle } from './useKioskCycle'
import type { Bucket } from './StatusStrip'
import type { LaneStatus } from './types'
import './plotroom.css'

/** Lane chip → richest existing ccswitch surface (spec §5): the Pipeline status
 *  filter for the PR-flow lanes, the Insights tab for the rest until richer
 *  targets exist. Pure so PipelineTab can unit-test the mapping. */
export function laneJumpTarget(laneId: string | null, status: LaneStatus | null):
  { kind: 'filter'; bucket: Bucket | null } | { kind: 'insights' } {
  switch (laneId) {
    case 'pr-ci': return { kind: 'filter', bucket: status === 'red' ? 'failed' : 'running' }
    case 'merge-queue': return { kind: 'filter', bucket: 'queued' }
    case 'deploy': return { kind: 'filter', bucket: 'deploy' }
    case 'failures': return { kind: 'filter', bucket: 'failed' }
    default: return { kind: 'insights' } // main / scheduled / cost / null
  }
}

/** ccswitch tabs are component-state driven (not URL/hash-routed), so the host
 *  shell passes a callback to switch to the Insights tab for the lane-jumps that
 *  land there. */
function Inner({ onOpenInsights }: { onOpenInsights?: () => void }) {
  const fixPr = useFixPr()
  // Depend on the stable `fixPr.mutate` (not the whole `fixPr` object, a fresh
  // ref each render) so onFixPr stays reference-stable — PrRow's areEqual
  // identity-compares it, so an unstable ref would defeat memoization.
  const onFixPr = useCallback(
    (id: string, number: number) => fixPr.mutate({ id, number }),
    [fixPr.mutate], // eslint-disable-line react-hooks/exhaustive-deps -- .mutate is the stable handle; depending on `fixPr` would break the stable-ref contract above
  )
  const { state, stale, notifySupported, notifyEnabled, toggleNotify } = useDashboard()
  const [{ kiosk, cycleSeconds }] = useState(readKioskConfig)
  const repos = useMemo(() => (state?.repos ?? []).map((r) => r.repo), [state])
  const [focused, focus] = useFocusedPipeline(repos, true, !kiosk)
  useKioskCycle(kiosk, cycleSeconds, repos, focused, focus)
  const [filter, setFilter] = useState<Bucket | null>(null)
  const [legendOpen, setLegendOpen] = useState(false)
  const legendBtnRef = useRef<HTMLButtonElement>(null)
  const onJumpToLane = useCallback((laneId: string | null, status: LaneStatus | null) => {
    const t = laneJumpTarget(laneId, status)
    if (t.kind === 'filter') setFilter(t.bucket)
    else onOpenInsights?.()
  }, [onOpenInsights])
  return (
    <>
      {!kiosk && (
        <div className="pipeline-chrome-bar">
          <PipelineSwitcher repos={repos} focused={focused} onFocus={focus} />
          {notifySupported && (
            <button
              type="button"
              className="notify-bell"
              aria-pressed={notifyEnabled}
              aria-label="Browser notifications (this tab)"
              title={notifyEnabled
                ? 'Browser notifications on (this tab only — tab must stay open).'
                : 'Enable browser notifications (this tab only — tab must stay open).'}
              onClick={toggleNotify}
            >
              <span aria-hidden="true">{notifyEnabled ? '🔔' : '🔕'}</span>
            </button>
          )}
          <button
            type="button"
            ref={legendBtnRef}
            className="legend-btn"
            aria-label="Legend"
            title="Legend — what every shape, color, and term on the board means"
            aria-haspopup="dialog"
            aria-expanded={legendOpen}
            onClick={() => setLegendOpen(true)}
          >
            <span aria-hidden="true">?</span>
          </button>
        </div>
      )}
      {state && <HealthHeader state={state} onJumpToLane={onJumpToLane} />}
      {stale && <p className="pipeline-stale" role="status">data stale — reconnecting…</p>}
      <LegendPanel open={legendOpen} onClose={() => setLegendOpen(false)} returnFocusRef={legendBtnRef} />
      <PipelineView state={state} focusedRepo={focused} onFixPr={onFixPr}
        activeFilter={filter} onFilter={setFilter} kiosk={kiosk} />
    </>
  )
}

export function PipelineTab({ onOpenInsights }: { onOpenInsights?: () => void } = {}) {
  return (
    <ApiBaseProvider base="/api/pipeline">
      <div className="prdash-root flex-1 min-h-0 overflow-y-auto">
        <Inner onOpenInsights={onOpenInsights} />
      </div>
    </ApiBaseProvider>
  )
}
