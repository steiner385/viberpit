// MCP status vocabulary shared by Settings › MCP and the in-session readout (spec 2026-10-09).
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
