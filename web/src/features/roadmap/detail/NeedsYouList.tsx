// Everything in scope that is waiting on a human: initiatives the server tiered
// `needs-you`, the per-product proposal counts (which are decisions, not work),
// and the control-band incidents. One list so "what needs me" is answered in one
// place rather than by three panels a human has to remember to visit.
import { Link } from 'react-router-dom'
import type { ProductNode } from '@/api/roadmap'
import { IncidentQueue } from '@/initiatives/IncidentQueue'
import { EmptyState } from '@/ui/EmptyState'
import { initiativeStatusLabel, reasonLabel } from '@/features/products/vocab'
import { inboxPath, initiativePath } from '../roadmapNavState'
import { needsYouRows } from './needsYou'

export function NeedsYouList({ products, repos }: { products: ProductNode[]; repos?: readonly string[] }) {
  const { initiatives, pseudo } = needsYouRows(products)
  const empty = initiatives.length === 0 && pseudo.length === 0

  return (
    <div className="flex flex-col gap-3">
      {empty && (
        <EmptyState title="Nothing needs you" description="Every initiative and proposal is moving on its own." />
      )}
      {initiatives.length > 0 && (
        <ul className="flex flex-col gap-1" aria-label="Initiatives that need you">
          {initiatives.map(({ product, initiative }) => (
            <li key={initiative.slug} className="rounded-md border border-edge bg-surface p-3">
              <Link to={initiativePath(initiative.slug)} className="font-medium text-ink hover:underline">
                {initiative.title ?? initiative.slug}
              </Link>
              <div className="text-xs text-ink-muted">
                {product.name ?? product.slug}
                {' · '}
                <span className="text-warning">
                  {reasonLabel(initiative.reason) || initiativeStatusLabel(initiative.status)}
                </span>
              </div>
            </li>
          ))}
        </ul>
      )}
      {pseudo.length > 0 && (
        <ul className="flex flex-col gap-1" aria-label="Proposals awaiting a decision">
          {pseudo.map((p) => (
            <li key={p.slug} className="rounded-md border border-edge bg-surface p-3 text-sm">
              <span className="font-medium text-ink">{p.name ?? p.slug}</span>
              {' · '}
              {p.counts.awaitingTriage > 0 && (
                // Stays inside this section: the queue is the Inbox scope,
                // narrowed to this product (finish-line row 3).
                <Link className="text-accent hover:underline" to={inboxPath({ product: p.slug })}>
                  {p.counts.awaitingTriage} awaiting triage
                </Link>
              )}
              {p.counts.awaitingTriage > 0 && p.counts.needsHome > 0 && ' · '}
              {p.counts.needsHome > 0 && <span className="text-ink-muted">{p.counts.needsHome} need a home</span>}
            </li>
          ))}
        </ul>
      )}
      {/* Renders nothing at all when no watched band is breached. */}
      <IncidentQueue repos={repos} title="Incidents" />
    </div>
  )
}
