/**
 * `/delivery`, `/delivery/automations/:automationId`, `/delivery/ci/:owner/:repo`
 * — one routed page, three detail levels chosen by the params (finish-line spec
 * §4.2, §4.4), plus the two welcome scopes chosen by `?scope=`. Modelled on
 * `features/roadmap/RoadmapPage.tsx`.
 */
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom'
import { useAutomationActionable, useAutomations, useStageModels } from '@/api/hooks'
import { DELIVERY, MODE_DESCRIPTION } from '@/features/products/vocab'
import { TierDot } from '@/features/roadmap/rows/TierDot'
import { useSearchParamState } from '@/lib/useSearchParamState'
import { sessionPath } from '@/session/sessionNavState'
import { Badge } from '@/ui/Badge'
import { Breadcrumb, type BreadcrumbItem } from '@/ui/Breadcrumb'
import { EmptyState } from '@/ui/EmptyState'
import { PageContent } from '@/ui/PageContent'
import { PageHeader } from '@/ui/PageHeader'
import { Select } from '@/ui/Select'
import { automationTier } from './automationTier'
import { AutomationControls } from './automation/AutomationControls'
import { AutomationDetail } from './automation/AutomationDetail'
import { engineBadge } from './automation/engineBadge'
import { CiRepoDetail, WINDOWS, type CiWindow } from './ci/CiRepoDetail'
import { deliveryOpenItem, deliveryPath, deliveryScope } from './deliveryNavState'
import { DeliveryWelcome } from './DeliveryWelcome'

export function DeliveryPage() {
  const { pathname, search } = useLocation()
  const navigate = useNavigate()
  const item = deliveryOpenItem(useParams(), new URLSearchParams(search))
  const scope = deliveryScope(pathname, search)

  // `poll: false` on the repo set: the shell's list column observes that exact
  // cache entry and owns the decision not to poll it (`useDeliveryWorkspace`),
  // and two polling observers of one key are two requests. `useAutomations`
  // takes no `poll` — that key has a `staleTime` and no interval at all — so
  // this is a third observer of it, not a third fetch.
  const automations = useAutomations()
  const repos = useStageModels({ poll: false })

  const automationId = item?.kind === 'automation' ? item.automationId : null
  const automation = automationId
    ? automations.data?.find((a) => a.id === automationId) ?? null
    : null
  // Shares `QK.automationActionable(id)` with `AutomationDetail`'s copy below,
  // so the header's tier dot costs no extra request.
  const actionable = useAutomationActionable(automationId ?? '', automation !== null)

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

  const engine = automation ? engineBadge(automation) : null
  const mode = automation?.mode ? MODE_DESCRIPTION[automation.mode] : undefined

  return (
    <div className="flex h-full min-h-0 flex-col" data-page-variant="standard">
      <PageHeader
        title={title}
        meta={crumbs.length > 1 ? (
          <div className="flex flex-col gap-1">
            <Breadcrumb items={crumbs} />
            {/* R9: what this automation is doing, and what its mode means, as
                text rather than as a dot the reader has to decode. */}
            {automation && (
              <div className="flex flex-wrap items-center gap-2 text-xs text-ink-muted">
                <TierDot tier={automationTier(automation, actionable.data?.length ?? 0)} />
                <span>{automation.status.text}</span>
                {mode && <span>{automation.mode} — {mode}</span>}
                {engine && (
                  <Badge tone="info" title="Agent engine this automation's unattended sessions run on">
                    {engine}
                  </Badge>
                )}
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
      {/* The CI detail owns its own skeleton, so only the panes that read the
          two list queries wait here. */}
      <PageContent
        busy={item?.kind === 'ciRepo'
          ? false
          : scope === 'ci'
            ? repos.isLoading && !repos.data
            : automations.isLoading && !automations.data}
      >
        {automation && (
          <AutomationDetail
            automation={automation}
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
        {item?.kind === 'ciRepo' && <CiRepoDetail repo={item.repo} window={ciWindow} />}
        {/* Nothing open: the pane says what is waiting rather than sitting blank. */}
        {item === null && (
          <DeliveryWelcome
            scope={scope}
            count={scope === 'ci'
              ? Object.keys(repos.data ?? {}).length
              : automations.data?.length ?? 0}
          />
        )}
      </PageContent>
    </div>
  )
}
