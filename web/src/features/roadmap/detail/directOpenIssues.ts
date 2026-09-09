// How many open issues sit on a capability ITSELF. Its own module so the tile,
// the tab badge and the panel below them cannot drift apart.
//
// `counts.openIssues` is a rollup: `server/pm/tree.py:_add_counts` folds every
// descendant's counts into the parent, which is what a collapsed tree row wants
// to show ("this branch holds 5 open"). The Issues panel, though, lists
// `GET /pm/leaves?capability=<id>`, which is `WorkItem.feature_id == id` —
// DIRECT children only. Badging the rollup over that list would promise issues
// the list cannot show. The children's rollups already contain their own
// descendants, so one subtraction is the whole correction.
import type { CapabilityNode } from '@/api/roadmap'

export function directOpenIssues(capability: CapabilityNode): number {
  const inChildren = capability.children.reduce((n, ch) => n + ch.counts.openIssues, 0)
  // Never negative, even if a count arrives inconsistent with its children.
  return Math.max(0, capability.counts.openIssues - inChildren)
}
