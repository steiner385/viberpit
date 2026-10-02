/**
 * Derived views over transcript state for the session status strip.
 */
import type { Block, Turn, SubThread, SubThreadTaskStatus, TranscriptState, BackgroundTaskState, BackgroundTaskStatus } from './reducer'
import type { BackgroundWork } from '@/api/types'

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

// ── Background work (#2635) ───────────────────────────────────────────────────

export type BackgroundWorkKind = 'shell' | 'monitor' | 'terminal' | 'workflow' | 'task'
export interface BackgroundWorkRow {
  toolUseId: string
  kind: BackgroundWorkKind
  description: string
  status: BackgroundTaskStatus
  startedAt?: number
  endedAt?: number
  /** True when the CLI's live task notices back this row — Stop can act on it. */
  live: boolean
}
export interface BackgroundWorkView { rows: BackgroundWorkRow[]; wakeup: boolean; wakeupAt: number | null }
export const EMPTY_BACKGROUND_WORK_VIEW: BackgroundWorkView = { rows: [], wakeup: false, wakeupAt: null }

type ToolBlock = Extract<Block, { kind: 'tool' }>

function bgKind(toolName: string | undefined, taskType: string | undefined, snapshotKind: string | undefined): BackgroundWorkKind {
  if (toolName === 'Monitor' || snapshotKind === 'monitor') return 'monitor'
  if (snapshotKind === 'terminal') return 'terminal'
  if (taskType === 'local_workflow') return 'workflow'
  if (toolName === 'Bash' || taskType === 'local_bash') return 'shell'
  return 'task'
}
function bgDescription(b: ToolBlock | undefined): string {
  const i = (b?.input ?? {}) as { description?: unknown; command?: unknown }
  if (typeof i.description === 'string' && i.description) return i.description
  return typeof i.command === 'string' ? i.command : ''
}
function launchedInBackground(l: BackgroundTaskState, b: ToolBlock | undefined): boolean {
  return l.backgrounded || b?.name === 'Monitor' || (b?.input as { run_in_background?: unknown } | undefined)?.run_in_background === true
}

/**
 * Merge the server's resting snapshot (`Session.backgroundWork`) with the live task
 * state (`state.backgroundTasks`), the way selectSubAgents merges turns + threads:
 * snapshot items are running unless the stream saw them end; live entries not in the
 * snapshot show only when launched in the background (a foreground Bash is also a
 * `local_bash` task and must never appear here).
 */
export function selectBackgroundWork(turns: Turn[], live: Record<string, BackgroundTaskState>, snapshot?: BackgroundWork | null): BackgroundWorkView {
  const blocks = new Map<string, ToolBlock>()
  for (const t of turns) for (const b of t.blocks) if (b.kind === 'tool') blocks.set(b.toolId, b)
  const rows = new Map<string, BackgroundWorkRow>()
  const snapshotIds = new Set<string>()
  for (const item of snapshot?.items ?? []) {
    snapshotIds.add(item.toolUseId)
    const l = live[item.toolUseId]
    if (l && l.status !== 'running') continue // the stream saw it end
    const b = blocks.get(item.toolUseId)
    const parsed = item.startedAt ? Date.parse(item.startedAt) : NaN
    const started = l?.startedAt ?? b?.ts ?? (Number.isFinite(parsed) ? parsed : undefined)
    rows.set(item.toolUseId, {
      toolUseId: item.toolUseId, kind: bgKind(b?.name ?? item.name, l?.taskType, item.kind),
      description: item.description || bgDescription(b), status: 'running',
      ...(started != null && { startedAt: started }), live: l != null,
    })
  }
  for (const [toolUseId, l] of Object.entries(live)) {
    if (snapshotIds.has(toolUseId)) continue
    const b = blocks.get(toolUseId)
    if (!launchedInBackground(l, b)) continue
    rows.set(toolUseId, {
      toolUseId, kind: bgKind(b?.name, l.taskType, undefined), description: l.description || bgDescription(b),
      status: l.status, ...((l.startedAt ?? b?.ts) != null && { startedAt: (l.startedAt ?? b?.ts) as number }), ...(l.endedAt != null && { endedAt: l.endedAt }), live: true,
    })
  }
  const parsedWake = snapshot?.wakeupAt ? Date.parse(snapshot.wakeupAt) : NaN
  return {
    rows: [...rows.values()].sort((a, b) => (b.startedAt ?? 0) - (a.startedAt ?? 0)),
    wakeup: snapshot?.wakeup ?? false,
    wakeupAt: Number.isFinite(parsedWake) ? parsedWake : null,
  }
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
