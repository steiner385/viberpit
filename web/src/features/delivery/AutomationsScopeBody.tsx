// The Automations scope of the Delivery list column. Modelled on
// `features/roadmap/inbox/InboxScopeBody.tsx`'s frame — `ScopeBody` plus a
// `ListSearchInput` in the controls strip — but with none of that scope's
// bulk bar or ordering store: Delivery has no bulk action and its only
// ordering is the fixed tier-then-name sort (spec §4.4). Its keyboard is the
// list's one roving tab stop (`ui/list/useRovingRows`), nothing more.
import { useMemo, useState } from 'react'
import type { Automation } from '@/api/automations'
import { ListSearchInput } from '@/ui/list/ListSearchInput'
import { useRovingRows } from '@/ui/list/useRovingRows'
import { Banner } from '@/ui/Banner'
import { EmptyState } from '@/ui/EmptyState'
import { Skeleton } from '@/ui/Skeleton'
import { DELIVERY } from '@/features/products/vocab'
import { ScopeBody } from '@/features/sessions/ScopeBody'
import { AUTOMATION_TIER_RANK, automationTier } from './automationTier'
import { AUTOMATIONS_ID_SCOPE, automationKey } from './deliveryNavState'
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
  const q = query.trim().toLowerCase()
  const visible = useMemo(
    () => (q ? automations.filter((a) => haystack(a).includes(q)) : automations),
    [automations, q],
  )
  const sorted = useMemo(() => [...visible].sort((a, b) => {
    const ta = automationTier(a)
    const tb = automationTier(b)
    return AUTOMATION_TIER_RANK[ta] - AUTOMATION_TIER_RANK[tb] || a.name.localeCompare(b.name)
  }), [visible])
  // One tab stop for the list — the open row, else the first — and arrows to
  // walk it. Without this every row was `tabIndex -1` until one was selected,
  // and nothing is selected on arrival, so Tab skipped the whole list
  // (2026-10-01 Delivery critique, P1).
  const ids = useMemo(() => sorted.map((a) => a.id), [sorted])
  const selectedId = sorted.find((a) => automationKey(a.id) === selectedKey)?.id ?? null
  const { containerRef, rovingKey, onKeyDown } = useRovingRows(ids, selectedId, AUTOMATIONS_ID_SCOPE)

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
        <div ref={containerRef} role="list" aria-label={DELIVERY.automations} className="flex flex-col gap-1" onKeyDown={onKeyDown}>
          {sorted.map((a) => (
            <AutomationRow
              key={a.id}
              automation={a}
              selected={selectedKey === automationKey(a.id)}
              focusable={rovingKey === a.id}
              idScope={AUTOMATIONS_ID_SCOPE}
              onOpen={onOpen}
            />
          ))}
        </div>
      )}
    </ScopeBody>
  )
}
