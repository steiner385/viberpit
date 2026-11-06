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
import { TOUCH_FLOOR } from '@/ui/list/listControlStyles'
import { BRANCH_KEPT_NOTE, BRANCH_KEPT_UNKNOWN, REPAIR_FAILED_FALLBACK, worktreeError } from './worktreeCopy'

const DiscardWorktreeDialog = lazy(() => import('./DiscardWorktreeDialog'))

export interface WorktreeControlsProps {
  sessionId: string
  busy: boolean
  /** The session was archived by a discard: the host leaves it. */
  onDiscarded?: () => void
}

/** The house 44px floor (`TOUCH_FLOOR`), with the label padded out on a coarse pointer. */
const ACTION = `rounded px-1.5 py-0.5 text-xs font-medium hover:bg-edge disabled:opacity-50 ${TOUCH_FLOOR} pointer-coarse:px-3 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent`

export function WorktreeControls({ sessionId, busy, onDiscarded }: WorktreeControlsProps) {
  const { data: pf } = useWorktreePreflight(sessionId, { enabled: !busy })
  const repair = useRepairWorktree()
  const [discardOpen, setDiscardOpen] = useState(false)
  // An open dialog outlives the card's own reading: a refetch or a gap in it must
  // never unmount the confirm (or a discard in flight) under the user.
  const managed = pf?.managed === true
  const canOpenDiscard = managed && pf.state === 'present'
  const repairable = managed && (pf.state === 'missing' || pf.state === 'empty') && pf.canRepair
  // D-22 choice 3A, read-only: a branch that looks like ours but nothing vouches for is
  // kept on cleanup — say so quietly. Never for the user's own unprefixed branch.
  const keptNote = managed && pf.branchPrefixed === true
    && (pf.branchOwnership === 'unowned' || pf.branchOwnership === 'unknown')
  if (!canOpenDiscard && !repairable && !discardOpen && !keptNote) return null

  // `mutateAsync`, not `mutate`'s callbacks: the result toasts even if the card has gone.
  function runRepair() {
    repair.mutateAsync(sessionId).then(
      (res) => { toast.success(res.branch ? `Worktree recreated on ${res.branch}` : 'Worktree recreated') },
      (err: unknown) => { toast.error(worktreeError(err, REPAIR_FAILED_FALLBACK).text) },
    )
  }

  return (
    <div className="flex flex-wrap items-center justify-end gap-1" data-testid="worktree-controls">
      {keptNote && (
        <p className="mr-auto w-full text-xs text-ink-muted" data-testid="branch-kept-note">
          {BRANCH_KEPT_NOTE}
          {pf?.branchOwnership === 'unknown' && <> {BRANCH_KEPT_UNKNOWN}</>}
        </p>
      )}
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
            onClose={() => setDiscardOpen(false)}
            onDiscarded={onDiscarded}
          />
        </Suspense>
      )}
    </div>
  )
}
