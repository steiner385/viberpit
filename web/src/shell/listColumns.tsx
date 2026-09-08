import type * as React from 'react'
import type { NavId } from './navDefs'
import {
  SessionListColumn,
  type SessionListColumnProps,
} from '@/features/sessions/SessionListColumn'

/**
 * Which nav sections own a list column, and how each one renders.
 *
 * `NavDef.hasList` tells the region matrix whether `app.list` exists for a
 * section; this is the other half — what goes in it. The two are pinned equal
 * by `listColumns.test.tsx`. Adding a list section means: flip `hasList`, add
 * the id here, add a source field and a `case`. `AppShell` supplies the
 * sources from its workspace hooks.
 */
export const LIST_SECTIONS = ['sessions'] as const satisfies readonly NavId[]

export interface ListColumnSources {
  sessions: SessionListColumnProps
}

export function listColumnFor(
  id: NavId | null | undefined,
  sources: ListColumnSources,
): React.ReactNode {
  switch (id) {
    case 'sessions':
      return <SessionListColumn {...sources.sessions} />
    default:
      return null
  }
}
