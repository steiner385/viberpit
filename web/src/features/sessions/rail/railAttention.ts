/**
 * Whether the session rail holds something worth a glance while it is out of
 * sight — the Details tab on a narrow pane, the collapsed rail on a wide one.
 * The one predicate behind both attention dots (D-6), so they cannot disagree.
 */
import type { McpServerState } from '@/api/types'
import type { BackgroundWorkView, SubAgent } from '@/session/selectors'

export interface RailAttentionInputs {
  /** `rt.backgroundWork`: shells / Monitors / workflows and the armed wakeup. */
  backgroundWork: BackgroundWorkView
  /** `rt.subAgents`. */
  subAgents: SubAgent[]
  /** Live MCP status with the snapshot fallback (`useMcpServers`). */
  mcpServers: McpServerState[]
}

export function railNeedsAttention({ backgroundWork, subAgents, mcpServers }: RailAttentionInputs): boolean {
  if (backgroundWork.wakeup || backgroundWork.rows.some((r) => r.status === 'running')) return true
  if (subAgents.some((a) => a.status === 'failed')) return true
  // The deleted `MCP n/m` pill's warn rule (worst status failed or needs-auth;
  // those rank top in `mcpStatus.ts`, so "any server is" is the same test).
  // Not `worstStatus` itself: importing it splits a preloaded chunk off (budget).
  return mcpServers.some((s) => s.status === 'failed' || s.status === 'needs-auth')
}
