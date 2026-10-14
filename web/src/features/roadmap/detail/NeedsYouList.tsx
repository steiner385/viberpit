// Everything in scope waiting on a human: attention issues, needs-you
// initiatives, per-product proposal counts (which are decisions, not work),
// and the control-band incidents. One list so "what needs me" is answered in one
// place rather than by three panels a human has to remember to visit. Blocked
// work is WAITING, not needs-you (design 2026-10-14 §1): it follows in a quiet
// section of its own and is never counted.
import { Link } from 'react-router-dom'
import type { CapabilityNode, InitiativeNode, ProductNode } from '@/api/roadmap'
import { useHolds } from '@/api/hooks'
import { IncidentQueue } from '@/initiatives/IncidentQueue'
import { EmptyState } from '@/ui/EmptyState'
import { SectionHeading } from '@/ui/SectionHeading'
import { initiativeAsk } from '@/features/products/vocab'
import { capabilityPath, inboxPath, initiativePath } from '../roadmapNavState'
import { TIER_TEXT } from '../roadmapTier'
import { needsYouRows, waitingRows } from './needsYou'
import { LeafList } from './LeafList'
import { HeldIssueCard } from './HeldIssueCard'

function InitiativeRow({ product, initiative }: { product: ProductNode; initiative: InitiativeNode }) {
  // Subtitle and CTA from one table (`vocab.initiativeAsk`): the "why" is the
  // state, the "move" is the page control that answers it. Not a third
  // "Review …" for every state — that was P2 of the 2026-09-30 critique.
  const { why, move } = initiativeAsk(initiative)
  return (
    <li className="rounded-md border border-edge bg-surface p-3">
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
}

export function NeedsYouList({ products, repos, capability, overview = false }: {
  products: ProductNode[]; repos?: readonly string[]; capability?: CapabilityNode; overview?: boolean
}) {
  const { initiatives, issues, pseudo } = needsYouRows(products, capability)
  // Not on the overview: it answers "what needs me" only.
  const waiting = overview ? { initiatives: [], issues: [] } : waitingRows(products, capability)
  const waitingCount = waiting.initiatives.length + waiting.issues.length
  // The held issues themselves, from the escalation read model — not the map's
  // per-capability counts. The map only counts a held issue under the capability
  // it is classified into, so an UNCLASSIFIED held issue (ghostdriver-mcp#28,
  // 2026-10-02) showed up here only as "1 need a home" with no title, no reason
  // and no Retry. A capability scope is served by its own `LeafList` rows below,
  // so the read is skipped there; a portfolio scope reads fleet-wide and keeps
  // the products on screen.
  const inScope = new Set(products.map((p) => p.slug))
  const { data: holds } = useHolds(products.length === 1 ? products[0].slug : undefined, { enabled: !capability })
  const held = capability ? [] : (holds?.held ?? []).filter((h) => inScope.has(h.product))
  const empty = initiatives.length === 0 && issues.length === 0 && pseudo.length === 0 && held.length === 0
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
      {held.length > 0 && (
        <div className="flex flex-col gap-1">
          <SectionHeading>Held issues · {held.length}</SectionHeading>
          <ul className="flex flex-col gap-2" aria-label="Held issues">
            {held.map((item) => <HeldIssueCard key={item.ref} item={item} showProduct={products.length > 1} />)}
          </ul>
        </div>
      )}
      {initiatives.length > 0 && (
        <ul className="flex flex-col gap-1" aria-label="Initiatives that need you">
          {initiatives.map(({ product, initiative }) => (
            <InitiativeRow key={initiative.slug} product={product} initiative={initiative} />
          ))}
        </ul>
      )}
      {issues.length > 0 && (
        <ul className="flex flex-col gap-1" aria-label="Issues that need you">
          {issues.map(({ product, capability: issueCapability, needsYou }) => (
            <li key={`${product.slug}:${issueCapability.id}`} className="rounded-md border border-edge bg-surface p-3 text-sm">
              <p className="font-medium text-ink">{issueCapability.title ?? issueCapability.slug}</p>
              <p className="text-ink-muted">
                {product.name ?? product.slug}{' · '}
                {`${needsYou} ${needsYou === 1 ? 'issue waiting' : 'issues waiting'} for your input`}
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
      {waitingCount > 0 && (
        // Quiet on purpose: stopped on other work, not on you — no badge, no count
        // in the Needs-you total, but still one tap from the move that clears it.
        <div className="flex flex-col gap-1">
          <SectionHeading>Waiting · {waitingCount}</SectionHeading>
          <p className="max-w-prose text-xs text-ink-muted">Stopped on other work, not on you. Nothing here is counted as needing you.</p>
          {waiting.initiatives.length > 0 && (
            <ul className="flex flex-col gap-1" aria-label="Initiatives waiting">
              {waiting.initiatives.map(({ product, initiative }) => (
                <InitiativeRow key={initiative.slug} product={product} initiative={initiative} />
              ))}
            </ul>
          )}
          {waiting.issues.length > 0 && (
            <ul className="flex flex-col gap-1" aria-label="Issues waiting">
              {waiting.issues.map(({ product, capability: waitingCapability, blocked }) => (
                <li key={`${product.slug}:${waitingCapability.id}`} className="text-sm text-ink-muted">
                  <Link to={`${capabilityPath(product.slug, waitingCapability.slug)}?tab=issues`} className="text-ink hover:underline">
                    {waitingCapability.title ?? waitingCapability.slug}
                  </Link>
                  {' · '}{product.name ?? product.slug}{' · '}
                  {`${blocked} blocked ${blocked === 1 ? 'issue' : 'issues'}`}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      {/* Renders nothing at all when no watched band is breached. */}
      {!overview && <IncidentQueue repos={repos} title="Incidents" />}
    </section>
  )
}
