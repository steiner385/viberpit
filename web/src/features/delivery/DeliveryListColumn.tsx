/**
 * `app.list` for the Delivery section (finish-line spec §4.4). Automations
 * scope: every automation, tier-sorted, with its actionable count.
 * CI health scope: every tracked repo's 7-day check summary. Region-neutral
 * like `RoadmapListColumn`/`SessionListColumn`: it fills whatever
 * `ContextList` gives it and owns no width, no header and no route. Mounted
 * by `shell/listColumns.tsx`, from `useDeliveryWorkspace`'s props.
 */
import type { Automation } from '@/api/automations'
import { AutomationsScopeBody } from './AutomationsScopeBody'
import { CiScopeBody } from './CiScopeBody'
import { DeliveryScopeSegment } from './DeliveryScopeSegment'
import type { DeliveryScope } from './deliveryNavState'

export interface DeliveryListColumnProps {
  scope: DeliveryScope
  automations: readonly Automation[]
  automationsLoading: boolean
  automationsError: boolean
  repos: readonly string[]
  reposLoading: boolean
  reposError: boolean
  /** Key of the open row (`keyForDeliveryItem`), or null. */
  selectedKey: string | null
  onOpenAutomation: (id: string) => void
  onOpenRepo: (repo: string) => void
}

export function DeliveryListColumn({
  scope, automations, automationsLoading, automationsError,
  repos, reposLoading, reposError, selectedKey, onOpenAutomation, onOpenRepo,
}: DeliveryListColumnProps) {
  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden">
      <div className="shrink-0 p-2">
        <DeliveryScopeSegment scope={scope} automationCount={automations.length} ciCount={repos.length} />
      </div>
      {scope === 'automations' && (
        <AutomationsScopeBody
          automations={automations}
          isLoading={automationsLoading}
          isError={automationsError}
          selectedKey={selectedKey}
          onOpen={onOpenAutomation}
        />
      )}
      {scope === 'ci' && (
        <CiScopeBody
          repos={repos}
          isLoading={reposLoading}
          isError={reposError}
          selectedKey={selectedKey}
          onOpen={onOpenRepo}
        />
      )}
    </div>
  )
}
