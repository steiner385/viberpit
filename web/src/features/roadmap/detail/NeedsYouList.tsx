// Everything in scope waiting on a human: attention issues, needs-you or blocked
// initiatives, per-product proposal counts (which are decisions, not work),
// and the control-band incidents. One list so "what needs me" is answered in one
// place rather than by three panels a human has to remember to visit.
import { Link } from 'react-router-dom'
import type { CapabilityNode, ProductNode } from '@/api/roadmap'
import { IncidentQueue } from '@/initiatives/IncidentQueue'
import { EmptyState } from '@/ui/EmptyState'
import { initiativeAsk } from '@/features/products/vocab'
import { capabilityPath, inboxPath, initiativePath } from '../roadmapNavState'
import { TIER_TEXT } from '../roadmapTier'
import { needsYouRows } from './needsYou'
import { LeafList } from './LeafList'

export function NeedsYouList({ products, repos, capability, overview = false }: {
  products: ProductNode[]; repos?: readonly string[]; capability?: CapabilityNode; overview?: boolean
}) {
  const { initiatives, issues, pseudo } = needsYouRows(products, capability)
  const empty = initiatives.length === 0 && issues.length === 0 && pseudo.length === 0
  if (overview && empty) return null

  return (
    <section aria-label="Next actions" className="flex flex-col gap-3">
      {!empty && <h3 className="text-sm font-semibold text-ink">Next actions</h3>}
      {/* `max-w-prose` (the `EmptyState` measure): in a wide detail pane this
          sentence ran ~144 characters a line (2026-09-30 Roadmap critique, P3). */}
      {!empty && <p className="max-w-prose text-sm text-ink-muted">Review the items below to decide what happens next. Issue counts include each issue waiting for attention.</p>}
      {empty && (
        <EmptyState title="Nothing needs you" description="No issues, initiatives or proposals need a decision in this scope. Incidents are checked separately below." />
      )}
      {initiatives.length > 0 && (
        <ul className="flex flex-col gap-1" aria-label="Initiatives that need you">
          {initiatives.map(({ product, initiative }) => {
            // Subtitle and CTA from one table (`vocab.initiativeAsk`): the
            // "why" is the state, the "move" is the page control that answers
            // it. Not a third "Review …" for every state — that was P2 of the
            // 2026-09-30 critique.
            const { why, move } = initiativeAsk(initiative)
            return (
              <li key={initiative.slug} className="rounded-md border border-edge bg-surface p-3">
                <Link to={initiativePath(initiative.slug)} className="font-medium text-ink hover:underline">
                  {initiative.title ?? initiative.slug}
                </Link>
                <div className="text-xs text-ink-muted">
                  {product.name ?? product.slug}
                  {' · '}
                  <span className={TIER_TEXT[initiative.tier]}>{why}</span>
                </div>
                <Link to={initiativePath(initiative.slug)} className="text-sm text-accent hover:underline">
                  {move}
                </Link>
              </li>
            )
          })}
        </ul>
      )}
      {issues.length > 0 && (
        <ul className="flex flex-col gap-1" aria-label="Issues that need you">
          {issues.map(({ product, capability: issueCapability, needsYou, blocked }) => (
            <li key={`${product.slug}:${issueCapability.id}`} className="rounded-md border border-edge bg-surface p-3 text-sm">
              <p className="font-medium text-ink">{issueCapability.title ?? issueCapability.slug}</p>
              <p className="text-ink-muted">
                {product.name ?? product.slug}{' · '}
                {needsYou > 0 && `${needsYou} ${needsYou === 1 ? 'issue waiting' : 'issues waiting'} for your input`}
                {needsYou > 0 && blocked > 0 && ' · '}
                {blocked > 0 && `${blocked} blocked ${blocked === 1 ? 'issue' : 'issues'}`}
              </p>
              {capability?.id === issueCapability.id && !overview ? (
                <LeafList scope={{ capability: issueCapability.id }} product={product} attentionOnly />
              ) : <Link to={`${capabilityPath(product.slug, issueCapability.slug)}?tab=needs-you`} className="text-accent hover:underline">
                Review issues in {issueCapability.title ?? issueCapability.slug}
              </Link>}
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
              {p.counts.needsHome > 0 && <span className="text-ink-muted">{p.counts.needsHome} need a home — these issues need capability assignment; they may not yet have an Inbox proposal.</span>}
            </li>
          ))}
        </ul>
      )}
      {/* Renders nothing at all when no watched band is breached. */}
      {!overview && <IncidentQueue repos={repos} title="Incidents" />}
    </section>
  )
}
