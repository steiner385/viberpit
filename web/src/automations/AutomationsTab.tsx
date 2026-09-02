import { useState, useEffect, useRef } from 'react'
import { toast } from 'sonner'
import { cn } from '@/lib/utils'
import {
  useAutomations, useAutomationEvents, useAutomationSessions,
  useAutomationActionable, useAutomationTrigger,
} from '@/api/hooks'
import { fetchPrDoctorJob, type Automation, type ActionSpec } from '@/api/automations'
import { ConsolidatePanel } from '@/components/ConsolidatePanel'
import { BacklogGroupsPanel } from '@/components/BacklogGroupsPanel'
import { ManualDispatchDialog } from '@/components/ManualDispatchDialog'

const LEVEL_DOT: Record<string, string> = {
  idle: 'bg-gray-400', active: 'bg-blue-500', warn: 'bg-amber-500', error: 'bg-red-500',
}

// Mirrors backend INACTIVE_SESSION_STATUSES (server/db/models.py).
const INACTIVE_SESSION_STATUSES = new Set(['ended', 'archived'])

// --- Spawned sessions section ------------------------------------------------

interface SpawnedSessionsSectionProps {
  id: string
  enabled: boolean
  onOpenSession?: (id: string) => void
  onReviewSession?: (id: string) => void
}

function SpawnedSessionsSection({ id, enabled, onOpenSession, onReviewSession }: SpawnedSessionsSectionProps) {
  const sessions = useAutomationSessions(id, enabled)
  const list = sessions.data ?? []
  const activeCount = list.filter((s) => !INACTIVE_SESSION_STATUSES.has(s.status)).length

  return (
    <section>
      <h4 className="text-xs font-semibold uppercase text-ink-muted mb-2">
        Spawned Sessions{list.length > 0 && ` (${activeCount} active)`}
      </h4>
      {list.length === 0
        ? <p className="text-sm text-ink-muted">No sessions spawned.</p>
        : (
          <div className="max-h-40 overflow-y-auto rounded border border-edge p-2">
            <ul className="space-y-1 text-sm">
              {list.map((s) => (
                <li key={s.id} className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={() => (INACTIVE_SESSION_STATUSES.has(s.status) ? onReviewSession : onOpenSession)?.(s.id)}
                    className="text-blue-600 hover:underline text-left"
                  >
                    {s.name}
                  </button>
                  <span className="text-ink-muted">· {s.status}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
    </section>
  )
}

// --- Action buttons ----------------------------------------------------------

function outcomeToast(r: { level: string; message: string }) {
  if (r.level === 'ok') toast.success(r.message)
  else if (r.level === 'warn') toast.warning(r.message)
  else toast.error(r.message)
}

// Async job phases → button label (issue #668). Shown while the background
// clone→assess→spawn pipeline runs so the user sees which phase is slow.
const PHASE_LABEL: Record<string, string> = {
  queued: 'Queued…', cloning: 'Cloning…', assessing: 'Assessing…', spawning: 'Spawning…',
}
const JOB_TERMINAL = new Set(['ready', 'error', 'deferred'])
const JOB_POLL_MS = 750

type Outcome = { level: string; message: string }

// Poll an async trigger job (issue #668) until it settles, reporting each phase
// to `onPhase`. Returns the final outcome, or a feed fallback if the job
// vanished (evicted / daemon restart → 404). `alive` lets the caller abandon the
// loop when the button unmounts.
async function pollJob(
  jobId: string, onPhase: (phase: string) => void, alive: () => boolean,
): Promise<Outcome> {
  while (alive()) {
    let job
    try {
      job = await fetchPrDoctorJob(jobId)
    } catch {
      return { level: 'warn', message: 'Lost track of the job — check the activity feed.' }
    }
    onPhase(job.phase)
    if (JOB_TERMINAL.has(job.status)) return { level: job.level, message: job.message }
    await new Promise((res) => setTimeout(res, JOB_POLL_MS))
  }
  return { level: 'warn', message: 'Cancelled' }
}

function ActionButton(
  { item, action, automationId }:
  { item: { id: string }; action: ActionSpec; automationId: string },
) {
  const trigger = useAutomationTrigger(automationId)
  const [armed, setArmed] = useState(false)
  const [outcome, setOutcome] = useState<Outcome | null>(null)
  // Non-null phase while an async trigger (issue #668) is in flight.
  const [phase, setPhase] = useState<string | null>(null)
  // Flipped false on unmount so an in-flight poll loop abandons quietly.
  const alive = useRef(true)
  useEffect(() => () => { alive.current = false }, [])
  const busy = trigger.isPending || phase !== null

  useEffect(() => {
    if (!outcome) return
    const id = setTimeout(() => setOutcome(null), 4000)
    return () => clearTimeout(id)
  }, [outcome])

  // Auto-disarm a risky confirm so a user who reconsiders isn't left with a
  // sticky "Confirm?" — mirrors the outcome-badge auto-clear (#466).
  useEffect(() => {
    if (!armed) return
    const id = setTimeout(() => setArmed(false), 4000)
    return () => clearTimeout(id)
  }, [armed])

  async function fire() {
    setArmed(false)
    const r = await trigger.mutateAsync({ item_id: item.id, kind: action.kind })
    // Async path (issue #668): poll the job for live phases, then settle. The
    // trigger RPC now returns in milliseconds instead of blocking 12–41 s.
    if (r.job_id && r.status === 'queued') {
      setPhase(r.phase ?? 'queued')
      const final = await pollJob(r.job_id, setPhase, () => alive.current)
      if (!alive.current) return
      setPhase(null)
      setOutcome(final)
      outcomeToast(final)
      return
    }
    setOutcome({ level: r.level, message: r.message })
    outcomeToast(r)
  }

  function onClick() {
    if (action.style === 'risky' && !armed) { setArmed(true); return }
    void fire()
  }

  // While a job runs, show its live phase; otherwise the generic pending label.
  const workingLabel = phase ? (PHASE_LABEL[phase] ?? 'Working…') : 'Working…'
  const base = 'rounded border px-2 py-1 text-xs font-semibold disabled:opacity-50'
  const btnTone = action.style === 'risky'
    ? 'border-red-300 text-red-700 hover:bg-red-600 hover:text-white hover:border-red-600'
    : 'border-edge hover:bg-accent hover:text-accent-ink hover:border-accent'
  return (
    <span className="inline-flex items-center gap-2">
      <button type="button" disabled={busy} onClick={onClick} className={`${base} ${btnTone}`}>
        {busy ? workingLabel : armed ? `Confirm ${action.label}?` : action.label}
      </button>
      {armed && !busy && (
        <button
          type="button"
          aria-label={`Cancel ${action.label}`}
          onClick={() => setArmed(false)}
          className={`${base} border-edge text-ink-muted hover:bg-surface-raised`}
        >
          Cancel
        </button>
      )}
      {outcome && (() => {
        const badgeTone = outcome.level === 'ok' ? 'text-green-700 bg-green-50'
          : outcome.level === 'warn' ? 'text-amber-700 bg-amber-50'
          : 'text-red-700 bg-red-50'
        return <span className={`rounded px-2 py-1 text-xs font-semibold ${badgeTone}`}>{outcome.message}</span>
      })()}
    </span>
  )
}

function AutomationActions({ automationId, enabled }: { automationId: string; enabled: boolean }) {
  const actionable = useAutomationActionable(automationId, enabled)
  const list = actionable.data ?? []
  return (
    <section>
      <h4 className="text-xs font-semibold uppercase text-ink-muted mb-2">Actionable items</h4>
      {list.length === 0
        ? <p className="text-sm text-ink-muted">No actionable items.</p>
        : (
          <div className="max-h-48 overflow-y-auto rounded border border-edge p-2">
            <ul className="space-y-1.5 text-sm">
              {list.map((item) => (
                <li key={item.id} className="flex items-center gap-2">
                  <span className="flex-1 truncate">
                    <span className="text-ink-muted tabular-nums">{item.id.split('#')[1] ? `#${item.id.split('#')[1]}` : ''}</span>{' '}
                    {item.title}
                  </span>
                  {item.actions.map((action) => (
                    <ActionButton key={action.kind} item={item} action={action} automationId={automationId} />
                  ))}
                </li>
              ))}
            </ul>
          </div>
        )}
    </section>
  )
}

// --- Per-automation live card ------------------------------------------------

function AutomationLiveCard({ automation: a, onOpenSession, onReviewSession }: {
  automation: Automation
  onOpenSession?: (id: string) => void
  onReviewSession?: (id: string) => void
}) {
  const [open, setOpen] = useState(false)
  const [customDispatch, setCustomDispatch] = useState(false)
  const events = useAutomationEvents(a.id, open)

  return (
    <div className="rounded-lg border border-edge bg-surface">
      <button
        type="button"
        aria-label={open ? 'Collapse' : 'Expand'}
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="text-left w-full p-4"
      >
        <div className="flex items-center gap-2">
          <span className="font-medium">{a.name}</span>
          {a.activeSessions > 0 && (
            <span className="text-xs tabular-nums rounded-full bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300 px-2 py-0.5 leading-none">
              {a.activeSessions} active
            </span>
          )}
        </div>
        {a.description && (
          <p className="text-xs text-ink-muted mt-0.5 leading-snug">{a.description}</p>
        )}
        <div className="flex items-center gap-2 text-sm text-ink-muted mt-0.5">
          <span className={cn('inline-block w-2 h-2 rounded-full shrink-0', LEVEL_DOT[a.status.level])} />
          {a.status.text}
        </div>
      </button>
      {open && (
        <div className="border-t border-edge p-4 space-y-4">
          <section>
            <h4 className="text-xs font-semibold uppercase text-ink-muted mb-2">Activity</h4>
            {(events.data ?? []).length === 0
              ? <p className="text-sm text-ink-muted">No activity yet.</p>
              : (
                <div className="max-h-40 overflow-y-auto rounded border border-edge p-2">
                  <ul className="space-y-1 text-sm">
                    {(events.data ?? []).map((e) => (
                      <li key={`${e.ts}-${e.sessionId ?? ''}`} className="flex gap-2">
                        <span className="text-ink-muted tabular-nums shrink-0">{e.ts.slice(11, 19)}</span>
                        {e.sessionId
                          ? <button
                              type="button"
                              onClick={() => onOpenSession?.(e.sessionId!)}
                              className="text-blue-600 hover:underline text-left"
                            >{e.summary}</button>
                          : <span>{e.summary}</span>}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
          </section>
          <AutomationActions automationId={a.id} enabled={open} />
          {a.id === 'pr-doctor' && <ConsolidatePanel enabled={open} onOpenSession={onOpenSession} />}
          {a.id === 'backlog-runner' && (
            <>
              <div className="flex justify-end">
                <button
                  type="button"
                  onClick={() => setCustomDispatch(true)}
                  className="rounded border border-edge px-2 py-0.5 text-xs hover:bg-surface-raised"
                >
                  Custom dispatch…
                </button>
              </div>
              <BacklogGroupsPanel enabled={open} onOpenSession={onOpenSession} />
              <ManualDispatchDialog
                open={customDispatch}
                onClose={() => setCustomDispatch(false)}
                onOpenSession={onOpenSession}
              />
            </>
          )}
          <SpawnedSessionsSection id={a.id} enabled={open} onOpenSession={onOpenSession} onReviewSession={onReviewSession} />
        </div>
      )}
    </div>
  )
}

// --- Tab root ----------------------------------------------------------------

export function AutomationsTab({ onOpenSession, onReviewSession }: {
  onOpenSession?: (id: string) => void
  onReviewSession?: (id: string) => void
} = {}) {
  const { data, isLoading } = useAutomations()
  if (isLoading) return <p className="text-sm text-ink-muted p-4">Loading…</p>
  const list = data ?? []
  return (
    <div className="p-4 space-y-3">
      {list.length === 0 && (
        <p className="text-sm text-ink-muted">No automations configured.</p>
      )}
      {list.map((a) => (
        <AutomationLiveCard
          key={a.id}
          automation={a}
          onOpenSession={onOpenSession}
          onReviewSession={onReviewSession}
        />
      ))}
    </div>
  )
}
