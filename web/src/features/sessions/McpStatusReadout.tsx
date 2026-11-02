/**
 * The session's MCP servers, inline in the rail's Context card (D-6; the
 * `MCP n/m` strip popover went with `SessionMeta`). Live `mcpStatus` events
 * win; the GET snapshot fills in until the first event. Engines without MCP
 * status report no servers, so it renders nothing. The body is
 * `McpServerList`, shared with the `/mcp` command panel, which rides this
 * module's lazy chunk (`DeferredMcpStatusReadout`).
 */
import { Link } from 'react-router-dom'
import type { McpServerState } from '@/api/types'
import { settingsSectionPath } from '@/shell/navDefs'
import { McpServerList } from './McpServerList'
import { useMcpServers } from './useMcpServers'

interface Props {
  sessionId: string
  account: string
  live: McpServerState[] | null
}

/** The server list plus the `Manage →` link into Settings → MCP. */
function McpServerPanel({ sessionId, account, servers }: { sessionId: string; account: string; servers: McpServerState[] }) {
  return (
    <>
      <McpServerList sessionId={sessionId} account={account} servers={servers} />
      <div className="py-1.5 text-xs">
        <Link to={settingsSectionPath('mcp')} className="text-accent hover:underline">
          Manage →
        </Link>
      </div>
    </>
  )
}

/** The same list, inline (no popover) — the session rail's Context card.
 *  Renders nothing when the session reports no servers. */
export function McpInlineList({ sessionId, account, live }: Props) {
  const servers = useMcpServers(sessionId, live)
  if (servers.length === 0) return null
  return (
    <div role="group" aria-label="MCP servers" className="min-w-0">
      <p className="text-ink-muted">MCP servers</p>
      <McpServerPanel sessionId={sessionId} account={account} servers={servers} />
    </div>
  )
}

// The `/mcp` panel shares this chunk (see `DeferredMcpStatusReadout`).
export { McpCommandPanel } from './McpCommandPanel'
