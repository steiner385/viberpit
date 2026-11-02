/**
 * Rail card: work running alongside the main turn — the session's sub-agents
 * (only when its engine spawns them) and its background shells / Monitors /
 * wakeup, each rendered inline (`SubAgentList`, `BackgroundWorkList`).
 */
import type { BackgroundWorkView, SubAgent } from '@/session/selectors'
import { BackgroundWorkList } from '../BackgroundWorkList'
import { SubAgentList } from '../SubAgentList'
import { RailCard, RailEmpty } from './RailCard'

interface Props {
  sessionId: string
  /** Server-advertised `subagents` capability (`can(caps, 'subagents')`). */
  subAgentsSupported: boolean
  subAgents: SubAgent[]
  backgroundWork: BackgroundWorkView
}

export function TasksCard({ sessionId, subAgentsSupported, subAgents, backgroundWork }: Props) {
  const showAgents = subAgentsSupported && subAgents.length > 0
  const showBackground = backgroundWork.rows.length > 0 || backgroundWork.wakeup
  return (
    <RailCard title="Tasks">
      {showAgents && <SubAgentList sessionId={sessionId} subAgents={subAgents} />}
      {showBackground && <BackgroundWorkList sessionId={sessionId} work={backgroundWork} />}
      {!showAgents && !showBackground && <RailEmpty>No background tasks</RailEmpty>}
    </RailCard>
  )
}
