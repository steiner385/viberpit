import { Link } from 'react-router-dom'
import { useCapacity } from '@/api/hooks'
import { Banner } from '@/ui/Banner'
import { deliveryPath } from '@/features/delivery/deliveryNavState'
import { buildCapacityStack, type CapacityStackRow } from '@/features/delivery/capacity/capacityStack'
import { SettingsGroup } from './SettingsGroup'

/**
 * The live ceiling stack, read-only, on a Settings section (spec 2026-09-20
 * D10): Limits shows it whole; Hosts and Accounts show it with their own row
 * marked current. One component, one rendering — the numbers come from
 * `buildCapacityStack`, never re-derived here. Reads `useCapacity()` without
 * a poll: the Delivery page owns the timer; this shares its cache.
 */
export function LiveReadoutCard({ highlight }: { highlight?: CapacityStackRow['key'] }) {
  const { data: capacity, isError } = useCapacity()
  const stack = capacity ? buildCapacityStack(capacity) : null
  return (
    <SettingsGroup
      legend="Live readout"
      description={(
        <>
          The whole ceiling stack as automation sees it.{' '}
          <Link to={deliveryPath('capacity')} className="text-accent hover:underline">Open Delivery → Capacity</Link>
        </>
      )}
    >
      {stack === null
        ? (
          <p className="py-3 text-sm text-ink-muted">
            {isError ? 'The readout is unavailable.' : 'Loading the readout…'}
          </p>
        )
        : (
          <ul className="divide-y divide-edge">
            {stack.map((row) => {
              const current = row.key === highlight
              return (
                <li
                  key={row.key}
                  aria-current={current ? 'true' : undefined}
                  className="flex items-baseline justify-between gap-3 py-2 text-sm"
                >
                  <span className={row.binding ? 'font-medium text-warning' : current ? 'font-medium text-ink' : 'text-ink'}>
                    {row.label}
                  </span>
                  <span className="tabular-nums text-ink-muted">
                    {`${row.used} / ${row.cap ?? '∞'} · ${row.free ?? '∞'} free`}
                  </span>
                </li>
              )
            })}
          </ul>
        )}
      {capacity?.binding && (
        <Banner tone="warning" role="status" title="Binding constraint">{capacity.binding.text}</Banner>
      )}
    </SettingsGroup>
  )
}
