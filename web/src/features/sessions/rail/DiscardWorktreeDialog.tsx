/**
 * Discard (D-19): end this session and remove its worktree, after a fresh preflight.
 *
 * Lazy (`WorktreeControls` imports it on open). The confirm states exactly what the
 * discard does before it is allowed: the uncommitted and unpushed counts (both kept
 * as recovery refs by the server first), whether the branch is deleted or kept, how
 * to restore, and — binding — that ignored files are NOT kept. The Discard button
 * needs an explicit tick and an answered, current preflight that allows it.
 *
 * On success the session is archived, so the dialog closes, the host navigates
 * away (`onDiscarded`) — only if the page still shows this session: a response that
 * lands after the user opened something else never pulls them off it — and a toast
 * that outlives the page lists the recovery refs
 * and the restore command, which copies on click (`CopyablePath`). A failure after
 * the refs were written lists them in place, the same way. Paths, branches and refs
 * are untrusted text, never markup.
 */
import { useEffect, useId, useRef, useState } from 'react'
import { matchPath } from 'react-router-dom'
import { Loader2 } from 'lucide-react'
import { toast } from 'sonner'
import { cn } from '@/lib/utils'
import type { WorktreeDiscardResult, WorktreePreflight } from '@/api/types'
import { useDiscardWorktree, useWorktreePreflight } from '@/api/worktreeHooks'
import {
  BACKGROUND_SESSION_ROUTE, BACKGROUND_SUBAGENT_ROUTE, SESSION_ROUTE, SUBAGENT_ROUTE,
} from '@/session/sessionNavState'
import { Button } from '@/ui/Button'
import { Dialog } from '@/ui/Dialog'
import { CopyablePath } from '../transcript/CopyablePath'
import {
  DISCARD_FAILED_FALLBACK, IGNORED_FILES_LOST, WORKTREE_ERROR_MESSAGES, countLine, worktreeError,
} from './worktreeCopy'

export interface DiscardWorktreeDialogProps {
  sessionId: string
  onClose: () => void
  /** The session was archived: the host leaves it. */
  onDiscarded?: () => void
}

/** Why the preflight refuses a discard, in the table's words. */
function refusal(pf: WorktreePreflight): string {
  const code = pf.busy ? 'WORKTREE_SESSION_BUSY' : !pf.managed ? 'WORKTREE_NOT_MANAGED'
    : !pf.present ? 'WORKTREE_MISSING' : null
  return (code && WORKTREE_ERROR_MESSAGES[code]) || "This worktree can't be discarded right now"
}

/** Does the page show *sessionId* (its transcript or one of its sub-agents, in either
 *  list scope) right now? Read from the live URL, not this component's render: the
 *  discard's response can land after the dialog — and the rail — unmounted, and the
 *  router keeps `window.location` current either way. */
function stillShowing(sessionId: string): boolean {
  const { pathname } = window.location
  return [SESSION_ROUTE, SUBAGENT_ROUTE, BACKGROUND_SESSION_ROUTE, BACKGROUND_SUBAGENT_ROUTE]
    .some((route) => matchPath(route, pathname)?.params.id === sessionId)
}

/** A ref or command as copyable monospace text that wraps rather than widening the dialog. */
function Code({ text }: { text: string }) {
  return <code className="block min-w-0 break-all rounded bg-surface-raised px-1.5 py-0.5 font-mono text-xs text-ink">{text}</code>
}

/** The restore command, copied by a click or Enter (the worktree path's own control). */
function Restore({ command }: { command: string }) {
  return (
    <span className="block min-w-0 break-all font-mono text-xs" data-testid="restore-command">
      <CopyablePath text={command} title="Copy restore command" />
      <span className="ml-1 font-sans text-ink-muted">(select to copy)</span>
    </span>
  )
}

function Refs({ refs, restore }: { refs: readonly string[]; restore: string | null }) {
  return (
    <div className="space-y-1 min-w-0" data-testid="discard-recovery">
      {refs.length > 0 && <p className="text-xs text-ink-muted">Saved as {refs.length === 1 ? 'a recovery ref' : 'recovery refs'}:</p>}
      {refs.map((r) => <Code key={r} text={r} />)}
      {restore && (
        <>
          <p className="text-xs text-ink-muted">Restore with:</p>
          <Restore command={restore} />
        </>
      )}
    </div>
  )
}

/** A failure that arrives after the dialog is gone (the card unmounted mid-request)
 *  still says why, with the refs — up until dismissed when there are any. */
function toastFailure(f: ReturnType<typeof worktreeError>) {
  const kept = f.refs.length > 0 || f.restore !== null
  toast.error(f.text, {
    description: kept ? <span className="block"><Refs refs={f.refs} restore={f.restore} /></span> : undefined,
    duration: kept ? Infinity : 8000,
  })
}

/** The page is about to leave the session, so the outcome rides a toast that stays
 *  until dismissed whenever there is something to restore. */
function toastDiscarded(res: WorktreeDiscardResult) {
  const branch = res.branch ? (res.branchDeleted ? ` and branch ${res.branch}` : '') : ''
  const kept = res.branch && !res.branchDeleted ? ` Branch ${res.branch} was kept.` : ''
  const restore = res.restoreCommand
  toast.success(`Discarded — worktree${branch} removed`, {
    description: (
      <span className="block space-y-1">
        {res.recoveryRefs.length > 0
          ? <>{`Saved: ${res.recoveryRefs.join(', ')}.${kept}`}{restore && <Restore command={restore} />}</>
          : `Nothing needed saving.${kept}`}
      </span>
    ),
    // Up until dismissed while there is something to restore: the toast is the only
    // copy of where the work went once the page has left the session.
    duration: restore ? Infinity : 8000,
  })
}

export default function DiscardWorktreeDialog({ sessionId, onClose, onDiscarded }: DiscardWorktreeDialogProps) {
  const ackId = useId()
  const preflight = useWorktreePreflight(sessionId, { fresh: true })
  const discard = useDiscardWorktree()
  const [failure, setFailure] = useState<ReturnType<typeof worktreeError> | null>(null)
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  const pf = preflight.data
  const checking = preflight.isFetching
  // The tick answers the facts it was given: it is held against that preflight answer
  // (structurally shared, so an identical refetch keeps it) and lapses when they change.
  const [ackFor, setAckFor] = useState<WorktreePreflight | null>(null)
  const ack = pf !== undefined && ackFor === pf && !checking
  const blocked = preflight.isError
    ? worktreeError(preflight.error, DISCARD_FAILED_FALLBACK).text
    : pf && !pf.canDiscard ? refusal(pf) : null
  const pending = discard.isPending
  const canDiscard = pf !== undefined && !checking && blocked === null && ack && !pending

  function submit() {
    if (!canDiscard) return
    setFailure(null)
    // `mutateAsync`, not `mutate`'s callbacks, which React Query drops on unmount: the
    // archive can drop the session from the list (and this dialog with the rail)
    // before the response lands, and the refs must still reach the user.
    discard.mutateAsync(sessionId).then(
      (res) => {
        if (mounted.current) onClose()
        if (stillShowing(sessionId)) onDiscarded?.()
        toastDiscarded(res)
      },
      (err: unknown) => {
        const f = worktreeError(err, DISCARD_FAILED_FALLBACK)
        if (mounted.current) setFailure(f)
        else toastFailure(f)
      },
    )
  }

  return (
    <Dialog open onClose={onClose} title="Discard this worktree?" size="md" dismissable={!pending}>
      <div className="space-y-3 min-w-0 text-sm" data-testid="discard-worktree-dialog">
        <p className="text-ink">
          This ends the session and deletes its worktree{pf?.path ? ':' : '.'}
        </p>
        {pf?.path && <Code text={pf.path} />}

        {checking && !pf && (
          <p role="status" className="flex items-center gap-2 text-ink-muted">
            <Loader2 size={14} aria-hidden="true" className="flex-shrink-0 animate-spin" />
            Checking the worktree…
          </p>
        )}

        {pf && (
          <ul className="space-y-1 text-ink" data-testid="discard-facts">
            <li>{countLine(pf.dirty, 'uncommitted file', 'uncommitted files')}</li>
            <li>{countLine(pf.unpushed, 'unpushed commit', 'unpushed commits')}</li>
            {pf.branch && (
              <li className="break-all">
                {pf.sessionBranch ? `Branch ${pf.branch} will be deleted.` : `Branch ${pf.branch} will be kept.`}
              </li>
            )}
          </ul>
        )}

        {pf?.restoreHint && (
          <div className="space-y-1 min-w-0">
            <p className="text-ink-muted">
              Uncommitted and unpushed work is saved as a recovery ref before anything is removed. To restore it later:
            </p>
            <Code text={pf.restoreHint} />
          </div>
        )}

        <p className="text-warning" data-testid="discard-ignored-warning">{IGNORED_FILES_LOST}</p>

        {(failure?.text ?? blocked) && (
          <p role="alert" data-testid="discard-refusal" className="text-danger break-words">
            {failure?.text ?? blocked}
          </p>
        )}
        {failure && (failure.refs.length > 0 || failure.restore) && <Refs refs={failure.refs} restore={failure.restore} />}

        <label htmlFor={ackId} className={cn('flex items-start gap-2 min-h-target text-ink cursor-pointer', (blocked || checking) && 'opacity-50')}>
          <input
            id={ackId}
            type="checkbox"
            checked={ack}
            // Not while a fresh answer is on its way: a tick must answer the facts shown.
            disabled={blocked !== null || pending || checking}
            onChange={(e) => setAckFor(e.target.checked ? pf ?? null : null)}
            className="mt-0.5 h-4 w-4 flex-shrink-0 rounded-sm border-edge-strong accent-accent focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          />
          <span className="min-w-0">End the session and delete its worktree</span>
        </label>

        <div className="flex gap-3 justify-end">
          <Button type="button" variant="secondary" onClick={onClose} disabled={pending}>Cancel</Button>
          <Button type="button" variant="danger" size="lg" disabled={!canDiscard} loading={pending} onClick={submit}>
            Discard
          </Button>
        </div>
      </div>
    </Dialog>
  )
}
