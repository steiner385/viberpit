/**
 * McpCommandPanel — the `/mcp` command result: the session's MCP servers (the
 * shared `McpServerList`) or an empty state, plus the Settings link. Reauth runs
 * in the session's own account; live `mcpStatus` servers win over the command's
 * snapshot (the readout's rule).
 * Loaded lazily with the readout's chunk (`DeferredMcpStatusReadout`) so the
 * reauth flow stays off the entry chunk (`npm run budget`).
 */
import { Link } from 'react-router-dom'
import type { McpServerState } from '@/api/types'
import type { SessionRuntime } from '@/session/useSessionRuntime'
import { settingsSectionPath } from '@/shell/navDefs'
import { McpServerList } from './McpServerList'

interface Props {
  /** The `/mcp` result payload, `{ servers }`. */
  data: unknown
  /** The session the panel acts on: its id, account (Reauth) and live `mcpStatus` servers. */
  rt: Pick<SessionRuntime, 'sessionId' | 'account' | 'state'>
  onClose: () => void
}

const NO_SERVERS: McpServerState[] = []

export function McpCommandPanel({ data, rt, onClose }: Props) {
  const snapshot = (data as { servers?: McpServerState[] } | null)?.servers ?? NO_SERVERS
  const servers = rt.state.mcpServers ?? snapshot
  return (
    <>
      <h2 className="mb-2 text-sm font-semibold text-ink">MCP servers</h2>
      <div className="text-xs">
        {servers.length === 0 ? (
          <p className="text-sm text-ink-muted">No MCP servers are configured for this session.</p>
        ) : (
          <McpServerList sessionId={rt.sessionId} account={rt.account} servers={servers} />
        )}
        <p className="pt-1.5">
          <Link to={settingsSectionPath('mcp')} onClick={onClose} className="text-accent hover:underline">
            Manage →
          </Link>
        </p>
      </div>
    </>
  )
}
