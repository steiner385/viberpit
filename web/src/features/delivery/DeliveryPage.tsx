/**
 * `/delivery`, `/delivery/automations/:automationId`, `/delivery/ci/:owner/:repo`
 * — one routed page, three detail levels chosen by the params (finish-line spec
 * §4.2, §4.4), plus the two welcome scopes chosen by `?scope=`. Modelled on
 * `features/roadmap/RoadmapPage.tsx`.
 */
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom'
import { useAutomations, useStageModels } from '@/api/hooks'
import { DELIVERY } from '@/features/products/vocab'
import { TierDot } from '@/features/roadmap/rows/TierDot'
import { useSearchParamState } from '@/lib/useSearchParamState'
import { useNow } from '@/hooks/useNow'
import { isValidTimestamp, relTime } from '@/lib/relativeTime'
import { sessionPath } from '@/session/sessionNavState'
import { useViewport } from '@/shell/useViewport'
import { Badge } from '@/ui/Badge'
import { Banner } from '@/ui/Banner'
import { Breadcrumb, type BreadcrumbItem } from '@/ui/Breadcrumb'
import { EmptyState } from '@/ui/EmptyState'
import { PageContent } from '@/ui/PageContent'
import { PageHeader } from '@/ui/PageHeader'
import { Select } from '@/ui/Select'
import { automationTier, automationTierLine } from './automationTier'
import { AutomationControls } from './automation/AutomationControls'
import { AutomationDetail } from './automation/AutomationDetail'
import { engineBadge } from './automation/engineBadge'
import { CapacityDetail } from './capacity/CapacityDetail'
import { CiRepoDetail, WINDOWS, type CiWindow } from './ci/CiRepoDetail'
import { deliveryOpenItem, deliveryPath, deliveryScope } from './deliveryNavState'
import { DeliveryWelcome } from './DeliveryWelcome'

export function DeliveryPage() {
  const { pathname, search } = useLocation()
  const navigate = useNavigate()
  const item = deliveryOpenItem(useParams())
  const scope = deliveryScope(pathname, search)

  // The shell navigation owns the list polling interval.
  const automations = useAutomations()
  const repos = useStageModels({ poll: false })

  const automationId = item?.kind === 'automation' ? item.automationId : null
  const automation = automationId
    ? automations.data?.find((a) => a.id === automationId) ?? null
    : null
  // The window is URL state so a shared link carries it; `replace`, because
  // changing a filter must not grow the Back stack. Declared unconditionally —
  // the CI branch is the only reader.
  const [ciWindow, setCiWindow] = useSearchParamState<CiWindow>('window', WINDOWS, '7d')

  // The address names an automation the list does not contain — a retired
  // origin or a stale bookmark. Gated on the list having ARRIVED: a failed
  // fetch also leaves `automation` null, and that is a broken connection.
  const missing = automationId !== null && automations.data !== undefined && automation === null

  const crumbs: BreadcrumbItem[] = [{ label: DELIVERY.label, to: deliveryPath() }]
  // Annotated: `DELIVERY.label` is a const literal, so an inferred `title` could
  // never be reassigned to an automation or repo name.
  let title: string = DELIVERY.label
  if (item?.kind === 'automation') {
    // The id is the fallback title, so a deep link to an automation the list has
    // not delivered yet still names something rather than rendering blank.
    title = automation?.name ?? item.automationId
    crumbs.push({ label: DELIVERY.automations, to: deliveryPath('automations') }, { label: title })
  } else if (item?.kind === 'ciRepo') {
    title = item.repo
    crumbs.push({ label: DELIVERY.ci, to: deliveryPath('ci') }, { label: title })
  }

  const hasObservation = isValidTimestamp(automation?.status.observedAt)
  const hasRefreshFailure = isValidTimestamp(automation?.status.lastRefreshFailedAt)
  const now = useNow(60_000, hasObservation || hasRefreshFailure)
  const engine = automation ? engineBadge(automation) : null
  const mode = automation?.mode ? automation.oversight?.modeDescriptions[automation.mode] : undefined

  // Below desktop the list column hides behind an open item (`app.list` in the
  // shell's matrix), so the page carries the way back to the scope's list. On
  // desktop the list is beside the page and a Back link would be a second
  // copy of it. Never from kiosk: this section is `showInKiosk: false`.
  const { mode: viewportMode } = useViewport()
  const back = item && viewportMode !== 'desktop'
    ? {
      to: deliveryPath(item.kind === 'ciRepo' ? 'ci' : 'automations'),
      label: `Back to ${item.kind === 'ciRepo' ? DELIVERY.ci : DELIVERY.automations}`,
    }
    : undefined

  return (
    <div className="flex h-full min-h-0 flex-col" data-page-variant="standard">
      <PageHeader
        title={title}
        back={back}
        meta={crumbs.length > 1 ? (
          <div className="flex flex-col gap-1">
            <Breadcrumb items={crumbs} />
            {/* R9: what this automation is doing, and what its mode means, as
                text rather than as a dot the reader has to decode. */}
            {/* The header never scrolls away, so it is budgeted: three short
                lines. Before this it was a fourteen-line wall on a phone that
                left the tab strip below the fold (2026-09-27 audit). */}
            {automation && (
              <div className="flex flex-col gap-0.5 text-xs text-ink-muted">
                <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
                  <TierDot tier={automationTier(automation)} />
                  <span>{automationTierLine(automationTier(automation), automation)}</span>
                  {/* Findability (spec D9): every capacity dial should point at
                      the Capacity page, including the runner header when it is
                      the reason nothing is happening. */}
                  {automation.id === 'backlog-runner' &&
                    (automation.status.waitingReason === 'capacity' || automation.status.text.includes('capacity')) && (
                    <Link to={deliveryPath('capacity')} className="underline">See capacity</Link>
                  )}
                  {engine && (
                    <Badge tone="info" title="Agent engine this automation's unattended sessions run on">
                      {engine}
                    </Badge>
                  )}
                </div>
                {/* Two lines at most: the runner's status carries a per-product
                    breakdown that the Queue tab tabulates in full, and the
                    title holds the whole string. */}
                <p className="line-clamp-2" title={automation.status.text}>{automation.status.text}</p>
                <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
                  {hasObservation && automation.status.observedAt && <span title={automation.status.observedAt}>Last observed {relTime(automation.status.observedAt, now || undefined)}</span>}
                  {hasRefreshFailure && automation.status.lastRefreshFailedAt && <span title={automation.status.lastRefreshFailedAt}>Refresh failed {relTime(automation.status.lastRefreshFailedAt, now || undefined)}</span>}
                  {/* The mode's name here; what it means is a sentence on the
                      Overview's identity block (and this span's title). */}
                  {(automation.operatorTriggered ?? automation.modes.length === 0)
                    ? <span>Invoked tool</span>
                    : <span title={mode}>Configured mode: {automation.mode ?? 'unknown'}</span>}
                </div>
              </div>
            )}
          </div>
        ) : undefined}
        actions={
          automation
            ? <AutomationControls automation={automation} />
            : item?.kind === 'ciRepo'
              ? (
                <Select
                  aria-label="Window"
                  size="sm"
                  value={ciWindow}
                  onChange={(e) => setCiWindow(e.target.value as CiWindow)}
                >
                  {WINDOWS.map((w) => <option key={w} value={w}>{w}</option>)}
                </Select>
              )
              : undefined
        }
      />
      {/* The CI detail and Capacity detail each own their own skeleton, so
          only the panes that read the two list queries wait here. */}
      <PageContent
        busy={item?.kind === 'ciRepo' || scope === 'capacity'
          ? false
          : scope === 'ci'
            ? repos.isLoading && !repos.data
            : automations.isLoading && !automations.data}
      >
        {/* The list column's banner is not on screen while a detail route is
            open (and is hidden entirely on phone/tablet), so a fetch failure
            needs its own surface here — the CI equivalent already lives
            inside `CiRepoDetail`. */}
        {automations.isError && item?.kind === 'automation' && (
          <Banner tone="danger" title={DELIVERY.automationsError} />
        )}
        {automation && (
          // Keyed by automation id: without this, the router reuses the same
          // `AutomationDetail` instance across a param-only route change (the
          // `<Outlet/>` this page renders under is un-keyed), so a config or
          // schedule edit made just before switching automations could patch
          // the WRONG one (#1468).
          <AutomationDetail
            key={automation.id}
            automation={automation}
            automations={automations.data}
            onOpenSession={(id) => navigate(sessionPath(id))}
          />
        )}
        {missing && (
          <EmptyState
            title="Automation not found"
            description="No automation with this id is registered. It may have been renamed or retired."
            action={(
              <Link className="text-accent hover:underline" to={deliveryPath()}>
                Back to {DELIVERY.label.toLowerCase()}
              </Link>
            )}
          />
        )}
        {item?.kind === 'ciRepo' && <CiRepoDetail repo={item.repo} window={ciWindow} automations={automations.data} />}
        {/* Capacity has no per-item route — this scope is always "nothing
            open", and its pane is the controls/table/plan, never the welcome. */}
        {item === null && scope === 'capacity' && <CapacityDetail />}
        {/* Nothing open: the pane says what is waiting rather than sitting blank. */}
        {item === null && scope !== 'capacity' && (
          <DeliveryWelcome
            scope={scope}
            count={scope === 'ci'
              ? Object.keys(repos.data ?? {}).length
              : automations.data?.length ?? 0}
            isError={scope === 'ci' ? repos.isError : automations.isError}
          />
        )}
      </PageContent>
    </div>
  )
}
