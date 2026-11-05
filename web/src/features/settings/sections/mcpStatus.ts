// MCP status vocabulary shared by Settings › MCP and the in-session readout (spec 2026-10-09).
import { apiErrorCode } from '@/api/client'
import { extractMutationErrorMessage } from '@/api/queryClient'
import { relTime } from '@/lib/relativeTime'

const TONE: Record<string, McpTone> = { failed: 'bad', 'needs-auth': 'warn', pending: 'pending', connected: 'ok' }

export type McpTone = 'bad' | 'warn' | 'pending' | 'ok' | 'neutral' | 'unknown'

export function toneFor(status: string | null): McpTone {
  if (status == null) return 'unknown'
  return TONE[status] ?? 'neutral'
}

export function shortName(server: string): string {
  return server.startsWith('plugin:') ? server.slice(server.lastIndexOf(':') + 1) : server
}

/** "3m ago" — the shared relative-time formatter, re-exported under the MCP vocabulary. */
export const relativeAge = relTime

/** The MCP scope kill switch (`VIBERPIT_MCP_SCOPE_ENABLED=false`): its routes answer 404 `mcp_scope_disabled`. */
export function isMcpScopeDisabled(error: unknown): boolean {
  return apiErrorCode(error) === 'mcp_scope_disabled'
}

export const MCP_SCOPE_DISABLED_MESSAGE = 'MCP switches are turned off on this server.'

/** A failed MCP switch write as readable text: the kill switch's code never shows raw. */
export function mcpMutationErrorMessage(error: unknown, fallback: string): string {
  return isMcpScopeDisabled(error) ? MCP_SCOPE_DISABLED_MESSAGE : extractMutationErrorMessage(error, fallback)
}
