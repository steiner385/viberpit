// Active | Background | Archived — the sessions list's scope switch, on the
// `ui/ScopeSegment` primitive (shared with Roadmap and Delivery).
//
// The Active count includes the pre-spawn rows the list-controls gate counts;
// Background is only ever quiet maintenance rows — a background session that
// needs a human LIFTS into Active (`session/backgroundSessions.ts`), so a
// second badge there would be a second opinion about the same fact.
import { ScopeSegment as Segment } from '@/ui/ScopeSegment'
import {
  ARCHIVED_PATH, BACKGROUND_PATH, SESSIONS_PATH, type ListScope,
} from '@/session/sessionNavState'

export interface ScopeSegmentProps {
  scope: ListScope
  /** Active-scope sessions, pre-spawn rows included (the list-controls gate's count). */
  activeCount: number
  /** Quiet maintenance sessions held out of Active. */
  backgroundCount: number
  archivedCount: number
}

export function ScopeSegment({ scope, activeCount, backgroundCount, archivedCount }: ScopeSegmentProps) {
  return (
    <Segment
      label="Session scope"
      items={[
        { to: SESSIONS_PATH, label: 'Active', count: activeCount, current: scope === 'live' },
        { to: BACKGROUND_PATH, label: 'Background', count: backgroundCount, current: scope === 'background' },
        { to: ARCHIVED_PATH, label: 'Archived', count: archivedCount, current: scope === 'archived' },
      ]}
    />
  )
}
