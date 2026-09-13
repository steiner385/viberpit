import { useId, useRef, useState } from 'react'
import { ApiRequestError } from '@/api/client'
import { useWorkItemIssue, useDispositionIssue } from '@/api/hooks'
import type { IssueDispositionInput, WorkItemIssue } from '@/api/pm'
import { extractMutationErrorMessage } from '@/api/queryClient'
import { Dialog } from '@/ui/Dialog'
import { Button } from '@/ui/Button'
import { Field } from '@/ui/Field'
import { Input } from '@/ui/Input'
import { Textarea } from '@/ui/Textarea'
import { Skeleton } from '@/ui/Skeleton'
import { Markdown } from '@/features/sessions/transcript/Markdown'

/** Lazy-mounted by the list: its queries exist only after an explicit review. */
export function IssueDispositionDialog({ itemId, onClose }: { itemId: string; onClose: () => void }) {
  const issue = useWorkItemIssue(itemId)
  const mutation = useDispositionIssue(itemId)
  const [mode, setMode] = useState<'read' | 'edit' | 'resolve' | 'dismiss'>('read')
  const [draft, setDraft] = useState<WorkItemIssue | null>(null)
  const [failure, setFailure] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)
  const inFlight = useRef(false)
  const id = useId()
  const data = draft ?? issue.data
  const pending = busy || mutation.isPending
  const conflict = failure instanceof ApiRequestError && failure.status === 409
  const start = (next: typeof mode) => {
    if (!issue.data) return
    setDraft(issue.data)
    setFailure(null)
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
