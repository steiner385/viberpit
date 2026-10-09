// MCP status vocabulary shared by Settings › MCP and the in-session readout (spec 2026-10-09).
import { relTime } from '@/lib/relativeTime'

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

/** "3m ago" — the shared relative-time formatter, re-exported under the MCP vocabulary. */
export const relativeAge = relTime
