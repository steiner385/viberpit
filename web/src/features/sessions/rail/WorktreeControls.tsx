/**
 * The Environment card's worktree actions (D-19): Discard (end the session and remove
 * its worktree) and Repair (recreate a missing one). Both read the preflight, so they
 * show only where the server would act: a managed worktree, asked for by a person —
 * a token caller's 403, or any failed preflight, shows nothing at all.
 *
 * - Discard: a present managed worktree. Opens the lazy confirm (`DiscardWorktreeDialog`),
 *   which re-runs the preflight and says why a busy session can't be discarded yet.
 * - Repair: only when the worktree is missing or empty and the server can recreate it.
 */
import { lazy, Suspense, useState } from 'react'
import { toast } from 'sonner'
import { useRepairWorktree, useWorktreePreflight } from '@/api/worktreeHooks'
import { REPAIR_FAILED_FALLBACK, worktreeError } from './worktreeCopy'

const DiscardWorktreeDialog = lazy(() => import('./DiscardWorktreeDialog'))

export interface WorktreeControlsProps {
  sessionId: string
  busy: boolean
  /** The session was archived by a discard: the host leaves it. */
  onDiscarded?: () => void
}

const ACTION = 'rounded px-1.5 py-0.5 text-xs font-medium hover:bg-edge disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent'

export function WorktreeControls({ sessionId, busy, onDiscarded }: WorktreeControlsProps) {
  const { data: pf } = useWorktreePreflight(sessionId, busy)
  const repair = useRepairWorktree()
  const [discardOpen, setDiscardOpen] = useState(false)
  if (!pf?.managed) return null

  const canOpenDiscard = pf.state === 'present'
  const repairable = (pf.state === 'missing' || pf.state === 'empty') && pf.canRepair
  if (!canOpenDiscard && !repairable && !discardOpen) return null

  function runRepair() {
    repair.mutate(sessionId, {
      onSuccess: (res) => toast.success(res.branch ? `Worktree recreated on ${res.branch}` : 'Worktree recreated'),
      onError: (err) => toast.error(worktreeError(err, REPAIR_FAILED_FALLBACK).text),
    })
  }

  return (
    <div className="flex flex-wrap items-center justify-end gap-1" data-testid="worktree-controls">
      {repairable && (
        <>
          <span className="mr-auto text-warning">The worktree is missing from this host.</span>
          <button type="button" className={`${ACTION} text-ink`} disabled={repair.isPending} onClick={runRepair}>
            {repair.isPending ? 'Repairing…' : 'Repair'}
          </button>
        </>
      )}
      {canOpenDiscard && (
        <button
          type="button"
          className={`${ACTION} text-danger`}
          onClick={() => setDiscardOpen(true)}
        >
          Discard…
        </button>
      )}
      {discardOpen && (
        <Suspense fallback={null}>
          <DiscardWorktreeDialog
            sessionId={sessionId}
            busy={busy}
            onClose={() => setDiscardOpen(false)}
            onDiscarded={onDiscarded}
          />
        </Suspense>
      )}
    </div>
  )
}
