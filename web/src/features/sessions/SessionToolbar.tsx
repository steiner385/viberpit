/**
 * SessionToolbar — the session pane's one-line strip above the transcript
 * (D-6, replacing the `SessionMeta` strip): the live status, the transcript
 * view mode, Export, and — on a pane wide enough for the rail — the rail
 * toggle. Nothing else: account, context, quota, MCP, rules, host, display
 * and the sub-agent / background lists live in the rail cards.
 */
import { PanelRightClose, PanelRightOpen } from 'lucide-react'
import { cn } from '@/lib/utils'
import { IconButton } from '@/ui/IconButton'
import { Badge } from '@/ui/Badge'
import { badgedName } from '@/ui/badgedName'
import { ICON_TOUCH_FLOOR } from '@/ui/list/listControlStyles'
import type { TranscriptViewMode } from '@/session/viewMode'
import { ViewModeMenu } from './transcript/ViewModeMenu'
import { StatusBar } from './StatusBar'
import { ExportMenu } from './ExportMenu'

interface Props {
  sessionId: string
  status: { state: string; count: number }
  connectionState: string
  subAgentCount: number
  viewMode: TranscriptViewMode
  onSelectViewMode: (mode: TranscriptViewMode) => void
  /** The rail toggle; null where the pane is too narrow for a rail (its cards
   *  are in the Details tab instead). `controls` is the rail's element id.
   *  `attention` (`railNeedsAttention`) puts the amber dot on the toggle while
   *  the rail is collapsed — an open rail shows the cause itself. */
  rail: { collapsed: boolean; onToggle: () => void; controls: string; attention: boolean } | null
}

/** Compact under a mouse, the 44px floor on coarse pointers (docs/ACCESSIBILITY.md §4). */
const TOGGLE = cn('relative h-7 w-7', ICON_TOUCH_FLOOR)

export function SessionToolbar({ sessionId, status, connectionState, subAgentCount, viewMode, onSelectViewMode, rail }: Props) {
  return (
    <div className={cn('flex flex-shrink-0 flex-wrap items-center gap-x-2 px-3 py-1', 'border-b border-edge bg-surface-raised text-xs text-ink-muted')}>
      <StatusBar status={status} connectionState={connectionState} subAgentCount={subAgentCount} />
      <div className="ml-auto flex items-center gap-2">
        <ViewModeMenu mode={viewMode} onSelect={onSelectViewMode} />
        <ExportMenu sessionId={sessionId} />
        {rail && (
          <IconButton
            aria-label={rail.collapsed
              ? badgedName({ label: 'Show details', attention: rail.attention })
              : 'Hide details'}
            aria-expanded={!rail.collapsed}
            aria-controls={rail.collapsed ? undefined : rail.controls}
            onClick={rail.onToggle}
            className={TOGGLE}
          >
            {rail.collapsed
              ? <PanelRightOpen size={14} aria-hidden="true" />
              : <PanelRightClose size={14} aria-hidden="true" />}
            {rail.collapsed && rail.attention && (
              <Badge tone="warning" dot data-testid="rail-toggle-attention" className="absolute top-0.5 right-0.5" />
            )}
          </IconButton>
        )}
      </div>
    </div>
  )
}
