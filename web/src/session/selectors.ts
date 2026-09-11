/**
 * Derived views over transcript state for the session status strip.
 */
import type { Turn, SubThread, SubThreadTaskStatus, TranscriptState } from './reducer'

// ── Visible transcript (double-buffer during replay) ──────────────────────────
//
// A gap-heal replays the whole transcript (replayReset → replay events →
// replayComplete), during which the reducer's live `turns`/`subAgents` are empty
// and rebuilding. To avoid blanking the view, replayReset captures the last
// rendered content into `staleTurns`/`staleSubAgents`. These selectors are the
// SINGLE decision point for which copy the UI shows: the stale snapshot while a
// replay is in flight (and a snapshot exists), otherwise the live arrays. Routing
// every consumer through here keeps the choice out of the components.

/** Fields the visible-transcript selectors read (a narrow slice of TranscriptState). */
type VisibleSlice = Pick<TranscriptState, 'replaying' | 'turns' | 'staleTurns' | 'subAgents' | 'staleSubAgents'>

/** True when a replay is in flight AND a stale snapshot exists to serve instead of the empty live arrays. */
function servingStale(state: VisibleSlice): boolean {
  return state.replaying && state.staleTurns.length > 0
}

/** The turns the UI should render: the stale snapshot during a heal, else the live turns. */
export function selectVisibleTurns(state: VisibleSlice): Turn[] {
  return servingStale(state) ? state.staleTurns : state.turns
}

/** The sub-agent threads the UI should render: the stale snapshot during a heal, else the live map. */
export function selectVisibleSubAgents(state: VisibleSlice): Record<string, SubThread> {
  return servingStale(state) ? state.staleSubAgents : state.subAgents
}

export type SubAgentStatus = 'running' | 'done' | 'failed' | 'stopped'

export interface SubAgent {
  toolId: string       // the spawning Task tool-use id (= key into state.subAgents)
  type: string         // subagent_type, e.g. "Explore" (falls back to "agent")
  description: string
  status: SubAgentStatus
  startedAt?: number   // ms epoch the Task tool call was issued (the block's create-time ts)
  endedAt?: number     // ms epoch its result arrived; only set once the agent finishes
  // Live task state (subAgentStarted/Progress/Ended); absent on a replayed session.
  taskId?: string
  activity?: string | null
  lastTool?: string | null
  totalTokens?: number | null
  toolUses?: number | null
  summary?: string | null
  backgrounded?: boolean
}

const SUBAGENT_TOOL_NAMES = new Set(['Task', 'Agent'])

const TASK_STATUS: Record<SubThreadTaskStatus, SubAgentStatus> = {
  running: 'running', completed: 'done', failed: 'failed', stopped: 'stopped',
}

/**
 * Extract the list of sub-agents from the transcript.
 *
 * Sub-agents surface to the parent session as `Task` or `Agent` tool calls. Status
 * comes from the thread's live task state when the CLI's task notices supplied one
 * (`threads[toolId].task`), else from the paired tool result: present+ok → done,
 * present+!ok → failed, absent → still running. The live state also carries the
 * progress line and usage the sub-agent view and rows show.
 */
export function selectSubAgents(turns: Turn[], threads: Record<string, SubThread> = {}): SubAgent[] {
  const out: SubAgent[] = []
  for (const turn of turns) {
    for (const b of turn.blocks) {
      if (b.kind !== 'tool' || !SUBAGENT_TOOL_NAMES.has(b.name)) continue
      const input = b.input as { subagent_type?: unknown; description?: unknown }
      const type = typeof input.subagent_type === 'string' ? input.subagent_type : 'agent'
      const description = typeof input.description === 'string' ? input.description : ''
      const task = threads[b.toolId]?.task
      const status: SubAgentStatus = task
        ? TASK_STATUS[task.status]
        : b.result == null ? 'running' : b.result.ok ? 'done' : 'failed'
      out.push({
        toolId: b.toolId, type, description, status,
        startedAt: b.ts ?? task?.startedAt,
        endedAt: task?.endedAt ?? b.result?.ts,
        ...(task && {
          taskId: task.taskId, activity: task.activity ?? null, lastTool: task.lastTool ?? null,
          totalTokens: task.totalTokens ?? null, toolUses: task.toolUses ?? null,
          summary: task.summary ?? null, backgrounded: task.backgrounded,
        }),
      })
    }
  }
  return out
}

/**
 * Count sub-agents (Task or Agent tool calls) that are still running (no
 * result yet, or live task state that hasn't reached a terminal status). Used
 * by the busy indicator to show a more specific label.
 *
 * Delegates to `selectSubAgents`, the one place `turns` and the reducer's
 * `state.subAgents` thread map are merged into a status per sub-agent — see
 * that function's docstring for how the two combine. Accepting the optional
 * `threads` here keeps this the single "how many sub-agents are running"
 * derivation (Principle VI) rather than a second one that only reads `turns`.
 */
export function selectRunningSubAgentCount(turns: Turn[], threads?: Record<string, SubThread>): number {
  return selectSubAgents(turns, threads).filter((a) => a.status === 'running').length
}

// ── Sub-agent tree (for the dashboard session-list drill-down) ────────────────

import type { SubAgentSummary } from '@/api/types'

export interface SubAgentNode extends SubAgentSummary {
  children: SubAgentNode[]
}

/**
 * Build a nested tree from a flat sub-agent summary list (from the session-list
 * API payload). Roots are entries whose `parentAgentId` is null; children nest
 * recursively to arbitrary depth via `parentAgentId` links.
 */
export function subAgentTreeFromSummaries(list: SubAgentSummary[]): SubAgentNode[] {
  const nodes = new Map<string, SubAgentNode>()
  for (const s of list) nodes.set(s.agentId, { ...s, children: [] })
  const roots: SubAgentNode[] = []
  for (const n of nodes.values()) {
    const parent = n.parentAgentId ? nodes.get(n.parentAgentId) : undefined
    if (parent) parent.children.push(n)
    else roots.push(n)
  }
  return roots
}
