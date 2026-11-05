// Per-host outcome of a fanned-out plugin operation (D-16c, D2). A partial failure is
// reported, never rolled back, and a retry is idempotent on the server, so a failed host
// offers Retry.
import { Button } from '@/ui/Button'
import { Badge, type Tone } from '@/ui/Badge'
import type { PluginHostOutcome, PluginOpResult } from '@/api/types'
import { NO_HOSTS_COPY, clip } from './pluginText'

const LABEL: Record<PluginHostOutcome['outcome'], { text: string; tone: Tone }> = {
  ok: { text: 'ok', tone: 'success' },
  noop: { text: 'no change', tone: 'neutral' },
  failed: { text: 'failed', tone: 'danger' },
}

export interface OpRecord {
  /** What ran, e.g. "Install foo@market". */
  label: string
  result: PluginOpResult
  retry?: () => void
  /** Extra line shown when any host succeeded (the restart copy for install/update). */
  note?: string
}

export function HostOutcomes({ record, retrying = false, onDismiss }: {
  record: OpRecord
  retrying?: boolean
  onDismiss?: () => void
}) {
  const { result } = record
  const hosts = result.hosts ?? []
  const failed = hosts.filter((h) => h.outcome === 'failed').length
  if (hosts.length === 0) {
    // Zero hosts is never success: nothing ran anywhere.
    return (
      <div aria-live="polite" aria-label="Host results" role="group"
        className="mt-2 rounded-md border border-danger p-2 text-xs min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="min-w-0 break-words text-danger">{record.label}: {NO_HOSTS_COPY}</span>
          {record.retry && (
            <Button size="sm" variant="secondary" loading={retrying} onClick={record.retry}>Retry</Button>
          )}
          {onDismiss && (
            <Button size="sm" variant="ghost" onClick={onDismiss} aria-label="Dismiss host results">Dismiss</Button>
          )}
        </div>
      </div>
    )
  }
  const ok = result.ok && failed === 0
  const headline = ok
    ? `${record.label}: done on ${hosts.length === 1 ? 'the host' : `all ${hosts.length} hosts`}.`
    : `${record.label}: failed on ${failed} of ${hosts.length} host${hosts.length === 1 ? '' : 's'}.`
  return (
    <div
      aria-live="polite"
      aria-label="Host results"
      role="group"
      className={`mt-2 rounded-md border p-2 text-xs min-w-0 ${ok ? 'border-edge' : 'border-danger'}`}
    >
      <div className="flex items-center gap-2 flex-wrap">
        <span className={`min-w-0 break-words ${ok ? '' : 'text-danger'}`}>{headline}</span>
        {!ok && record.retry && (
          <Button size="sm" variant="secondary" loading={retrying} onClick={record.retry}>Retry</Button>
        )}
        {onDismiss && (
          <Button size="sm" variant="ghost" onClick={onDismiss} aria-label="Dismiss host results">Dismiss</Button>
        )}
      </div>
      {record.note && failed < hosts.length && <p className="mt-1">{record.note}</p>}
      <ul className="mt-1 space-y-1">
        {hosts.map((h) => (
          <li key={h.host} className="flex flex-wrap items-baseline gap-2 min-w-0">
            <span className="font-medium break-all"><bdi>{clip(h.host, 80)}</bdi></span>
            <Badge tone={LABEL[h.outcome]?.tone ?? 'neutral'}>{LABEL[h.outcome]?.text ?? h.outcome}</Badge>
            {(h.failureCode || h.message) && (
              <span className="min-w-0 break-words opacity-70">
                {h.failureCode && <code>{clip(h.failureCode, 80)}</code>}
                {h.failureCode && h.message ? ' — ' : ''}
                {h.message && <bdi>{clip(h.message, 500)}</bdi>}
              </span>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}
