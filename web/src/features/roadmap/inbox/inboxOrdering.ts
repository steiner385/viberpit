/**
 * The Inbox list's group/sort spec, in a module of its own so the ordering is
 * named and testable apart from the component that renders it.
 *
 * `InboxScopeBody` is the ONLY caller: it feeds this spec to `useItemOrdering`
 * and then publishes the resulting row ids through `inboxOrderStore`, which is
 * where the detail pane reads "what comes next" from. The pane deliberately
 * does NOT re-derive the order from this spec — two `useItemOrdering` instances
 * cannot stay in step, because each seeds its group-by/sort-by from
 * localStorage once and never re-reads it (see `inboxOrderStore`'s header).
 */
import { isDecisionProposal, type InboxProposal } from '@/api/triage'
import type { OrderingSpec } from '@/session/useSessionOrdering'

export type InboxGroupBy = 'product' | 'capability' | 'confidence' | 'none'
export type InboxSortBy = 'newest' | 'confidence' | 'title'

/**
 * THE spec for both inbox card kinds (spec 2026-09-18 §6: decision proposals
 * list with classification proposals, "same ordering spec and keys").
 *
 * Product, newest and title are facts both kinds carry, so those three need no
 * branch at all. Capability and confidence are the classifier's alone — a
 * decision proposal has neither — so each of those groupings gives decisions one
 * bucket of their own, ordered LAST: the grouping was chosen to sort
 * classification work, and burying decisions inside "no fit" or "low confidence"
 * would say something about them that is not true.
 *
 * The key is NAMESPACED (`kind:decision`) because the two groupings that use it
 * key their other buckets on user data — a capability slug, a confidence level.
 * A capability literally slugged `decision` would otherwise land in this bucket
 * with a different label and a different order, and the collapsed-state store
 * (which persists bucket keys) would confuse the two across sessions.
 */
const DECISION_BUCKET = { key: 'kind:decision', label: 'Decisions' }

/** High first — the queue's whole job is to surface the safe accepts. A
 *  decision has no confidence and ranks after every level of it. */
const CONF_RANK: Record<string, number> = { high: 0, medium: 1, low: 2 }
const DECISION_RANK = 3
const confRank = (p: InboxProposal) =>
  (isDecisionProposal(p) ? DECISION_RANK : CONF_RANK[p.confidenceLevel ?? 'low'] ?? 2)

/** What a row blocks (spec 2026-09-30 §8): a decision's `downstream` from the
 *  escalation read model; a triage row blocks nothing. Leads every sort so the
 *  loudest decision is first in its bucket whatever the operator sorts by. */
const downstreamOf = (p: InboxProposal) => (isDecisionProposal(p) ? p.downstream ?? 0 : 0)
const impactFirst = (cmp: (a: InboxProposal, b: InboxProposal) => number) =>
  (a: InboxProposal, b: InboxProposal) => downstreamOf(b) - downstreamOf(a) || cmp(a, b)

export const INBOX_ORDERING_SPEC: OrderingSpec<InboxGroupBy, InboxSortBy, InboxProposal> = {
  namespace: 'roadmap-inbox',
  groupByValues: ['product', 'capability', 'confidence', 'none'],
  sortByValues: ['newest', 'confidence', 'title'],
  defaultPref: { groupBy: 'product', sortBy: 'newest' },
  config: {
    idOf: (p) => p.id,
    groupings: {
      product: {
        bucketOf: (p) => ({ key: p.projectSlug ?? 'unknown', label: p.projectSlug ?? 'unknown' }),
      },
      capability: {
        // "No fit" last: it is the bucket that needs a decision, not a home.
        bucketOf: (p) => (isDecisionProposal(p)
          ? { ...DECISION_BUCKET, order: 2 }
          : {
            key: p.proposedFeatureSlug ?? 'no-fit',
            label: p.proposedFeatureSlug ?? 'No fit',
            order: p.proposedFeatureSlug ? 0 : 1,
          }),
      },
      confidence: {
        bucketOf: (p) => (isDecisionProposal(p)
          ? { ...DECISION_BUCKET, order: DECISION_RANK }
          : {
            key: p.confidenceLevel ?? 'low',
            label: p.confidenceLevel ?? 'low',
            order: confRank(p),
          }),
      },
    },
    // Every comparator ends on the id, so a refetch cannot reshuffle equal rows
    // under the cursor.
    sorts: {
      newest: impactFirst((a, b) => (b.createdAt ?? '').localeCompare(a.createdAt ?? '') || a.id.localeCompare(b.id)),
      confidence: impactFirst((a, b) => confRank(a) - confRank(b) || a.id.localeCompare(b.id)),
      title: impactFirst((a, b) => (a.title ?? a.id).localeCompare(b.title ?? b.id) || a.id.localeCompare(b.id)),
    },
  },
}
