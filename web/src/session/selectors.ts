/**
 * Derived views over transcript state for the session status strip.
 */
import type { TranscriptState, Turn } from './reducer'

export type SubAgentStatus = 'running' | 'done' | 'failed'

export interface SubAgent {
  toolId: string
  type: string         // subagent_type, e.g. "Explore" (falls back to "agent")
  description: string  // the Task description (falls back to "")
  status: SubAgentStatus
}

/**
 * Extract the list of sub-agents from the transcript.
 *
 * Sub-agents surface to the parent session as `Task` tool calls; the parent
 * never sees their internal steps, only the spawn and (eventually) the result.
 * Status is derived from the paired tool result: present+ok → done,
 * present+!ok → failed, absent → still running.
 */
export function selectTurnsForAgent(turns: Turn[], agentId: string | null): Turn[] {
  return turns.filter((t) => (t.agentId ?? null) === agentId)
}

export interface SubAgentNode extends SubAgent {
  agentId: string
  parentAgentId: string | null
  children: SubAgentNode[]
}

export function selectSubAgentTree(subAgents: TranscriptState['subAgents']): SubAgentNode[] {
  const nodes = new Map<string, SubAgentNode>()
  for (const s of Object.values(subAgents)) {
    nodes.set(s.agentId, {
      agentId: s.agentId,
      toolId: s.agentId,
      type: s.agentType,
      description: s.description,
      status: s.status,
      parentAgentId: s.parentAgentId,
      children: [],
    })
  }
  const roots: SubAgentNode[] = []
  for (const n of nodes.values()) {
    const parent = n.parentAgentId ? nodes.get(n.parentAgentId) : undefined
    if (parent) parent.children.push(n)
    else roots.push(n)
  }
  return roots
}

export function selectSubAgents(turns: Turn[]): SubAgent[] {
  const out: SubAgent[] = []
  for (const turn of turns) {
    for (const b of turn.blocks) {
      if (b.kind !== 'tool' || b.name !== 'Task') continue
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
