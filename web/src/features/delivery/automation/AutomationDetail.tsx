/**
 * The automation detail pane: five tabs over one automation, re-homing both
 * the old monitor page (`automations/AutomationsTab.tsx`) and the old
 * Settings card (`components/AutomationCard.tsx`) behind `ui/Tabs` + URL state
 * (finish-line spec §4.4). `AutomationControls` (header actions) is a sibling
 * component, not rendered here — the page assembling this pane puts it beside
 * the automation's name.
 */
import { useState } from 'react'
import type { Automation } from '@/api/automations'
import { useAutomationActionable } from '@/api/hooks'
import { AUTOMATION_TAB, AUTOMATION_TABS, type AutomationTab } from '@/features/products/vocab'
import { TierDot } from '@/features/roadmap/rows/TierDot'
import { useSearchParamState } from '@/lib/useSearchParamState'
import { Button } from '@/ui/Button'
import { Tabs } from '@/ui/Tabs'
import { BacklogGroupsPanel } from '@/components/BacklogGroupsPanel'
import { ConsolidatePanel } from '@/components/ConsolidatePanel'
import { ManualDispatchDialog } from '@/components/ManualDispatchDialog'
import { AutomationActions } from './actions'
import { automationTier } from '../automationTier'
import { ConfigPanel } from './ConfigPanel'
import { EventsList } from './EventsList'
import { SchedulePanel } from './SchedulePanel'
import { SessionsList } from './SessionsList'

export function AutomationDetail({ automation: a, onOpenSession }: {
  automation: Automation
  onOpenSession: (id: string) => void
}) {
  const operatorTriggered = a.operatorTriggered ?? a.modes.length === 0
  const items = AUTOMATION_TABS.filter((t) =>
    !(t === 'schedule' && operatorTriggered) && !(t === 'config' && a.configSchema.length === 0))
  const [tab, setTab] = useSearchParamState<AutomationTab>('tab', items, 'status')
  const [customDispatch, setCustomDispatch] = useState(false)
  // Shares the `useAutomationActionable` query key with `AutomationActions`
  // below, so this costs no extra request — only the status line's tier.
  const actionable = useAutomationActionable(a.id)
  const tier = automationTier(a, actionable.data?.length ?? 0)

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
        {tab === 'status' && (
          <>
            {a.description && <p className="text-sm text-ink-muted">{a.description}</p>}
            <div className="flex items-center gap-2 text-sm text-ink-muted">
              <TierDot tier={tier} />
              {a.status.text}
            </div>
            <AutomationActions automationId={a.id} enabled />
            {a.id === 'pr-doctor' && <ConsolidatePanel enabled onOpenSession={onOpenSession} />}
            {a.id === 'backlog-runner' && (
              <>
                <div className="flex justify-end">
                  <Button size="sm" variant="secondary" onClick={() => setCustomDispatch(true)}>Custom dispatch…</Button>
                </div>
                <BacklogGroupsPanel enabled onOpenSession={onOpenSession} />
                <ManualDispatchDialog open={customDispatch} onClose={() => setCustomDispatch(false)} onOpenSession={onOpenSession} />
              </>
            )}
          </>
        )}
        {tab === 'schedule' && <SchedulePanel automation={a} />}
        {tab === 'events' && <EventsList automationId={a.id} onOpenSession={onOpenSession} />}
        {tab === 'sessions' && <SessionsList automation={a} />}
        {tab === 'config' && <ConfigPanel automation={a} />}
      </div>
    </>
  )
}
