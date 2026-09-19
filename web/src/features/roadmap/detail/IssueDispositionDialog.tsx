import { useId, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { toast } from 'sonner'
import { ApiRequestError } from '@/api/client'
import { useWorkItemIssue, useDispositionIssue, useReleaseHold, useDispatchBacklogGroup } from '@/api/hooks'
import type { IssueDispositionInput, WorkItemIssue } from '@/api/pm'
import { extractMutationErrorMessage } from '@/api/queryClient'
import { Dialog } from '@/ui/Dialog'
import { Button } from '@/ui/Button'
import { Field } from '@/ui/Field'
import { Input } from '@/ui/Input'
import { Textarea } from '@/ui/Textarea'
import { Skeleton } from '@/ui/Skeleton'
import { Banner } from '@/ui/Banner'
import { Markdown } from '@/features/sessions/transcript/Markdown'
import { ConsequenceList } from '../guidance/ConsequenceList'
import { holdLabel, ROADMAP } from '@/features/products/vocab'
import { sessionPath } from '@/session/sessionNavState'
import { inboxPath } from '../roadmapNavState'

/** Lazy-mounted by the list: its queries exist only after an explicit review. */
export function IssueDispositionDialog({ itemId, onClose }: { itemId: string; onClose: () => void }) {
  const issue = useWorkItemIssue(itemId)
  const mutation = useDispositionIssue(itemId)
  const release = useReleaseHold(itemId)
  const dispatch = useDispatchBacklogGroup()
  const [mode, setMode] = useState<'read' | 'edit' | 'resolve' | 'dismiss'>('read')
  const [draft, setDraft] = useState<WorkItemIssue | null>(null)
  const [failure, setFailure] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)
  const [spawned, setSpawned] = useState<{ id: string; name: string | null } | null>(null)
  const [dispatchError, setDispatchError] = useState<string | null>(null)
  const inFlight = useRef(false)
  const id = useId()
  const data = draft ?? issue.data
  const pending = busy || mutation.isPending || release.isPending || dispatch.isPending
  const conflict = failure instanceof ApiRequestError && failure.status === 409
  const start = (next: typeof mode) => {
    if (!issue.data) return
    setDraft(issue.data)
    setFailure(null)
    // The outcome of the last Implement now belongs to the issue as it was; an
    // edit or a decision starts from a clean panel rather than re-showing a
    // stale refusal or a link the user has already followed.
    setSpawned(null)
    setDispatchError(null)
    setMode(next)
  }
  const submit = async () => {
    if (!data || mode === 'read' || inFlight.current || conflict) return
    const input: IssueDispositionInput = mode === 'edit'
      ? { action: mode, version: data.version, title: data.title.trim(), body: data.body }
      : { action: mode, version: data.version }
    inFlight.current = true
    setBusy(true)
    setFailure(null)
    try {
      const result = await mutation.mutateAsync(input)
      if (input.action === 'edit') {
        setDraft(result.item)
        setMode('read')
      } else onClose()
    } catch (e) {
      setFailure(e)
    } finally {
      inFlight.current = false
      setBusy(false)
    }
  }
  const reload = async () => {
    if (inFlight.current) return
    inFlight.current = true
    setBusy(true)
    try {
      const result = await issue.refetch()
      if (result.error) setFailure(result.error)
      else {
        setDraft(null)
        setMode('read')
        setFailure(null)
      }
    } finally {
      inFlight.current = false
      setBusy(false)
    }
  }
  // Retry: releases the hold. The dialog stays open — the issue query is
  // invalidated and refetched by useReleaseHold's onSuccess, and the panel
  // disappears on its own once the refetched issue has no holdKind.
  const retryHold = async () => {
    try {
      await release.mutateAsync()
    } catch {
      // surfaced inline via release.error below
    }
  }
  // Implement now: dispatches a coding session for the issue's own ref. A 200
  // response can still carry a refusal (`no_account` / `refused`) rather than
  // an HTTP error, so success/failure is read off `status`, not the promise.
  const implementNow = async () => {
    if (!data) return
    setDispatchError(null)
    setSpawned(null)
    try {
      const result = await dispatch.mutateAsync([data.ref])
      if (result.status === 'spawned' && result.session_id) {
        setSpawned({ id: result.session_id, name: result.session_name })
        toast.success(result.session_name ? `Started ${result.session_name}` : 'Started a session')
      } else {
        setDispatchError(`Dispatch failed: ${result.status}`)
      }
    } catch (e) {
      setDispatchError(extractMutationErrorMessage(e, 'Could not dispatch this issue.'))
    }
  }
  return (
    <Dialog open title="Review and decide" size="xl" onClose={onClose} dismissable={!pending && mode === 'read' && !failure} bodyClassName="min-w-0 space-y-4">
      {!data && issue.isLoading && <Skeleton lines={5} />}
      {!data && issue.isError && <div role="alert" className="text-danger">
        {extractMutationErrorMessage(issue.error, 'Could not load this issue.')}
        <Button variant="secondary" disabled={pending} onClick={() => void reload()}>Retry</Button>
      </div>}
      {data && <>
        <p className="text-sm text-ink-muted">{data.repo} #{data.number} · {data.state}</p>
        {mode === 'edit' ? <form id={`${id}-edit`} className="space-y-3" onSubmit={(e) => { e.preventDefault(); if (data.title.trim()) void submit() }}>
          <Field label="Title" htmlFor={`${id}-title`} required>
            <Input id={`${id}-title`} value={data.title} disabled={pending} required onChange={(e) => setDraft({ ...data, title: e.target.value })} />
          </Field>
          <Field label="Description" htmlFor={`${id}-body`} help="Describe the problem, requirements, or decision. Saves to the tracking issue on GitHub.">
            <Textarea id={`${id}-body`} value={data.body} disabled={pending} rows={10} onChange={(e) => setDraft({ ...data, body: e.target.value })} />
          </Field>
        </form> : <>
          <h3 className="break-words text-lg font-medium">{data.title}</h3>
          <div className="min-w-0 overflow-x-auto break-words text-sm"><Markdown>{data.body || 'No description provided.'}</Markdown></div>
        </>}
        {mode === 'read' && data.holdKind && <div role="region" aria-label="Hold" className="rounded-md border border-edge p-3 space-y-3">
          <p className="font-medium">{holdLabel(data.holdKind)}</p>
          {data.holdReason && <p className="text-sm text-ink">{data.holdReason}</p>}
          {(data.holdBy || data.holdSetAt) && <p className="text-sm text-ink-muted">
            {data.holdBy && <>by {data.holdBy}</>}
            {data.holdBy && data.holdSetAt && ' · '}
            {data.holdSetAt && new Date(data.holdSetAt).toLocaleString()}
          </p>}
          {data.holdKind === 'owner_action' && <>
            <div className="flex flex-wrap gap-2">
              <Button size="lg" loading={release.isPending} disabled={pending} onClick={() => void retryHold()}>Retry</Button>
              <Button
                size="lg"
                variant="secondary"
                loading={dispatch.isPending}
                disabled={pending || (data.guidance && 'implement' in data.guidance
                  ? !data.guidance.implement.available : false)}
                onClick={() => void implementNow()}
              >
                Implement now
              </Button>
            </div>
            {release.isError && <p role="alert" className="text-sm text-danger">
              {extractMutationErrorMessage(release.error, 'Could not release this hold.')}
            </p>}
            {data.guidance && 'retry' in data.guidance && <div className="grid gap-3 sm:grid-cols-2">
              <ConsequenceList heading="Retry" consequence={data.guidance.retry} />
              <ConsequenceList heading="Implement now" consequence={data.guidance.implement} />
            </div>}
          </>}
          {/* Evidence: the Backlog Runner dispatches and settles this on its own
              (spec 2026-09-18 §5) — a plain consequence line through the same
              renderer as owner_action above, never a second one, and no buttons:
              there is nothing here for a person to tap. */}
          {data.holdKind === 'evidence' && data.guidance && 'evidence' in data.guidance &&
            <ConsequenceList consequence={data.guidance.evidence} />}
          {/* Decision: the options, the recommendation and the two consequence
              columns all live on the proposal card, so this panel LINKS there
              (spec 2026-09-18 §7) rather than growing a second copy of the
              choice. No Retry either — accepting the proposal is the only thing
              that clears a decision hold, which is why `hold_guidance` computes
              no buttons for this kind. */}
          {data.holdKind === 'decision' && <>
            <p className="text-sm text-ink-muted">
              A session proposed options instead of guessing. Pick one in the {ROADMAP.inbox.toLowerCase()} and the next run implements it.
            </p>
            <Link to={inboxPath({})} className="inline-block text-sm text-accent hover:underline">
              Open the {ROADMAP.inbox.toLowerCase()}
            </Link>
          </>}
        </div>}
        {/* Outside the hold panel on purpose: a dispatch RELEASES the hold, so the
            panel disappears on the refetch that follows — and the session it just
            started, or the reason it could not, has to outlive it. */}
        {dispatchError && <Banner tone="danger" role="alert" title={dispatchError} />}
        {spawned && <Link to={sessionPath(spawned.id)} className="inline-block text-sm text-accent hover:underline">Open session</Link>}
        {(mode === 'resolve' || mode === 'dismiss') && <div className="rounded-md border border-edge p-3 space-y-2">
          <p className="font-medium">{mode === 'resolve' ? 'Mark this issue as resolved?' : 'Close this issue as won’t do?'}</p>
          <p className="text-sm text-ink-muted">This closes the tracking issue on GitHub and removes it from open Roadmap work. It does not repair an account or change code. {mode === 'resolve' ? 'Confirm only if the underlying work is complete.' : 'Confirm if this work is no longer needed or will not be pursued.'}</p>
        </div>}
        {failure != null && <div role="alert" className="space-y-2 text-sm text-danger">
          <p>{extractMutationErrorMessage(failure, 'Could not save your decision. Your changes are still here.')}</p>
          {conflict && <><p>The issue changed. Reloading replaces your draft with the latest issue.</p><Button variant="secondary" disabled={pending} onClick={() => void reload()}>Reload issue</Button></>}
        </div>}
        <div className="flex flex-wrap gap-2">
          {mode === 'read' && data.state.toLowerCase() === 'open' && <>
            <Button onClick={() => start('edit')}>Edit details</Button>
            <Button variant="secondary" onClick={() => start('resolve')}>Mark resolved</Button>
            <Button variant="secondary" onClick={() => start('dismiss')}>Won’t do</Button>
          </>}
          {mode === 'edit' && <>
            <Button type="submit" form={`${id}-edit`} size="lg" loading={pending} disabled={!data.title.trim() || conflict}>Save changes</Button>
            <Button variant="ghost" disabled={pending} onClick={() => { setMode('read'); setDraft(null); setFailure(null) }}>Cancel edit</Button>
          </>}
          {(mode === 'resolve' || mode === 'dismiss') && <>
            <Button size="lg" loading={pending} disabled={conflict} onClick={() => void submit()}>{mode === 'resolve' ? 'Confirm resolved' : 'Confirm won’t do'}</Button>
            <Button variant="ghost" disabled={pending} onClick={() => { setMode('read'); setDraft(null); setFailure(null) }}>Go back</Button>
          </>}
        </div>
        {data.url && <a href={data.url} target="_blank" rel="noreferrer" className="inline-block text-sm text-accent hover:underline">View on GitHub</a>}
      </>}
      <div className="flex justify-end"><Button variant="ghost" disabled={pending} onClick={onClose}>Close</Button></div>
    </Dialog>
  )
}
