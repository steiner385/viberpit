/**
 * One MCP server's tools as switches (D-16b), shared by Settings › MCP (the tool's default) and
 * the session Context card (a live session override). Presentational: the caller owns the data
 * and the mutation. Not optimistic — `checked` is always the server's state, so a refused toggle
 * simply stays where it was.
 */
import type { McpTool } from '@/api/types'
import { Badge } from '@/ui/Badge'
import { Switch } from '@/ui/Switch'

interface Props {
  /** The server's display name, for the list's accessible name. */
  server: string
  tools: McpTool[]
  onToggle: (tool: string, enabled: boolean) => void
  /** True while a toggle is in flight: every switch waits for the server's answer. */
  busy: boolean
  /** The tool whose toggle is in flight, shown as "Saving…". */
  pendingTool?: string | null
}

export function McpToolSwitchList({ server, tools, onToggle, busy, pendingTool = null }: Props) {
  if (tools.length === 0) return <p className="text-xs text-ink-muted">No tools.</p>
  return (
    <ul aria-label={`${server} tools`} className="space-y-1">
      {tools.map((t) => (
        <li key={t.name} className="flex min-w-0 items-center gap-2 text-xs">
          <div className="min-w-0 flex-1">
            <div className="flex min-w-0 flex-wrap items-center gap-1">
              <span className="truncate font-mono text-ink" title={t.name}>{t.name}</span>
              {t.annotations.readOnly === true && <Badge tone="neutral">read-only</Badge>}
              {t.annotations.destructive === true && <Badge tone="warning">destructive</Badge>}
              {t.source === 'session' && <Badge tone="accent">Session</Badge>}
              {pendingTool === t.name && <span className="text-ink-muted">Saving…</span>}
            </div>
            {t.description && (
              <p className="truncate text-ink-muted" title={t.description}>{t.description}</p>
            )}
          </div>
          <Switch
            checked={t.enabled}
            disabled={busy}
            aria-label={`${t.name} enabled`}
            onChange={(next) => onToggle(t.name, next)}
          />
        </li>
      ))}
    </ul>
  )
}
