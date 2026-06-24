// web/src/components/GroupHeader.tsx
import { useEffect, useRef } from 'react'
import { ChevronRight, ChevronDown } from 'lucide-react'
import { matchHeaderShortcut } from '@/lib/shortcuts'
import { cn } from '@/lib/utils'

interface GroupHeaderProps {
  groupKey: string
  label: string
  count: number
  collapsed: boolean
  /** Keyboard cursor is on this header. */
  selected?: boolean
  /** When true, the selected header takes real DOM focus so single keys land here. */
  listMode?: boolean
  onToggle: (key: string) => void
  onExpand?: (key: string) => void
  onCollapse?: (key: string) => void
  onNavigate?: (dir: 'up' | 'down') => void
  onExit?: () => void
}

export function GroupHeader({
  groupKey, label, count, collapsed, selected = false, listMode = false,
  onToggle, onExpand, onCollapse, onNavigate, onExit,
}: GroupHeaderProps) {
  const ref = useRef<HTMLButtonElement>(null)
  const panelId = `group-panel-${groupKey}`

  // In list-focus mode, give the highlighted header REAL keyboard focus so the
  // single-key shortcuts land here (mirrors SessionCard row focus).
  useEffect(() => {
    if (listMode && selected) ref.current?.focus()
  }, [listMode, selected])

  function handleKeyDown(e: React.KeyboardEvent) {
    const action = matchHeaderShortcut(e)
    if (!action) return
    e.preventDefault()
    switch (action.type) {
      case 'expand': onExpand?.(groupKey); break
      case 'collapse': onCollapse?.(groupKey); break
      case 'toggle': onToggle(groupKey); break
      case 'navigate': onNavigate?.(action.dir); break
      case 'exit': onExit?.(); break
    }
  }

  return (
    <button
      ref={ref}
      type="button"
      id={`group-header-${groupKey}`}
      tabIndex={selected ? 0 : -1}
      data-selected={selected}
      onClick={() => onToggle(groupKey)}
      onKeyDown={handleKeyDown}
      aria-expanded={!collapsed}
      aria-controls={collapsed ? undefined : panelId}
      className={cn(
        'sticky top-0 z-10 flex w-full items-center gap-2 px-1 py-1',
        'bg-gray-50/95 dark:bg-gray-900/95 backdrop-blur',
        'text-xs font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400',
        'hover:text-gray-700 dark:hover:text-gray-200',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 rounded',
        selected && 'ring-2 ring-blue-500 text-gray-700 dark:text-gray-200',
      )}
    >
      {collapsed ? <ChevronRight size={14} aria-hidden /> : <ChevronDown size={14} aria-hidden />}
      <span>{label}</span>
      <span className="tabular-nums text-gray-400 dark:text-gray-500">{count}</span>
    </button>
  )
}
