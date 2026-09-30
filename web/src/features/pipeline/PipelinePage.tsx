import { useCallback, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Bell, BellOff, HelpCircle } from 'lucide-react'
import { useFixPr } from '@/api/hooks'
import { ApiBaseProvider } from '@/pipeline/ApiBaseContext'
import { useDashboard } from '@/pipeline/useDashboard'
import { HealthHeader } from '@/pipeline/HealthHeader'
import { LegendPanel } from '@/pipeline/LegendPanel'
import { PipelineView } from '@/pipeline/PipelineView'
import type { Bucket } from '@/pipeline/StatusStrip'
import type { LaneStatus } from '@/pipeline/types'
import { Banner } from '@/ui/Banner'
import { IconButton } from '@/ui/IconButton'
import { PageContent } from '@/ui/PageContent'
import { PageHeader } from '@/ui/PageHeader'
import { Skeleton } from '@/ui/Skeleton'
import { Tabs } from '@/ui/Tabs'
import { useViewport } from '@/shell/useViewport'
import { deliveryPath } from '@/features/delivery/deliveryNavState'
// The charts are the one allowlisted stylesheet (geometry, not palette); every
// other pipeline rule is a token utility on its component.
import '@/features/pipeline/charts/plotroom.css'
import { laneJumpRepo, laneJumpTarget } from './laneJump'
import { useFocusedPipeline } from './useFocusedPipeline'
import { useRepoCycle } from './useRepoCycle'

/** How long a wall display shows each repo when the URL gives no `cycle`.
 *  `shell/kiosk.ts` already defaults the parsed config; this only covers the
 *  impossible-in-practice case of kiosk mode with no kiosk config object. */
const FALLBACK_CYCLE_SECONDS = 30

/** "as of 14:02" — the frame's `generatedAt` in the viewer's local time. */
function AsOf({ iso }: { iso: string }) {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return null
  return (
    <time dateTime={iso} className="whitespace-nowrap text-xs text-ink-muted" data-testid="pipeline-as-of">
      as of {d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', hour12: false })}
    </time>
  )
}

/**
 * `/pipeline` — the first Workspace-variant page (page-migration §2.2): the
 * health band is the header's `meta`, the bell and legend are its `actions`,
 * the repo strip is `page.subnav`, and the board owns its own scroll under a
 * non-scrolling `PageContent`.
 */
export function PipelinePage() {
  return (
    <ApiBaseProvider base="/api/pipeline">
      <Inner />
    </ApiBaseProvider>
  )
}

function Inner() {
  const navigate = useNavigate()
  // Kiosk is read ONCE by the shell (`AppShell` → `ViewportProvider`); the page
  // must never re-parse the URL, or a mid-session `?kiosk=1` could flip the
  // interaction model under a user's hands.
  const { mode, kiosk, portrait } = useViewport()
  const isKiosk = mode === 'kiosk'
  const fixPr = useFixPr()
  // Depend on the stable `fixPr.mutate` (not the whole `fixPr` object, a fresh
  // ref each render) so onFixPr stays reference-stable — PrRow's areEqual
  // identity-compares it, so an unstable ref would defeat memoization.
  const onFixPr = useCallback(
    (id: string, number: number) => fixPr.mutate({ id, number }),
    [fixPr.mutate], // eslint-disable-line react-hooks/exhaustive-deps -- .mutate is the stable handle; depending on `fixPr` would break the stable-ref contract above
  )
  const { state, connected, stale, notifySupported, notifyEnabled, toggleNotify } = useDashboard()
  const repos = useMemo(() => (state?.repos ?? []).map((r) => r.repo), [state])
  // In kiosk the focus is not written to the URL: the wall display's URL is its
  // configuration and must survive a reload unchanged.
  const [focused, focus] = useFocusedPipeline(repos, true, !isKiosk)
  useRepoCycle(isKiosk, kiosk?.cycleSeconds ?? FALLBACK_CYCLE_SECONDS, repos, focused, focus)
  const [filter, setFilter] = useState<Bucket | null>(null)
  const [legendOpen, setLegendOpen] = useState(false)
  const legendBtnRef = useRef<HTMLButtonElement>(null)
  const onJumpToLane = useCallback((laneId: string | null, status: LaneStatus | null) => {
    const t = laneJumpTarget(laneId, status)
    if (t.kind !== 'filter') { navigate(deliveryPath('ci')); return }
    setFilter(t.bucket)
    // The lanes are cross-repo, the strip and rows are the focused repo's — so
    // the jump also focuses the repo that owns what the lane is talking about
    // (`laneJumpRepo`: stays put when the focused repo already does).
    if (t.bucket && state) {
      const owner = laneJumpRepo(laneId, status, t.bucket, state.repos, focused)
      if (owner) focus(owner)
    }
  }, [navigate, state, focused, focus])

  return (
    <div className="flex h-full min-h-0 flex-col" data-page-variant="workspace">
      <PageHeader
        title="Pipeline"
        meta={state ? (
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <HealthHeader state={state} onJumpToLane={onJumpToLane} />
            {/* When this frame was computed. Every number on the board — a
                queue's "~16m left" most of all — is only as fresh as this, and a
                wall display with a stalled feed had no way to say so beyond a
                banner (2026-09-30 Pipeline critique, heuristic 1 / P2). */}
            <AsOf iso={state.generatedAt} />
          </div>
        ) : undefined}
        actions={(
          <>
            {notifySupported && (
              <IconButton
                aria-label="Browser notifications (this tab)"
                aria-pressed={notifyEnabled}
                title={notifyEnabled
                  ? 'Browser notifications on (this tab only — tab must stay open).'
                  : 'Enable browser notifications (this tab only — tab must stay open).'}
                onClick={toggleNotify}
              >
                {notifyEnabled ? <Bell size={18} aria-hidden="true" /> : <BellOff size={18} aria-hidden="true" />}
              </IconButton>
            )}
            <IconButton
              ref={legendBtnRef}
              aria-label="Legend"
              title="Legend — what every shape, color, and term on the board means"
              aria-haspopup="dialog"
              aria-expanded={legendOpen}
              onClick={() => setLegendOpen(true)}
            >
              <HelpCircle size={18} aria-hidden="true" />
            </IconButton>
          </>
        )}
      />
      {/* page.subnav: one tab per tracked repo. Not rendered in kiosk — the
          repo cycle drives focus there, and a wall display has no one to tap. */}
      {!isKiosk && repos.length > 0 && (
        <Tabs
          label="Pipeline repository"
          items={repos.map((r) => ({ id: r, label: r }))}
          value={focused ?? repos[0]!}
          onChange={focus}
          className="shrink-0 px-4 md:px-6"
        />
      )}
      <PageContent scroll={false} busy={!state}>
        {stale && !state && !connected && (
          <Banner
            tone="danger"
            role="status"
            title="Pipeline feed unavailable — reconnecting…"
            className="mx-4 mt-3 shrink-0 md:mx-6"
          />
        )}
        {stale && (state || connected) && (
          <Banner
            tone="warning"
            role="status"
            title={state ? 'Pipeline feed stalled — showing the last frame.' : 'Pipeline feed stalled — waiting for the first frame.'}
            className="mx-4 mt-3 shrink-0 md:mx-6"
          />
        )}
        {!state
          ? (
            <div role="status" className="space-y-3 p-4 md:p-6">
              <span className="sr-only">Loading pipeline…</span>
              <div className="grid grid-cols-3 gap-2 md:grid-cols-6">
                {Array.from({ length: 6 }, (_, i) => <Skeleton key={i} lines={2} />)}
              </div>
              <Skeleton lines={6} />
            </div>
          )
          : (
            <div className="flex min-h-0 flex-1 flex-col">
              <PipelineView
                state={state}
                focusedRepo={focused}
                onFixPr={onFixPr}
                activeFilter={filter}
                onFilter={setFilter}
                portrait={portrait}
              />
            </div>
          )}
      </PageContent>
      <LegendPanel open={legendOpen} onClose={() => setLegendOpen(false)} returnFocusRef={legendBtnRef} />
    </div>
  )
}
