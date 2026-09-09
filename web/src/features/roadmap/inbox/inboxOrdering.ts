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
import type { TriageProposal } from '@/api/triage'
import type { OrderingSpec } from '@/session/useSessionOrdering'

export type InboxGroupBy = 'product' | 'capability' | 'confidence' | 'none'
export type InboxSortBy = 'newest' | 'confidence' | 'title'

/** High first — the queue's whole job is to surface the safe accepts. */
const CONF_RANK: Record<string, number> = { high: 0, medium: 1, low: 2 }
const confRank = (p: TriageProposal) => CONF_RANK[p.confidenceLevel ?? 'low'] ?? 2

export const INBOX_ORDERING_SPEC: OrderingSpec<InboxGroupBy, InboxSortBy, TriageProposal> = {
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
        bucketOf: (p) => ({
          key: p.proposedFeatureSlug ?? 'no-fit',
          label: p.proposedFeatureSlug ?? 'No fit',
          order: p.proposedFeatureSlug ? 0 : 1,
        }),
      },
      confidence: {
        bucketOf: (p) => ({
          key: p.confidenceLevel ?? 'low',
          label: p.confidenceLevel ?? 'low',
          order: confRank(p),
        }),
      },
    },
    // Every comparator ends on the id, so a refetch cannot reshuffle equal rows
    // under the cursor.
    sorts: {
      newest: (a, b) => (b.createdAt ?? '').localeCompare(a.createdAt ?? '') || a.id.localeCompare(b.id),
      confidence: (a, b) => confRank(a) - confRank(b) || a.id.localeCompare(b.id),
      title: (a, b) => (a.title ?? a.id).localeCompare(b.title ?? b.id) || a.id.localeCompare(b.id),
    },
  },
}
