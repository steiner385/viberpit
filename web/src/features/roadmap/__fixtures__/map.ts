import type { CapabilityNode, InitiativeNode, ProductNode, RoadmapMap } from '@/api/roadmap'

export function cap(over: Partial<CapabilityNode> & { slug: string }): CapabilityNode {
  return {
    id: `f-${over.slug}`, title: over.slug, state: 'live', summary: null, docPath: null,
    commitSha: null, prNumber: null, parentSlug: null, tier: 'idle',
    counts: { openIssues: 0, inFlight: 0, blocked: 0, needsYou: 0 },
    links: [], initiatives: [], children: [], ...over,
  }
}

export function ini(over: Partial<InitiativeNode> & { slug: string; projectSlug: string }): InitiativeNode {
  return {
    title: over.slug, stage: 'build', status: 'active', primaryCapability: null,
    capabilities: [], tier: 'in-flight', reason: null, epic: null,
    parentInitiative: null, hasChildren: false, ...over,
  }
}

export function product(over: Partial<ProductNode> & { slug: string }): ProductNode {
  return {
    name: over.slug, tier: 'idle',
    counts: { inFlight: 0, blocked: 0, needsYou: 0, capabilities: 0, awaitingTriage: 0, needsHome: 0 },
    capabilities: [], initiatives: [], mapChanges: null, ...over,
  }
}

/** kindash: tasks ▸ (task-views) ; billing.  Initiative "views" primary=task-views. */
export const MAP: RoadmapMap = {
  generatedAt: '2026-09-08T00:00:00Z',
  products: [
    product({
      slug: 'kindash', name: 'KinDash', tier: 'needs-you',
      counts: { inFlight: 1, blocked: 0, needsYou: 1, capabilities: 3, awaitingTriage: 2, needsHome: 0 },
      capabilities: [
        cap({ slug: 'billing', title: 'Billing' }),
        // `tasks` counts 5 open issues, 3 of which are its child's: capability
        // counts are ROLLED UP by the server, so a parent's direct issues are
        // the difference. Kept different on purpose so the subtraction shows.
        cap({ slug: 'tasks', title: 'Tasks', tier: 'in-flight',
          counts: { openIssues: 5, inFlight: 0, blocked: 0, needsYou: 0 },
          children: [
            cap({ slug: 'task-views', title: 'Task views', parentSlug: 'tasks', tier: 'in-flight',
              counts: { openIssues: 3, inFlight: 0, blocked: 0, needsYou: 0 },
              initiatives: ['views'] }),
          ] }),
      ],
      initiatives: [
        ini({ slug: 'views', title: 'Use-case-first task views', projectSlug: 'kindash', primaryCapability: 'task-views', capabilities: ['task-views'] }),
        ini({ slug: 'orphan', title: 'No home yet', projectSlug: 'kindash', tier: 'needs-you', reason: 'advance' }),
        // A program: `hasChildren` and its phase (`audit-01`, nested via
        // `parentInitiative`) exercise treeRows' phase-nesting.
        ini({ slug: 'audit', title: 'UI audit', projectSlug: 'kindash', primaryCapability: 'tasks', hasChildren: true }),
        ini({ slug: 'audit-01', title: '01 Restore', projectSlug: 'kindash', primaryCapability: 'tasks', parentInitiative: 'audit' }),
      ],
    }),
    product({ slug: 'viberpit', name: 'ViberPit', capabilities: [cap({ slug: 'sessions', title: 'Sessions' })] }),
  ],
}
