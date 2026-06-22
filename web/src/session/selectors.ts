/**
 * Derived views over transcript state for the session status strip.
 */
import type { Turn } from './reducer'

export type SubAgentStatus = 'running' | 'done' | 'failed'

export interface SubAgent {
  toolId: string
  type: string         // subagent_type, e.g. "Explore" (falls back to "agent")
  description: string  // the Task description (falls back to "")
  status: SubAgentStatus
}

const SUBAGENT_TOOL_NAMES = new Set(['Task', 'Agent'])

/**
 * Extract the list of sub-agents from the transcript.
 *
 * Sub-agents surface to the parent session as `Task` or `Agent` tool calls;
 * the parent never sees their internal steps, only the spawn and (eventually)
 * the result. Status is derived from the paired tool result: present+ok →
 * done, present+!ok → failed, absent → still running.
 */
export function selectSubAgents(turns: Turn[]): SubAgent[] {
  const out: SubAgent[] = []
  for (const turn of turns) {
    for (const b of turn.blocks) {
      if (b.kind !== 'tool' || !SUBAGENT_TOOL_NAMES.has(b.name)) continue
      const input = b.input as { subagent_type?: unknown; description?: unknown }
      const type = typeof input.subagent_type === 'string' ? input.subagent_type : 'agent'
      const description = typeof input.description === 'string' ? input.description : ''
      const status: SubAgentStatus =
        b.result == null ? 'running' : b.result.ok ? 'done' : 'failed'
      out.push({ toolId: b.toolId, type, description, status })
    }
  }
  return out
}

/**
 * Count sub-agents (Task or Agent tool calls) that are still running (no
 * result yet). Used by the busy indicator to show a more specific label.
 */
export function selectRunningSubAgentCount(turns: Turn[]): number {
  return selectSubAgents(turns).filter((a) => a.status === 'running').length
}
