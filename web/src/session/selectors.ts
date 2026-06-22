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
 *
 * Intentionally derived from the parent thread's `turns` (the Task tool block +
 * its result), NOT from the reducer's `state.subAgents` map. The two are
 * complementary, not redundant: `state.subAgents` holds each sub-agent's nested
 * thread *content* and is only created once the sub-agent emits its first child
 * event, whereas the spawning Task block appears in `turns` immediately. Reading
 * `turns` therefore reflects a just-spawned sub-agent as "running" before any
 * child event arrives — the more responsive signal for a live busy indicator.
 */
export function selectRunningSubAgentCount(turns: Turn[]): number {
  return selectSubAgents(turns).filter((a) => a.status === 'running').length
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
