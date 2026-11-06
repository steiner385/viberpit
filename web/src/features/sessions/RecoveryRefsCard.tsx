/**
 * Recovery snapshots on an archived session's page (D-21).
 *
 * A Discard — and a close, delete, drain or forced removal — keeps the worktree's
 * work as recovery refs on the clone; until now they were only in a toast or a log.
 * This card lists every one (reason, short SHA, date) with ITS restore command, so
 * an older snapshot is as restorable as the newest. The command copies through the
 * same `CopyablePath` control the Discard toast uses (pointer, Enter or Space; the
 * copy is announced). A ref with no command (an edge that predates the per-entry
 * field) shows its full name as plain text instead, never only in a tooltip.
 *
 * Lazy (`ArchivedReviewPage` imports it through `import()`), so the archived page's
 * eager route pays nothing for it. Supplementary on a read-only page: while loading,
 * on any error (an unknown session, an old daemon) and with no refs it renders
 * nothing. Refs, reasons and commands are untrusted text, never markup.
 */
import { useId } from 'react'
import { useSessionRecoveryRefs } from '@/api/hooks'
import { isValidTimestamp } from '@/lib/relativeTime'
import { Card } from '@/ui/Card'
import { SectionHeading } from '@/ui/SectionHeading'
import { CopyablePath } from './transcript/CopyablePath'

/** Git's own short-SHA length. */
const SHORT_SHA = 7

export default function RecoveryRefsCard({ sessionId }: { sessionId: string }) {
  const headingId = useId()
  const { data } = useSessionRecoveryRefs(sessionId)
  // A body without a `refs` array (an edge that predates the route behind a
  // catch-all) is as quiet as an error.
  if (!data || !Array.isArray(data.refs) || data.refs.length === 0) return null

  return (
    <Card
      as="section"
      padding="sm"
      aria-labelledby={headingId}
      data-testid="recovery-refs-card"
      className="mx-4 mt-3 shrink-0 space-y-2 md:mx-6"
    >
      <SectionHeading id={headingId}>Recovery snapshots</SectionHeading>
      <p className="text-xs text-ink-muted">
        Work saved when this session&apos;s worktree was removed. Select a restore command to copy it.
      </p>
      <ul className="space-y-2">
        {data.refs.map((r) => (
          <li key={r.ref} className="min-w-0 space-y-0.5">
            <p className="flex flex-wrap items-baseline gap-x-2 text-sm">
              <span className="text-ink">{r.reason}</span>
              <code className="font-mono text-xs text-ink-muted">{r.sha.slice(0, SHORT_SHA)}</code>
              {isValidTimestamp(r.createdAt) && (
                <span className="text-xs text-ink-muted">{new Date(r.createdAt).toLocaleString()}</span>
              )}
            </p>
            <p className="min-w-0 break-all font-mono text-xs text-ink">
              {r.restoreCommand
                ? <CopyablePath text={r.restoreCommand} title="Copy restore command" />
                : <code className="text-ink-muted">{r.ref}</code>}
            </p>
          </li>
        ))}
      </ul>
    </Card>
  )
}
