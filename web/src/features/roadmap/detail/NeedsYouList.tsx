// Everything in scope that is waiting on a human: attention issues, initiatives
// the server tiered `needs-you` or `blocked`, per-product proposal counts,
// and the control-band incidents. One list so "what needs me" is answered in one
// place rather than by three panels a human has to remember to visit.
import { Link } from 'react-router-dom'
import type { ProductNode } from '@/api/roadmap'
import { IncidentQueue } from '@/initiatives/IncidentQueue'
import { EmptyState } from '@/ui/EmptyState'
import { initiativeStatusLabel, reasonLabel } from '@/features/products/vocab'
import { capabilityPath, inboxPath, initiativePath } from '../roadmapNavState'
import { TIER_TEXT } from '../roadmapTier'
import { needsYouRows } from './needsYou'
import { LeafList } from './LeafList'

export function NeedsYouList({ products, repos, issueScopeCapability, issueProduct }: {
  products: ProductNode[]
  repos?: readonly string[]
  /** On a capability detail, show its own issue actions instead of a self-link. */
  issueScopeCapability?: string
  /** Full product tree for reassignment when the attention list is subtree-scoped. */
  issueProduct?: ProductNode
}) {
  const { initiatives, pseudo, issueGroups } = needsYouRows(products)
  const empty = initiatives.length === 0 && pseudo.length === 0 && issueGroups.length === 0

  return (
    <div className="flex flex-col gap-3">
      {empty && (
        <EmptyState title="Nothing needs you" description="No issues, initiatives, or proposals are waiting for your attention." />
      )}
      {issueGroups.length > 0 && (
        <ul className="flex flex-col gap-3" aria-label="Issues that need you">
          {issueGroups.map(({ product, capability, count }) => (
            <li key={`${product.slug}:${capability.id}`} className="rounded-md border border-edge bg-surface p-3">
              <h3 className="font-medium text-ink">{capability.title ?? capability.slug}</h3>
              {capability.id === issueScopeCapability ? (
                <LeafList scope={{ capability: capability.id }} product={issueProduct ?? product} attentionOnly />
              ) : (
                <Link
                  className="text-sm text-accent hover:underline"
                  to={`${capabilityPath(product.slug, capability.slug)}?tab=needs-you`}
                >
                  {count} {count === 1 ? 'issue needs' : 'issues need'} attention
                </Link>
              )}
            </li>
          ))}
        </ul>
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
                <span className={TIER_TEXT[initiative.tier]}>
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
