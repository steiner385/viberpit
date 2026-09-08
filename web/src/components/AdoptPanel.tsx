import { useRef, useState } from 'react'
import { BRAND_NAME } from '@/brand'
import { cn } from '@/lib/utils'
import { relTime } from '@/lib/relativeTime'
import { useAdoptable, useAdoptSession } from '@/api/hooks'
import { Dialog } from '@/ui/Dialog'
import { Button } from '@/ui/Button'
import type { AdoptionCandidate } from '@/api/types'

/** Plain-language copy for skip reasons (UX SHOULD #5 — never show machine codes). */
const SKIP_COPY: Record<string, string> = {
  unmapped: 'Not a managed account — running under an unmanaged config dir (e.g. ~/.claude).',
  tracked: `Already managed by ${BRAND_NAME}.`,
}

/** Middle-elide a long path so the repo basename stays visible. */
function elide(path: string, max = 48): string {
  if (path.length <= max) return path
  const head = path.slice(0, max - 20)
  const tail = path.slice(-17)
  return `${head}…${tail}`
}

interface ConfirmProps {
  candidate: AdoptionCandidate
  onCancel: () => void
  onConfirm: () => void
}

function ConfirmDialog({ candidate, onCancel, onConfirm }: ConfirmProps) {
  const [ack, setAck] = useState(false)
  const active = candidate.pidAlive
  const cancelRef = useRef<HTMLButtonElement>(null)
  return (
    <Dialog open onClose={onCancel} title="Take over this session?" size="md" initialFocus={cancelRef}>
      <p className="text-sm text-ink-muted">
        {BRAND_NAME} will {active ? 'stop' : 'resume'} the <code>claude</code> process running in{' '}
        <b>{elide(candidate.cwd)}</b>
        {candidate.pid != null && active ? <> (PID {candidate.pid})</> : null} under{' '}
        <b>{candidate.account}</b> and resume the conversation <b>&ldquo;{candidate.name}&rdquo;</b>{' '}
        here.{' '}
        {active && 'Any unsaved work in that terminal will be lost. '}
        This can&rsquo;t be undone.
      </p>
      {active && (
        <label className="flex items-center gap-2 text-sm text-ink-muted">
          <input
            type="checkbox"
            checked={ack}
            onChange={(e) => setAck(e.target.checked)}
          />
          I understand this will stop PID {candidate.pid}.
        </label>
      )}
      <div className="flex justify-end gap-2">
        <Button ref={cancelRef} type="button" variant="secondary" onClick={onCancel}>
          Cancel
        </Button>
        <Button type="button" variant="danger" size="lg" disabled={active && !ack} onClick={onConfirm}>
          Stop &amp; take over
        </Button>
      </div>
    </Dialog>
  )
}

export function AdoptPanel() {
  const { data } = useAdoptable()
  const adoptMutation = useAdoptSession()
  const [confirming, setConfirming] = useState<AdoptionCandidate | null>(null)

  const candidates = data?.candidates ?? []
  if (candidates.length === 0) {
    return (
      <p className="text-sm text-gray-500 dark:text-gray-400">
        All running sessions are managed.
      </p>
    )
  }

  const adoptable = candidates.filter((c) => c.mapped && !c.tracked)
  const skipped = candidates.filter((c) => !(c.mapped && !c.tracked))

  return (
    <section className="space-y-2">
      <h2 className="text-sm font-semibold text-gray-900 dark:text-gray-100">Untracked sessions</h2>

      {adoptable.map((c) => (
        <div
          key={c.claudeSessionId}
          title={`claude session ${c.claudeSessionId}`}
          className="flex items-center gap-3 rounded-lg border border-gray-200 dark:border-gray-700 p-3"
        >
          <div className="flex-1 min-w-0">
            <div className="font-medium text-gray-900 dark:text-gray-100 truncate">{c.name}</div>
            <div className="text-xs text-gray-500 dark:text-gray-400 truncate">
              {c.account} · {elide(c.cwd)} · {relTime(c.lastActivity)}
              {c.pid != null && <> · PID {c.pid}</>}
            </div>
          </div>
          <button
            type="button"
            onClick={() => setConfirming(c)}
            className={cn(
              'rounded-md px-3 py-1.5 text-sm font-medium whitespace-nowrap',
              c.pidAlive
                ? 'bg-red-600 hover:bg-red-700 text-white'
                : 'border border-gray-300 dark:border-gray-600',
            )}
          >
            {c.pidAlive ? 'Stop & take over' : 'Manage this session'}
          </button>
        </div>
      ))}

      {skipped.length > 0 && (
        <details className="text-sm">
          <summary className="cursor-pointer text-gray-600 dark:text-gray-400">
            Not adoptable ({skipped.length})
          </summary>
          <div className="mt-2 space-y-1">
            {skipped.map((c) => (
              <div
                key={c.claudeSessionId}
                className="flex items-center gap-2 rounded-md px-2 py-1 text-gray-500 dark:text-gray-400"
              >
                <span className="truncate">{c.name}</span>
                <span className="text-xs">
                  — {c.skipReason ? SKIP_COPY[c.skipReason] : 'Not adoptable.'}
                </span>
              </div>
            ))}
          </div>
        </details>
      )}

      {confirming && (
        <ConfirmDialog
          candidate={confirming}
          onCancel={() => setConfirming(null)}
          onConfirm={() => {
            adoptMutation.mutate(confirming.claudeSessionId)
            setConfirming(null)
          }}
        />
      )}
    </section>
  )
}
