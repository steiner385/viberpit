/**
 * "Move to host…" (D-17) — close an idle Claude session on its host and resume the
 * same conversation on another fleet host, worktree and all.
 *
 * Lazy (`SessionHeader` imports it on open), so neither it nor its hooks
 * (`api/moveHooks`) nor the reason map ride in the entry bundle.
 *
 * Flow, per the server contract (`GET …/move/preflight`, `POST …/move`):
 *  1. Pick a target in the Runner-style select (enabled fleet hosts, the session's
 *     own host excluded). Picking one runs the preflight for it.
 *  2. A refusal shows its plain-language reason (`moveReasons.ts`) and Move stays off.
 *  3. A dirty tree in a PRIVATE repo lists the paths and asks for consent to carry
 *     them; in a public repo — or one whose visibility is unknown — it is refused
 *     outright (owner ruling, revised D1): consent cannot fix that.
 *  4. Background tasks that would stop are listed and need a confirm.
 *  5. Move posts `{host, carryDirty, stopBackgroundTasks}` and shows the outcome.
 *
 * Host names and file paths are untrusted: they are rendered as text, never markup,
 * and every one truncates so a long value cannot widen a 390px dialog.
 */
import { useId, useState } from 'react'
import { Loader2 } from 'lucide-react'
import { cn } from '@/lib/utils'
import { apiErrorCode, ApiRequestError } from '@/api/client'
import { useEnabledFleetHosts } from '@/api/fleet'
import { useMovePreflight, useMoveSession } from '@/api/moveHooks'
import { movingLabel } from '@/api/sessionMove'
import type { MovePreflight } from '@/api/types'
import { Button } from '@/ui/Button'
import { Dialog } from '@/ui/Dialog'
import { CARRY_DIRTY_COPY, PUBLIC_REPO_COPY, moveReasonText } from './moveReasons'

/** Dirty paths shown before the "+N more" line. */
export const DIRTY_LIST_CAP = 8

export interface MoveSessionDialogProps {
  sessionId: string
  /** The session's current host (excluded from the targets); null when unknown. */
  source: string | null
  onClose: () => void
  /** The session is usable again — on the target, or restored on the source. The
   *  host re-attaches its socket. */
  onSettled?: () => void
}

type Outcome =
  | { kind: 'moved'; host: string }
  | { kind: 'restored'; host: string; reason: string }
  | { kind: 'failed' }

/** `{detail: {code, message, outcome?, …}}` off a failed request. */
function errorDetail(err: unknown): { code: string | null; message: string | null; outcome: string | null } {
  const code = apiErrorCode(err)
  const detail = err instanceof ApiRequestError ? err.body?.detail : null
  const rec = detail && typeof detail === 'object' ? (detail as Record<string, unknown>) : {}
  return {
    code,
    message: typeof rec.message === 'string' ? rec.message : null,
    outcome: typeof rec.outcome === 'string' ? rec.outcome : null,
  }
}

/** One line of untrusted text that truncates instead of wrapping the dialog wider. */
function Clip({ text, className }: { text: string; className?: string }) {
  return <span title={text} className={cn('block min-w-0 truncate', className)}>{text}</span>
}

/** A consent tick. The `ui/Checkbox` look, inline: importing that primitive into
 *  this lazy chunk re-split the shared chunks and cost the entry bundle ~0.1 kB. */
function Consent({ label, checked, onChange }: { label: string; checked: boolean; onChange: (next: boolean) => void }) {
  return (
    <label className="flex items-start gap-2 min-h-target text-sm text-ink cursor-pointer">
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="mt-0.5 h-4 w-4 flex-shrink-0 rounded-sm border-edge-strong accent-accent focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
      />
      <span className="min-w-0">{label}</span>
    </label>
  )
}

export default function MoveSessionDialog({ sessionId, source, onClose, onSettled }: MoveSessionDialogProps) {
  const selectId = useId()
  const hosts = useEnabledFleetHosts().filter((h) => h.name !== source)
  const [target, setTarget] = useState('')
  const [carryDirty, setCarryDirty] = useState(false)
  const [stopBackground, setStopBackground] = useState(false)
  const [outcome, setOutcome] = useState<Outcome | null>(null)
  const [refusal, setRefusal] = useState<string | null>(null)
  const preflight = useMovePreflight(sessionId, target)
  const move = useMoveSession()

  function choose(host: string) {
    setTarget(host)
    setCarryDirty(false)
    setStopBackground(false)
    setRefusal(null)
  }

  const pf: MovePreflight | undefined = preflight.data
  // Per-host verdicts from the last preflight: a host known to refuse is greyed out.
  const hostVerdict = new Map((pf?.hosts ?? []).map((h) => [h.name, h]))

  const dirtyCount = pf?.dirty?.count ?? 0
  const dirtyPaths = pf?.dirty?.paths ?? []
  const dirty = dirtyCount > 0
  const privateRepo = pf?.repoPrivate === true
  const tasks = pf?.backgroundTasks ?? []

  // Why Move is off, in plain language — or null when the verdict allows it.
  let blocked: string | null = null
  if (preflight.isError) {
    const d = errorDetail(preflight.error)
    blocked = moveReasonText(d.code, d.message)
  } else if (pf) {
    if (pf.factsError) blocked = moveReasonText(pf.factsError)
    else if (!pf.verdict.ok) blocked = moveReasonText(pf.verdict.reason, pf.verdict.message)
    // Defence in depth: a dirty tree is carried only from a repo GitHub says is private.
    else if (dirty && !privateRepo) blocked = PUBLIC_REPO_COPY
  }
  const needsConsent = pf !== undefined && blocked === null && dirty && privateRepo
  const needsConfirm = pf !== undefined && blocked === null && tasks.length > 0
  const canMove = target !== '' && pf !== undefined && !preflight.isFetching && blocked === null
    && (!needsConsent || carryDirty) && (!needsConfirm || stopBackground) && !move.isPending

  function submit() {
    if (!canMove) return
    setRefusal(null)
    move.mutate(
      { sessionId, host: target, carryDirty: needsConsent && carryDirty, stopBackgroundTasks: needsConfirm && stopBackground },
      {
        onSuccess: (res) => { setOutcome({ kind: 'moved', host: res.to }); onSettled?.() },
        onError: (err) => {
          const d = errorDetail(err)
          const reason = moveReasonText(d.code, d.message)
          if (d.outcome === 'moved') { setOutcome({ kind: 'moved', host: target }); onSettled?.() }
          else if (d.outcome === 'restored') {
            setOutcome({ kind: 'restored', host: source ?? 'its host', reason }); onSettled?.()
          } else if (d.outcome === 'failed') setOutcome({ kind: 'failed' })
          // Refused before anything was touched: back to the form, with the reason.
          else setRefusal(reason)
        },
      },
    )
  }

  const pending = move.isPending

  return (
    <Dialog open onClose={onClose} title="Move to host" size="md" dismissable={!pending}>
      <div className="space-y-4 min-w-0" data-testid="move-session-dialog">
        {outcome ? (
          <OutcomeView outcome={outcome} onClose={onClose} />
        ) : pending ? (
          <p role="status" className="flex items-center gap-2 min-w-0 text-sm text-ink">
            <Loader2 size={16} aria-hidden="true" className="flex-shrink-0 animate-spin" />
            <Clip text={`${movingLabel(target)} This can take up to a minute.`} />
          </p>
        ) : (
          <>
            <p className="text-sm text-ink-muted">
              The session closes here and resumes the same conversation on the new host. Its worktree comes with it.
            </p>
            {source && (
              <p className="flex min-w-0 gap-1 text-sm text-ink-muted">
                <span className="flex-shrink-0">Now on</span>
                <Clip text={source} className="font-medium text-ink" />
              </p>
            )}

            <div className="flex flex-col gap-1 min-w-0">
              <label htmlFor={selectId} className="text-xs text-ink-muted">Target host</label>
              {/* The Runner select's pattern (`list/DispatchBar.tsx`): a native select. */}
              <select
                id={selectId}
                aria-label="Target host"
                value={target}
                onChange={(e) => choose(e.target.value)}
                className={cn(
                  'w-full min-w-0 rounded border border-edge-strong text-sm',
                  'bg-surface text-ink px-2 py-1 min-h-target',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
                )}
              >
                <option value="">Choose a host…</option>
                {hosts.map((h) => {
                  const v = hostVerdict.get(h.name)
                  const refused = v !== undefined && !v.ok
                  return (
                    <option key={h.name} value={h.name} disabled={refused && h.name !== target}>
                      {refused ? `${h.name} — ${moveReasonText(v.reason, v.message)}` : h.name}
                    </option>
                  )
                })}
              </select>
            </div>

            {target !== '' && preflight.isFetching && !pf && (
              <p role="status" className="flex items-center gap-2 min-w-0 text-sm text-ink-muted">
                <Loader2 size={14} aria-hidden="true" className="flex-shrink-0 animate-spin" />
                <Clip text={`Checking ${target}…`} />
              </p>
            )}

            {(refusal ?? blocked) && (
              <p role="alert" data-testid="move-refusal" className="text-sm text-danger break-words">
                {refusal ?? blocked}
              </p>
            )}

            {pf && dirty && (
              <div className="space-y-2 min-w-0">
                <p className="text-sm text-ink">
                  {dirtyCount === 1 ? '1 uncommitted change:' : `${dirtyCount} uncommitted changes:`}
                </p>
                <ul data-testid="move-dirty-paths" className="rounded border border-edge bg-surface-raised px-2 py-1 font-mono text-xs text-ink min-w-0">
                  {dirtyPaths.slice(0, DIRTY_LIST_CAP).map((p) => (
                    <li key={p} className="min-w-0"><Clip text={p} /></li>
                  ))}
                  {dirtyCount > Math.min(dirtyPaths.length, DIRTY_LIST_CAP) && (
                    <li className="text-ink-muted">+{dirtyCount - Math.min(dirtyPaths.length, DIRTY_LIST_CAP)} more</li>
                  )}
                </ul>
                {needsConsent && (
                  <Consent label={CARRY_DIRTY_COPY} checked={carryDirty} onChange={setCarryDirty} />
                )}
              </div>
            )}

            {needsConfirm && (
              <div className="space-y-2 min-w-0">
                <p className="text-sm text-ink">These background tasks stop here and restart on the new host:</p>
                <ul data-testid="move-background-tasks" className="space-y-1 text-xs text-ink min-w-0">
                  {tasks.map((t, i) => (
                    <li key={`${t.kind}:${t.name}:${i}`} className="min-w-0">
                      <Clip text={t.description ? `${t.name} — ${t.description}` : t.name} />
                    </li>
                  ))}
                </ul>
                <Consent label="Stop these tasks and move" checked={stopBackground} onChange={setStopBackground} />
              </div>
            )}

            <div className="flex gap-3 justify-end">
              <Button type="button" variant="secondary" onClick={onClose}>Cancel</Button>
              <Button type="button" variant="primary" size="lg" disabled={!canMove} onClick={submit}>
                Move
              </Button>
            </div>
          </>
        )}
      </div>
    </Dialog>
  )
}

function OutcomeView({ outcome, onClose }: { outcome: Outcome; onClose: () => void }) {
  return (
    <>
      {outcome.kind === 'moved' && (
        <p role="status" data-testid="move-outcome" className="text-sm font-medium text-ink">
          <Clip text={`Moved to ${outcome.host}`} />
        </p>
      )}
      {outcome.kind === 'restored' && (
        <div role="alert" data-testid="move-outcome" className="space-y-1 min-w-0">
          <p className="text-sm font-medium text-danger">
            <Clip text={`Move failed — the session is back on ${outcome.host}`} />
          </p>
          <p className="text-sm text-ink-muted break-words">{outcome.reason}</p>
        </div>
      )}
      {outcome.kind === 'failed' && (
        <p role="alert" data-testid="move-outcome" className="text-sm font-medium text-danger">
          Move failed — finishing cleanup; this may take a minute
        </p>
      )}
      <div className="flex justify-end">
        <Button type="button" variant="primary" size="lg" onClick={onClose}>Done</Button>
      </div>
    </>
  )
}
