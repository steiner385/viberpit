/**
 * Per-item action results for the actionable list (#3429).
 *
 * A click's result belongs to the item it was clicked on, never to whatever the
 * automation did last. The state is keyed by item id and lives with the list,
 * not inside the button: when a refetch turns the clicked row into a status
 * row ("Being worked by session …") the button unmounts, but the job is still
 * polled and its outcome still lands on its own row and in a toast that names
 * the item.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { useAutomationTrigger } from '@/api/hooks'
import { fetchPrDoctorJob, type ActionableItem, type ActionSpec } from '@/api/automations'
import { outcomeToast } from './outcomeToast'

export type Outcome = { level: string; message: string }

export interface ItemJob {
  busy: boolean
  /** Non-null while an async trigger (issue #668) is in flight. */
  phase: string | null
  /** The settled result, shown inline on this item's row until it expires. */
  outcome: Outcome | null
}

const IDLE: ItemJob = { busy: false, phase: null, outcome: null }
const JOB_TERMINAL = new Set(['ready', 'error', 'deferred'])
const JOB_POLL_MS = 750
/** How long a settled outcome stays on its row. */
export const OUTCOME_TTL_MS = 4000

const LOST: Outcome = { level: 'warn', message: 'Lost track of the job — check the activity feed.' }

/** What a toast calls the item: its `owner/repo#N` ref when it is a PR, else its title. */
export function itemLabel(item: Pick<ActionableItem, 'id' | 'title'>): string {
  return /^[\w.-]+\/[\w.-]+#\d+$/.test(item.id) ? item.id : item.title
}

// Poll an async trigger job (issue #668) until it settles, reporting each phase
// to `onPhase`. Only an answer about this job id AND this item is taken: any
// other answer, or a vanished job (expired / daemon restart → 404), is "lost".
async function pollJob(
  jobId: string, itemId: string, onPhase: (phase: string) => void, alive: () => boolean,
): Promise<Outcome | null> {
  while (alive()) {
    let job
    try {
      job = await fetchPrDoctorJob(jobId)
    } catch {
      return LOST
    }
    if (job.job_id !== jobId || job.target !== itemId) return LOST
    if (JOB_TERMINAL.has(job.status)) return { level: job.level, message: job.message }
    onPhase(job.phase)
    await new Promise((res) => setTimeout(res, JOB_POLL_MS))
  }
  return null
}

export function useActionJobs(automationId: string) {
  const trigger = useAutomationTrigger(automationId)
  const [jobs, setJobs] = useState<Record<string, ItemJob>>({})
  const alive = useRef(true)
  const busy = useRef(new Set<string>())
  const expiry = useRef(new Map<string, ReturnType<typeof setTimeout>>())

  useEffect(() => {
    alive.current = true
    const timers = expiry.current
    return () => {
      alive.current = false
      timers.forEach(clearTimeout)
      timers.clear()
    }
  }, [])

  const patch = useCallback((id: string, p: Partial<ItemJob>) => {
    if (!alive.current) return
    setJobs((prev) => ({ ...prev, [id]: { ...(prev[id] ?? IDLE), ...p } }))
  }, [])

  const settle = useCallback((item: ActionableItem, outcome: Outcome) => {
    busy.current.delete(item.id)
    if (!alive.current) return
    patch(item.id, { busy: false, phase: null, outcome })
    outcomeToast({ level: outcome.level, message: `${itemLabel(item)}: ${outcome.message}` })
    clearTimeout(expiry.current.get(item.id))
    expiry.current.set(item.id, setTimeout(() => {
      expiry.current.delete(item.id)
      patch(item.id, { outcome: null })
    }, OUTCOME_TTL_MS))
  }, [patch])

  const { mutateAsync } = trigger
  const run = useCallback(async (item: ActionableItem, action: ActionSpec) => {
    if (busy.current.has(item.id)) return
    busy.current.add(item.id)
    clearTimeout(expiry.current.get(item.id))
    patch(item.id, { busy: true, phase: null, outcome: null })
    let r
    try {
      r = await mutateAsync({ item_id: item.id, kind: action.kind })
    } catch {
      // The global mutation handler already toasts the request failure.
      busy.current.delete(item.id)
      patch(item.id, { busy: false })
      return
    }
    // Async path (issue #668): poll this item's own job, then settle.
    if (r.job_id && r.status === 'queued') {
      patch(item.id, { phase: r.phase ?? 'queued' })
      const final = await pollJob(
        r.job_id, item.id, (phase) => patch(item.id, { phase }), () => alive.current)
      if (final) settle(item, final)
      else busy.current.delete(item.id)
      return
    }
    settle(item, { level: r.level, message: r.message })
  }, [mutateAsync, patch, settle])

  return { jobs, run }
}
