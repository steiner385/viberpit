/**
 * The Capacity scope of the Delivery list column — the same five stack rows
 * `CapacityDetail` shows, read-only apart from each row's link to the page
 * that owns its dial. The controls live in the detail pane: beside this
 * column on desktop, and below desktop one tap away — the pane opens as an
 * item (`/delivery/capacity/governor`) from the link at the foot, because the
 * shell gives a phone's single band to the list and the pane was unreachable
 * there (2026-10-01 Delivery re-critique, P0).
 * Same frame as `AutomationsScopeBody`/`CiScopeBody` minus the search input:
 * five fixed rows have nothing to filter.
 */
import { Link } from 'react-router-dom'
import type { CapacityReadout } from '@/api/capacity'
import { DELIVERY } from '@/features/products/vocab'
import { ScopeBody } from '@/features/sessions/ScopeBody'
import { cn } from '@/lib/utils'
import { Banner } from '@/ui/Banner'
import { buttonVariants } from '@/ui/Button'
import { Skeleton } from '@/ui/Skeleton'
import { capacityPath } from '../deliveryNavState'
import { buildCapacityStack } from './capacityStack'
import { CapacityRowLabel } from './CapacityRowLabel'

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
        <>
          <ul role="list" aria-label={DELIVERY.capacity} className="flex flex-col gap-1 text-sm">
            {buildCapacityStack(capacity).map((row) => (
              <li key={row.key} data-testid={`capacity-stack-${row.key}`} className="rounded-md px-2 py-1.5 text-ink">
                <div className="flex items-center justify-between gap-2">
                  <CapacityRowLabel row={row} className="truncate text-accent hover:underline" />
                  <span className="shrink-0 tabular-nums text-ink-muted">
                    {row.used} / {row.cap ?? '∞'} · {row.free ?? '∞'} free
                  </span>
                </div>
                {row.secondary && <p className="text-xs text-ink-muted">{row.secondary}</p>}
              </li>
            ))}
          </ul>
          {/* `lg:hidden`: from the desktop breakpoint the pane is already
              beside this column, and a second way in would be a second copy. */}
          <Link
            to={capacityPath()}
            className={cn(buttonVariants({ variant: 'secondary', size: 'lg' }), 'mt-2 w-full no-underline lg:hidden')}
          >
            {DELIVERY.capacityControls}
          </Link>
        </>
      )}
    </ScopeBody>
  )
}
