/**
 * The Inbox list's group/sort spec, in a module of its own because TWO
 * components have to agree on it.
 *
 * `InboxScopeBody` renders the rows in this order; `RoadmapPage` walks the same
 * order to pick the next proposal after a decision. When the spec lived inside
 * the list body the pane could only walk the SERVER's order (`created_at DESC`),
 * so with the default grouping (by product) "next" jumped to whatever came next
 * chronologically — a different row from the one under it on screen.
 *
 * Both call `useItemOrdering` with this same `namespace`, so both read the same
 * persisted group-by/sort-by prefs and produce the same sequence. The list's
 * transient search box is deliberately NOT part of it: a search narrows what is
 * on screen, but a decision should still advance to the next proposal in the
 * queue rather than dead-end because the next one does not match the text
 * currently typed in a box on the other pane.
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
