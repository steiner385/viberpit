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
}: GroupedListProps<T>) {
  return (
    <>
      {groups.map((group) => {
        const collapsed = collapsedKeys?.has(group.key) ?? false
        const panelId = `group-panel-${group.key}`
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
              <div
                id={panelId}
                className="space-y-2"
                {...(group.label !== null
                  ? { role: 'group', 'aria-labelledby': `group-header-${group.key}` }
                  : {})}
              >
                {group.sessions.map(renderRow)}
              </div>
            )}
          </div>
        )
      })}
    </>
  )
}
