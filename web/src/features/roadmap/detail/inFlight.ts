// What "in flight" means, in one place: the initiatives an In-flight list
// renders and the number an In-flight tile or tab badge shows. The sibling of
// `needsYou.ts`, and for the same reason — the portfolio summed the server's
// `counts.inFlight` (which rolls up ISSUE tiers too) while the product and
// capability levels counted initiatives, so one label counted two different
// populations one level apart.
//
// `IN_FLIGHT_TIERS` in `roadmapTier.ts` stays the one definition of which tiers
// count; this module is only where products are turned into rows.
//
// LEAF-ONLY (#1597 nesting): a program (`hasChildren`) is never counted here,
// only its phases are — the server's `counts.inFlight` already counts leaf
// initiatives alone, and counting a program AND its phase as two separate
// in-flight items would double-count one piece of work.
import type { InitiativeNode, ProductNode } from '@/api/roadmap'
import { IN_FLIGHT_TIERS } from '../roadmapTier'

/** Every LEAF initiative in scope that is moving or stuck, in map order. */
export function inFlightInitiatives(products: readonly ProductNode[]): InitiativeNode[] {
  return products.flatMap((p) => p.initiatives.filter((i) => IN_FLIGHT_TIERS.has(i.tier) && !i.hasChildren))
}

/** The number an "In flight" tile or tab badge shows — the rows the list renders. */
export function inFlightCount(products: readonly ProductNode[]): number {
  return inFlightInitiatives(products).length
}
