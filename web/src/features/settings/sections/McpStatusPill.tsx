import { cn } from '@/lib/utils'
import { toneFor, type McpTone } from './mcpStatus'

const LABEL: Record<string, string> = {
  connected: 'Connected',
  failed: 'Failed',
  'needs-auth': 'Needs auth',
  pending: 'Connecting…',
  disabled: 'Disabled',
}

const TONE_CLASS: Record<McpTone, string> = {
  bad: 'bg-danger-wash text-danger border-danger',
  warn: 'bg-warning-wash text-warning border-warning',
  pending: 'text-ink-muted border-edge',
  ok: 'bg-success-wash text-success border-success',
  neutral: 'text-ink-muted border-edge',
  unknown: 'text-ink-muted border-edge border-dashed',
}

/** Status chip for an MCP server (Settings › MCP and the in-session readout). null = never checked. */
export function McpStatusPill({ status }: { status: string | null }) {
  const label = status == null ? 'Not checked yet' : (LABEL[status] ?? status)
  return (
    <span className={cn('text-xs px-1.5 py-0.5 rounded-full border', TONE_CLASS[toneFor(status)])}>
      {label}
    </span>
  )
}
