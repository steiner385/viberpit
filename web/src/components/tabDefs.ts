/**
 * Canonical tab model — the one place the app's section list, order, and labels
 * are defined. Both the mobile `Dashboard` strip and the wide `DesktopShell`
 * header render from this, so their tabs can never drift apart. Kept in a
 * non-component module so `TabBar.tsx` stays fast-refresh-friendly.
 */
import type { TabId } from './Dashboard'

export interface TabDef {
  id: TabId
  label: string
  /** Rendered as the "111"-style pill badge when greater than zero. */
  count?: number
  /** Renders an amber dot and appends "needs attention" to the accessible name. */
  attention?: boolean
}

/** Canonical tab order + labels. */
export const TAB_DEFS: { id: TabId; label: string }[] = [
  { id: 'sessions', label: 'Sessions' },
  { id: 'accounts', label: 'Accounts' },
  { id: 'pipeline', label: 'Pipeline' },
  { id: 'archived', label: 'Archived' },
  { id: 'automation', label: 'Automation' },
  { id: 'insights', label: 'Insights' },
]

/** Live counts/flags that decorate the canonical tabs with badges. */
export interface TabCounts {
  sessions?: number
  accounts?: number
  pipeline?: number
  archived?: number
  accountsAttention?: boolean
}

export function buildTabs(counts: TabCounts): TabDef[] {
  return TAB_DEFS.map((t) => ({
    ...t,
    count: counts[t.id as keyof TabCounts] as number | undefined,
    attention: t.id === 'accounts' ? counts.accountsAttention : undefined,
  }))
}
