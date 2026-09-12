import * as React from 'react'
import { GroupHeader } from './GroupHeader'
import type { SessionGroup } from '@/session/ordering'

interface GroupedListProps<T> {
  groups: SessionGroup<T>[]
  /** Must return a keyed element (key on the row itself). */
  renderRow: (item: T) => React.ReactNode
  collapsedKeys?: Set<string>
  onToggleCollapse?: (key: string) => void
  selectedGroupKey?: string | null
  onExpand?: (key: string) => void
  onCollapse?: (key: string) => void
  listMode?: boolean
  onNavigate?: (dir: 'up' | 'down') => void
  onExit?: () => void
  /**
   * ARIA role for each group's row-container ("panel"). Default `'group'` is
   * the LIVE list's shape and MUST NOT change: `SessionCard` rows are not
   * `role="listitem"`, so pairing them with `role="list"` would itself be an
   * `aria-required-children` violation, and `SessionList.test.tsx` — "group
   * panel ARIA disclosure" — pins `role="group"` (label !== null) / no role at
   * all (label === null, the single ungrouped panel).
   *
   * `'list'` is for callers whose rows ARE `role="listitem"` (currently
   * `ArchivedSessionList`). axe's `aria-required-parent` needs a `list`
   * ancestor for every `listitem`, in EVERY grouping mode — not just the
   * ungrouped one a single outer `role="list"` wrapper used to cover. Putting
   * `role="list"` on each panel instead (grouped panels keep
   * `aria-labelledby` pointed at their header; the lone ungrouped panel takes
   * `ariaLabel`) satisfies that in all three modes: `GroupHeader`'s `button`
   * sits beside its panel, never inside one, so it is never a disallowed
   * `list` child either.
   */
  panelRole?: 'group' | 'list'
  /** Accessible name for the ungrouped panel when `panelRole === 'list'` — it
   *  has no header to attach `aria-labelledby` to. Unused otherwise. */
  ariaLabel?: string
}

export function GroupedList<T>({
  groups,
  renderRow,
  collapsedKeys,
  onToggleCollapse,
  selectedGroupKey,
  onExpand,
  onCollapse,
  listMode,
  onNavigate,
  onExit,
  panelRole = 'group',
  ariaLabel,
}: GroupedListProps<T>) {
  return (
    <>
      {groups.map((group) => {
        const collapsed = collapsedKeys?.has(group.key) ?? false
        const panelId = `group-panel-${group.key}`
        const panelProps =
          group.label !== null
            ? { role: panelRole, 'aria-labelledby': `group-header-${group.key}` }
            : panelRole === 'list'
              ? { role: 'list' as const, ...(ariaLabel ? { 'aria-label': ariaLabel } : {}) }
              : {}
        return (
          <div key={group.key} className="space-y-2">
            {group.label !== null && (
              <GroupHeader
                groupKey={group.key}
                label={group.label}
                count={group.count}
                collapsed={collapsed}
                selected={group.key === selectedGroupKey}
                listMode={listMode}
                onToggle={(k) => onToggleCollapse?.(k)}
                onExpand={onExpand}
                onCollapse={onCollapse}
                onNavigate={onNavigate}
                onExit={onExit}
              />
            )}
            {!collapsed && (
              <div id={panelId} className="space-y-2" {...panelProps}>
                {group.sessions.map(renderRow)}
              </div>
            )}
          </div>
        )
      })}
    </>
  )
}
