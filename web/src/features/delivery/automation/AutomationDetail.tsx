/**
 * The automation detail pane: Overview · Queue · Schedule · Events ·
 * Sessions · Config (`docs/PAGE-TABS.md`) over one automation, re-homing both
 * the deleted monitor page (`automations/AutomationsTab.tsx`) and the old
 * Settings card (`components/AutomationCard.tsx`) behind `ui/Tabs` + URL state
 * (finish-line spec §4.4). `AutomationControls` (header actions) is a sibling
 * component, not rendered here — the page assembling this pane puts it beside
 * the automation's name.
 */
import { useState } from 'react'
import { Link } from 'react-router-dom'
import type { Automation, AutomationStatusProduct } from '@/api/automations'
import { AUTOMATION_TAB, AUTOMATION_TABS, type AutomationTab } from '@/features/products/vocab'
import { CheckTable, type Column } from '@/features/delivery/ci/CheckTable'
import { automationPath } from '../deliveryNavState'
import { productPath } from '@/features/roadmap/roadmapNavState'
import { useSearchParamState } from '@/lib/useSearchParamState'
import { Button } from '@/ui/Button'
import { Tabs } from '@/ui/Tabs'
import { AutomationActions } from './actions'
import { BacklogGroupsPanel } from './BacklogGroupsPanel'
import { ConfigPanel } from './ConfigPanel'
import { ConsolidatePanel } from './ConsolidatePanel'
import { EventsList } from './EventsList'
import { ManualDispatchDialog } from './ManualDispatchDialog'
import { SchedulePanel } from './SchedulePanel'
import { SessionsList } from './SessionsList'

const PRODUCT_COLUMNS: Column<AutomationStatusProduct>[] = [
  { header: 'Product', cell: (p) => <Link to={productPath(p.slug)} className="text-ink hover:underline">{p.slug}</Link> },
  { header: 'Dispatch', cell: (p) => p.dispatch },
  { header: 'In flight', cell: (p) => `${p.inFlight}/${p.cap}` },
  { header: 'Eligible', cell: (p) => p.eligible },
  { header: 'Bounced', cell: (p) => p.bounced },
]

/** Automations whose work can be inspected and dispatched by hand — the only
 *  ones that get a Queue tab (standard §4: hide a tab by object kind, never
 *  because it is empty right now).
 */
const QUEUE_AUTOMATIONS: ReadonlySet<string> = new Set(['backlog-runner', 'pr-doctor'])

export function AutomationDetail({ automation: a, automations = [], onOpenSession }: {
  automation: Automation
  automations?: readonly Automation[]
  onOpenSession: (id: string) => void
}) {
  const operatorTriggered = a.operatorTriggered ?? a.modes.length === 0
  const items = AUTOMATION_TABS.filter((t) =>
    !(t === 'queue' && !QUEUE_AUTOMATIONS.has(a.id))
    && !(t === 'schedule' && operatorTriggered)
    && !(t === 'config' && a.configSchema.length === 0))
  // `?tab=status` (the pre-standard name of the first tab) is not in `items`,
  // so an old bookmark falls back to Overview — no redirect needed.
  const [tab, setTab] = useSearchParamState<AutomationTab>('tab', items, 'overview')
  const [customDispatch, setCustomDispatch] = useState(false)

  return (
    <>
      <Tabs
        label="Automation"
        items={items.map((id) => ({ id, label: AUTOMATION_TAB[id] }))}
        value={tab}
        onChange={(id) => setTab(id as AutomationTab)}
        panelId={(id) => `automation-panel-${id}`}
        className="mb-4"
      />
      <div role="tabpanel" id={`automation-panel-${tab}`} aria-labelledby={`tab-${tab}`} className="space-y-4">
        {tab === 'overview' && (
          <>
            {a.description && <p className="text-sm text-ink-muted">{a.description}</p>}
            {/* The tier dot and `status.text` are NOT repeated here: the page
                header carries them (R9), where they stay visible on every tab
                rather than only on this one. */}
            {a.oversight ? (
              <div className="space-y-2 text-sm">
                <p><strong>Responsibility: </strong>{a.oversight.responsibility.label}</p>
                <p><strong>Scope: </strong>{a.oversight.scope}</p>
                {a.oversight.relationships.length > 0 && (
                  <ul className="space-y-2" aria-label="Relationships">
                    {a.oversight.relationships.map((relationship) => {
                      const target = automations.find((item) => item.id === relationship.targetId)
                      return (
                        <li key={`${relationship.kind}:${relationship.targetId}`}>
                          <span className="capitalize">{relationship.kind.replaceAll('_', ' ')}</span>{' '}
                          <Link className="text-accent hover:underline" to={automationPath(relationship.targetId)}>
                            {target?.name ?? relationship.targetId}
                          </Link>
                          <p className="text-ink-muted">{relationship.description}</p>
                        </li>
                      )
                    })}
                  </ul>
                )}
              </div>
            ) : <p className="text-sm text-ink-muted">Responsibility and scope unavailable.</p>}
            {a.id === 'backlog-runner' && (
              <div className="text-sm text-ink-muted">
                {a.status.products == null && <p>Product workload unavailable.</p>}
                {a.status.counts?.unassigned_in_flight == null
                  ? <p>Unassigned workload unknown.</p>
                  : a.status.counts.unassigned_in_flight > 0 && <p>{a.status.counts.unassigned_in_flight} in-flight work items are not assigned to a product.</p>}
              </div>
            )}
            {/* The Overview's "needs you" notice: renders nothing when there is
                nothing actionable (standard §5). */}
            <AutomationActions automationId={a.id} enabled />
          </>
        )}
        {tab === 'queue' && (
          <>
            {a.status.products && a.status.products.length > 0 && (
              <CheckTable<AutomationStatusProduct>
                title="Products"
                columns={PRODUCT_COLUMNS}
                rows={a.status.products}
                empty="No products yet."
              />
            )}
            {a.id === 'pr-doctor' && <ConsolidatePanel enabled onOpenSession={onOpenSession} />}
            {a.id === 'backlog-runner' && (
              <>
                <div className="flex justify-end">
                  <Button size="sm" variant="secondary" onClick={() => setCustomDispatch(true)}>Custom dispatch…</Button>
                </div>
                <BacklogGroupsPanel enabled onOpenSession={onOpenSession} />
              </>
            )}
          </>
        )}
        {tab === 'schedule' && <SchedulePanel automation={a} />}
        {tab === 'events' && <EventsList automationId={a.id} onOpenSession={onOpenSession} />}
        {tab === 'sessions' && <SessionsList automation={a} />}
        {tab === 'config' && <ConfigPanel automation={a} />}
      </div>
      {/* Outside the tab panel (standard §6): a tab change must not unmount a
          half-filled dispatch form. */}
      {a.id === 'backlog-runner' && (
        <ManualDispatchDialog open={customDispatch} onClose={() => setCustomDispatch(false)} onOpenSession={onOpenSession} />
      )}
    </>
  )
}
