// The Automations scope of the Delivery list column. Modelled on
// `features/roadmap/inbox/InboxScopeBody.tsx`'s frame — `ScopeBody` plus a
// `ListSearchInput` in the controls strip — but with none of that scope's
// keyboard map, bulk bar or ordering store: Delivery has no bulk action and
// its only ordering is the fixed tier-then-name sort (spec §4.4).
import { useMemo, useState } from 'react'
import type { Automation } from '@/api/automations'
import { useAutomationActionableCounts } from '@/api/hooks'
import { ListSearchInput } from '@/components/ListSearchInput'
import { Banner } from '@/ui/Banner'
import { EmptyState } from '@/ui/EmptyState'
import { Skeleton } from '@/ui/Skeleton'
import { DELIVERY } from '@/features/products/vocab'
import { ScopeBody } from '@/features/sessions/ScopeBody'
import { AUTOMATION_TIER_RANK, automationTier } from './automationTier'
import { automationKey } from './deliveryNavState'
import { AutomationRow } from './rows/AutomationRow'

export interface AutomationsScopeBodyProps {
  automations: readonly Automation[]
  isLoading: boolean
  isError: boolean
  selectedKey: string | null
  onOpen: (id: string) => void
}

const haystack = (a: Automation) => `${a.name} ${a.id}`.toLowerCase()

export function AutomationsScopeBody({
  automations, isLoading, isError, selectedKey, onOpen,
}: AutomationsScopeBodyProps) {
  const [query, setQuery] = useState('')
  // One counts read for the whole scope — each row is handed its own number
  // rather than re-running `useAutomationActionableCounts` per row.
  const counts = useAutomationActionableCounts(automations.map((a) => a.id))
  const q = query.trim().toLowerCase()
  const visible = useMemo(
    () => (q ? automations.filter((a) => haystack(a).includes(q)) : automations),
    [automations, q],
  )
  const sorted = useMemo(() => [...visible].sort((a, b) => {
    const ta = automationTier(a, counts[a.id] ?? 0)
    const tb = automationTier(b, counts[b.id] ?? 0)
    return AUTOMATION_TIER_RANK[ta] - AUTOMATION_TIER_RANK[tb] || a.name.localeCompare(b.name)
  }), [visible, counts])

  return (
    <ScopeBody
      controls={
        <ListSearchInput
          value={query}
          onChange={setQuery}
          placeholder={`Search ${DELIVERY.automations.toLowerCase()}…`}
          ariaLabel={`Search ${DELIVERY.automations.toLowerCase()}`}
        />
      }
    >
      {isError && <Banner tone="danger" title={DELIVERY.automationsError} />}
      {isLoading && automations.length === 0 && (
        <div role="status" aria-label={`Loading ${DELIVERY.automations.toLowerCase()}`} className="space-y-2">
          {Array.from({ length: 3 }, (_, i) => (
            <div key={i} data-testid="skeleton"><Skeleton lines={1} /></div>
          ))}
        </div>
      )}
      {!isLoading && sorted.length === 0 && (
        <EmptyState title={q ? 'No matches' : DELIVERY.emptyAutomations} />
      )}
      {sorted.length > 0 && (
        <div role="list" aria-label={DELIVERY.automations} className="flex flex-col gap-1">
          {sorted.map((a) => (
            <AutomationRow
              key={a.id}
              automation={a}
              actionable={counts[a.id] ?? 0}
              selected={selectedKey === automationKey(a.id)}
              onOpen={onOpen}
            />
          ))}
        </div>
      )}
    </ScopeBody>
  )
}
