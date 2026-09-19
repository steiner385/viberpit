/**
 * The Capacity scope of the Delivery list column — the same five stack rows
 * `CapacityDetail` shows, read-only (no controls: those live in the detail
 * pane, which is always what's open for this scope — see `DeliveryPage`).
 * Same frame as `AutomationsScopeBody`/`CiScopeBody` minus the search input:
 * five fixed rows have nothing to filter.
 */
import type { CapacityReadout } from '@/api/capacity'
import { DELIVERY } from '@/features/products/vocab'
import { ScopeBody } from '@/features/sessions/ScopeBody'
import { Banner } from '@/ui/Banner'
import { Skeleton } from '@/ui/Skeleton'
import { buildCapacityStack } from './capacityStack'

export interface CapacityScopeBodyProps {
  capacity: CapacityReadout | undefined
  isLoading: boolean
  isError: boolean
}

export function CapacityScopeBody({ capacity, isLoading, isError }: CapacityScopeBodyProps) {
  return (
    <ScopeBody>
      {isError && <Banner tone="danger" title={DELIVERY.capacityError} />}
      {isLoading && !capacity && (
        <div role="status" aria-label="Loading capacity" className="space-y-2">
          {Array.from({ length: 3 }, (_, i) => (
            <div key={i} data-testid="skeleton"><Skeleton lines={1} /></div>
          ))}
        </div>
      )}
      {capacity && (
        <ul role="list" aria-label={DELIVERY.capacity} className="flex flex-col gap-1 text-sm">
          {buildCapacityStack(capacity).map((row) => (
            <li key={row.key} data-testid={`capacity-stack-${row.key}`} className="rounded-md px-2 py-1.5 text-ink">
              <div className="flex items-center justify-between gap-2">
                <span className="truncate">{row.label}</span>
                <span className="shrink-0 tabular-nums text-ink-muted">
                  {row.used} / {row.cap ?? '∞'} · {row.free ?? '∞'} free
                </span>
              </div>
              {row.secondary && <p className="text-xs text-ink-muted">{row.secondary}</p>}
            </li>
          ))}
        </ul>
      )}
    </ScopeBody>
  )
}
