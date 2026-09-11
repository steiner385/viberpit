import type * as React from 'react'
import type { NavId } from './navDefs'
import {
  SessionListColumn,
  type SessionListColumnProps,
} from '@/features/sessions/SessionListColumn'
import {
  RoadmapListColumn,
  type RoadmapListColumnProps,
} from '@/features/roadmap/RoadmapListColumn'
import {
  DeliveryListColumn,
  type DeliveryListColumnProps,
} from '@/features/delivery/DeliveryListColumn'

/**
 * Which nav sections own a list column, and how each one renders.
 *
 * `NavDef.hasList` tells the region matrix whether `app.list` exists for a
 * section; this is the other half — what goes in it. The two are pinned equal
 * by `listColumns.test.tsx`. Adding a list section means: flip `hasList`, add
 * the id here, add a source field and a `case`. `AppShell` supplies the
 * sources from its workspace hooks.
 */
export const LIST_SECTIONS = ['sessions', 'roadmap', 'delivery'] as const satisfies readonly NavId[]

export interface ListColumnSources {
  sessions: SessionListColumnProps
  roadmap: RoadmapListColumnProps
  delivery: DeliveryListColumnProps
}

export function listColumnFor(
  id: NavId | null | undefined,
  sources: ListColumnSources,
): React.ReactNode {
  switch (id) {
    case 'sessions':
      return <SessionListColumn {...sources.sessions} />
    case 'roadmap':
      return <RoadmapListColumn {...sources.roadmap} />
    case 'delivery':
      return <DeliveryListColumn {...sources.delivery} />
    default:
      return null
  }
}
