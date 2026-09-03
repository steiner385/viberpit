/**
 * TabBar — the single source of truth for the app's section tabs.
 *
 * Previously the tab list, labels, active styling, the "Archived 111" count
 * badge, and the amber needs-attention dot were duplicated between the mobile
 * `Dashboard` and the wide-screen `DesktopShell`. This component owns all of it:
 * one canonical tab order, WAI-ARIA `role="tablist"` semantics with roving
 * `tabIndex` + arrow-key focus, count badges, and horizontal-scroll overflow so
 * every tab stays reachable on a narrow viewport.
 *
 * Callers supply a `TabDef[]` (label + optional count + optional attention flag)
 * and own the active state. Pass `panelId` to wire `aria-controls` to real
 * `role="tabpanel"` regions (the mobile Dashboard does; the desktop master-detail
 * pane is not a tabpanel, so it omits it).
 *
 * This is now a thin adapter over the generic `Tabs` primitive in `web/src/ui/`.
 */
import { Tabs } from '@/ui/Tabs'
import type { TabId } from './Dashboard'
import type { TabDef } from './tabDefs'

export interface TabBarProps {
  tabs: TabDef[]
  activeTab: TabId
  onSelect: (id: TabId) => void
  /** Accessible label for the tablist container. */
  label: string
  /** When provided, each tab gets `id="tab-<id>"` and `aria-controls={panelId(id)}`. */
  panelId?: (id: TabId) => string
  /** Extra classes for the tablist container (e.g. a baseline border on mobile). */
  className?: string
}

export function TabBar({ tabs, activeTab, onSelect, label, panelId, className }: TabBarProps) {
  return (
    <Tabs
      items={tabs}
      value={activeTab}
      onChange={(id) => onSelect(id as TabId)}
      label={label}
      panelId={panelId ? (id) => panelId(id as TabId) : undefined}
      className={className}
    />
  )
}
