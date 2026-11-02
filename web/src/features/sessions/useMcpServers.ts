import { useSessionMcp } from '@/api/hooks'
import type { McpServerState } from '@/api/types'

const NO_SERVERS: McpServerState[] = []

/** The session's MCP servers: live `mcpStatus` events (`rt.state.mcpServers`)
 *  win; the GET snapshot (fetched only while there is no live status) fills in
 *  until the first one. The one copy — the rail's inline list and the rail
 *  attention dot both read it, so they cannot disagree about a server. */
export function useMcpServers(sessionId: string, live: McpServerState[] | null): McpServerState[] {
  const snapshot = useSessionMcp(sessionId, live == null)
  return live ?? snapshot.data?.servers ?? NO_SERVERS
}
