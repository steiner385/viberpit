import { useEffect, useMemo, useState } from 'react'

export interface NoticeCardData {
  title: string
  body: string
  url?: string | null
  ts?: number
}

interface Props {
  notice: NoticeCardData
  /** Absent for a read-only transcript (e.g. ReviewView) — the button hides. */
  onAsk?: (text: string) => void
  /** Injected for deterministic relative-time rendering in tests; defaults to the real clock. */
  now?: number
}

/**
 * A proactive watch notice inline in the assistant transcript (spec v2 §4.7 Pack N,
 * Task 8.5). "Ask about it" hands the composer a ready-to-send question rather than
 * asking it itself, so the user can edit or add context before sending.
 */
export function NoticeCard({ notice, onAsk, now: nowProp }: Props) {
  // Date.now() is impure and may not run during render — captured in a mount effect
  // instead (same pattern as WorkingIndicator's elapsed-time readout). `now` stays
  // null for the very first render when the caller doesn't inject a clock, so the
  // relative-time label appears a tick after mount rather than showing a stale "0".
  const [mountedNow, setMountedNow] = useState<number | null>(null)
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- Date.now() is impure and can't run during render
    setMountedNow(Date.now())
  }, [])
  const now = nowProp ?? mountedNow
  const relative = useMemo(() => (now == null ? null : formatRelative(notice.ts, now)), [notice.ts, now])
  return (
    <div className="my-2 rounded-lg border border-edge bg-surface-raised p-3 text-sm">
      <div className="flex items-start justify-between gap-2">
        <p className="font-medium text-ink">{notice.title}</p>
        {relative && <span className="shrink-0 text-xs text-ink-muted">{relative}</span>}
      </div>
      {notice.body && <p className="mt-1 text-ink-muted">{notice.body}</p>}
      <div className="mt-2 flex items-center gap-3">
        {notice.url && (
          <a href={notice.url} className="text-xs font-medium text-accent hover:underline">
            View
          </a>
        )}
        {onAsk && (
          <button
            type="button"
            onClick={() => onAsk(`About "${notice.title}": what happened and what should I do?`)}
            className="text-xs font-medium text-accent hover:underline"
          >
            Ask about it
          </button>
        )}
      </div>
    </div>
  )
}

function formatRelative(ts: number | undefined, now: number): string | null {
  if (!ts) return null
  const mins = Math.round((now - ts) / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hours = Math.round(mins / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.round(hours / 24)
  return `${days}d ago`
}
