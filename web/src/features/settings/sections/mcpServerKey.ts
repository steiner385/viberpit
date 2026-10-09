// Settings-only (kept out of mcpStatus.ts, which the preloaded in-session readout imports).
import type { LibraryMcpServer } from '@/api/types'

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
