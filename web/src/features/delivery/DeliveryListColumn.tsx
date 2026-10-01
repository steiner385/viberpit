/**
 * `app.list` for the Delivery section (finish-line spec §4.4). Automations
 * scope: every automation, tier-sorted, with its actionable count.
 * CI health scope: every tracked repo's 7-day check summary. Region-neutral
 * like `RoadmapListColumn`/`SessionListColumn`: it fills whatever
 * `ContextList` gives it and owns no width, no header and no route. Mounted
 * by `shell/listColumns.tsx`, from `useDeliveryWorkspace`'s props.
 */
import type { Automation } from '@/api/automations'
import type { CapacityReadout } from '@/api/capacity'
import { AutomationsScopeBody } from './AutomationsScopeBody'
import { CapacityScopeBody } from './capacity/CapacityScopeBody'
import { CiScopeBody } from './CiScopeBody'
import { ciThresholds } from './ci/ciHealth'
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
  capacity: CapacityReadout | undefined
  capacityLoading: boolean
  capacityError: boolean
  /** Key of the open row (`keyForDeliveryItem`), or null. */
  selectedKey: string | null
  onOpenAutomation: (id: string) => void
  onOpenRepo: (repo: string) => void
}

export function DeliveryListColumn({
  scope, automations, automationsLoading, automationsError,
  repos, reposLoading, reposError,
  capacity, capacityLoading, capacityError,
  selectedKey, onOpenAutomation, onOpenRepo,
}: DeliveryListColumnProps) {
  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden">
      <div className="shrink-0 p-2">
        <DeliveryScopeSegment
          scope={scope}
          automationCount={automations.length}
          ciCount={repos.length}
          capacityCount={capacity?.binding ? 1 : 0}
        />
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
          // The CI/CD Doctor's thresholds come off the automations list this
          // column already holds — no second fetch for a verdict.
          thresholds={ciThresholds(automations)}
          selectedKey={selectedKey}
          onOpen={onOpenRepo}
        />
      )}
      {scope === 'capacity' && (
        <CapacityScopeBody
          capacity={capacity}
          isLoading={capacityLoading}
          isError={capacityError}
        />
      )}
    </div>
  )
}
