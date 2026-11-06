// Per-session outcome of a live plugin reload (D-16c residuals). A held session keeps its
// plugins until it restarts (its prompt cache is worth more); a failed host has no
// session row of its own, so it shows under the host name. Names and messages are untrusted.
import { Badge, type Tone } from '@/ui/Badge'
import type { PluginReloadResult, PluginSessionOutcome } from '@/api/types'
import { NO_SESSIONS_COPY, clip } from './pluginText'

const LABEL: Record<PluginSessionOutcome['outcome'], Tone> = {
  applied: 'success',
  held: 'warning',
  busy: 'neutral',
  skipped: 'neutral',
  failed: 'danger',
}

export function SessionOutcomes({ result }: { result: PluginReloadResult }) {
  const rows = result.sessions ?? []
  if (rows.length === 0) {
    return <p aria-live="polite" className="mt-2 text-xs opacity-70">{NO_SESSIONS_COPY}</p>
  }
  return (
    <div
      aria-live="polite"
      aria-label="Session results"
      role="group"
      className={`mt-2 rounded-md border p-2 text-xs min-w-0 ${result.ok ? 'border-edge' : 'border-danger'}`}
    >
      <ul className="space-y-1">
        {rows.map((r, i) => (
          <li key={`${r.host}:${r.sessionId ?? i}`} className="flex flex-wrap items-baseline gap-2 min-w-0">
            <span className="font-medium break-all"><bdi>{clip(r.name ?? r.host, 80)}</bdi></span>
            <Badge tone={LABEL[r.outcome] ?? 'neutral'}>{r.outcome}</Badge>
            {(r.failureCode || r.message) && (
              <span className="min-w-0 break-words opacity-70">
                {r.failureCode && <code>{clip(r.failureCode, 80)}</code>}
                {r.failureCode && r.message ? ' — ' : ''}
                {r.message && <bdi>{clip(r.message, 500)}</bdi>}
              </span>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}
