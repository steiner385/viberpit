// MCP status vocabulary shared by Settings › MCP and the in-session readout (spec 2026-10-09).
import { relTime } from '@/lib/relativeTime'
import type { LibraryMcpServer } from '@/api/types'

const RANK: Record<string, number> = { failed: 4, 'needs-auth': 3, pending: 2, connected: 0, disabled: 0 }
const TONE: Record<string, McpTone> = { failed: 'bad', 'needs-auth': 'warn', pending: 'pending', connected: 'ok' }

export type McpTone = 'bad' | 'warn' | 'pending' | 'ok' | 'neutral' | 'unknown'

export function worstStatus(statuses: string[]): string | null {
  if (statuses.length === 0) return null
  // Unknown statuses rank 1: above healthy (worth showing), below every known problem.
  return statuses.reduce((a, b) => ((RANK[b] ?? 1) > (RANK[a] ?? 1) ? b : a))
}

export function toneFor(status: string | null): McpTone {
  if (status == null) return 'unknown'
  return TONE[status] ?? 'neutral'
}

export function shortName(server: string): string {
  return server.startsWith('plugin:') ? server.slice(server.lastIndexOf(':') + 1) : server
}

/**
 * The key the server and the CLI file a server under (mirror of `server_key` in
 * server/mcp_scope/servers.py): the bare name for user servers, `plugin:<manifest>:<name>` for
 * plugin servers. `pluginName` is the plugin MANIFEST name (the CLI keys registry id
 * `notion@claude-plugins-official` as `plugin:Notion:notion`), never derived from `source`.
 * Without `pluginName` it falls back to the registry name (the part of `source` before `@`),
 * the same fallback `_plugin_manifest_name` in server/library/scan.py applies.
 */
export function serverKey(s: Pick<LibraryMcpServer, 'name' | 'source' | 'pluginName'>): string {
  if (s.source === 'user') return s.name
  return `plugin:${s.pluginName || s.source.split('@')[0]}:${s.name}`
}

/** "3m ago" — the shared relative-time formatter, re-exported under the MCP vocabulary. */
export const relativeAge = relTime
