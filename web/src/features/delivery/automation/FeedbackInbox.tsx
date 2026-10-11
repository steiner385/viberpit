/**
 * The clustered inbox (#2936): Session Feedback's Needs you tab as one row per
 * problem rather than one per report. Fifty reports that were really a handful
 * of themes used to cost fifty reads and fifty confirmations; a cluster is read
 * once and settled once — filed as ONE issue with every report's evidence,
 * dismissed, or attached to an existing issue or a live session.
 *
 * Clustering is the server's (`server/automations/feedback_clusters.py`); this
 * only renders it and routes the actions to `/clusters/*`. Any set of reports —
 * whole clusters, single members, or a mix picked with the checkboxes — is
 * settled by ONE request after ONE confirmation. A cluster may carry merged PRs
 * that possibly fixed it, and reports that aged out wait, collapsed, in a
 * restorable digest.
 */
import { useEffect, useId, useRef, useState } from 'react'
import { toast } from 'sonner'
import {
  useAttachFeedbackCluster, useDismissFeedbackCluster, useFeedbackClusterDraft,
  useFeedbackClusters, useFileFeedbackCluster, useRestoreFeedbackReports, useSessions,
} from '@/api/hooks'
import type {
  ClusterAttachTarget, ClusterDraft, InboxClusters, InboxReport, PossibleFix, ReportCluster,
} from '@/api/automations'
import { extractMutationErrorMessage } from '@/api/queryClient'
import { INACTIVE_SESSION_STATUSES } from '@/api/types'
import { isValidTimestamp, relTime } from '@/lib/relativeTime'
import { Badge } from '@/ui/Badge'
import { Button } from '@/ui/Button'
import { Dialog } from '@/ui/Dialog'
import { Field } from '@/ui/Field'
import { Input } from '@/ui/Input'
import { RadioGroup } from '@/ui/RadioGroup'
import { Select } from '@/ui/Select'
import { Textarea } from '@/ui/Textarea'
import { BulkActionBar } from '@/ui/list/BulkActionBar'
import { useBulkSelection } from '@/ui/list/useBulkSelection'
import { useConfirm } from '@/ui/useConfirm'
import { ParkedNotice } from './actions'
import { outcomeToast } from './outcomeToast'

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? '' : 's'}`

const ids = (c: ReportCluster) => c.members.map((m) => m.id)

/** The reports one action settles: a whole cluster, or the bulk selection. */
interface Target {
  /** Keys the dialog, so each opening drafts afresh. */
  key: string
  ids: string[]
}

const NO_CLUSTERS: ReportCluster[] = []

/** What the model pass means for the rows on screen, when it is not simply done. */
const LLM_NOTE: Partial<Record<InboxClusters['llm'], string>> = {
  pending: 'Grouping with a model — themes update shortly.',
  failed: 'Grouped by similarity only — the model pass failed.',
}

function when(iso: string | null): string {
  return isValidTimestamp(iso) ? relTime(iso) : 'unknown'
}

export function FeedbackInbox({ automationId, paused = false }: {
  automationId: string
  /** The automation is paused: its reports are parked (see `ParkedNotice`). */
  paused?: boolean
}) {
  const clusters = useFeedbackClusters(automationId)
  const dismiss = useDismissFeedbackCluster(automationId)
  const { confirm, dialog } = useConfirm()
  const [filing, setFiling] = useState<Target | null>(null)
  const [attaching, setAttaching] = useState<Target | null>(null)
  const rows = clusters.data?.clusters ?? NO_CLUSTERS
  // Selection is over report ids; a cluster's checkbox is the set of its members.
  // Ids that leave the inbox (settled, or regrouped away) drop out on their own.
  const selection = useBulkSelection(rows.flatMap(ids))

  if (clusters.isLoading) return <p className="text-sm text-ink-muted">Loading…</p>
  if (clusters.isError || !clusters.data) return <p className="text-sm text-danger">Could not load the inbox.</p>
  const { total, llm, digest } = clusters.data
  if (rows.length === 0) {
    return (
      <div className="space-y-3">
        <p className="text-sm text-ink-muted">Nothing needs you.</p>
        <AgedOutDigest automationId={automationId} digest={digest} />
      </div>
    )
  }

  /** ONE confirmation, then ONE request for every report in *reportIds*. */
  async function onDismiss(reportIds: string[], fixedBy?: number): Promise<boolean> {
    const ok = await confirm({
      title: `Dismiss ${plural(reportIds.length, 'report')}${fixedBy ? ` as fixed by #${fixedBy}` : ''}?`,
      description: fixedBy
        ? `They leave the inbox, recorded as fixed by #${fixedBy}, and will not be filed.`
        : 'They leave the inbox and will not be filed.',
      confirmLabel: 'Dismiss',
      tone: 'danger',
    })
    if (!ok) return false
    try {
      const r = await dismiss.mutateAsync(fixedBy ? { ids: reportIds, fixedBy } : { ids: reportIds })
      outcomeToast(r)
      return r.level === 'ok'
    } catch (e) {
      toast.error(extractMutationErrorMessage(e, 'Could not dismiss'))
      return false
    }
  }

  const selectedIds = rows.flatMap(ids).filter((id) => selection.isSelected(id))

  return (
    <div className="space-y-3">
      {paused && <ParkedNotice heading={`Paused — ${total} parked`} />}
      <p className="text-sm text-ink-muted">
        {plural(total, 'report')} in {plural(rows.length, 'cluster')}
        {LLM_NOTE[llm] && <span className="block text-xs">{LLM_NOTE[llm]}</span>}
      </p>
      {selection.count > 0 && (
        <BulkActionBar label={`${selection.count} selected`} onClear={selection.clear}>
          <Button type="button" size="sm" variant="secondary"
            onClick={() => setFiling({ key: `sel:${selectedIds.join(',')}`, ids: selectedIds })}>
            File as one issue
          </Button>
          <Button type="button" size="sm" variant="danger"
            onClick={() => void onDismiss(selectedIds).then((ok) => ok && selection.clear())}>
            Dismiss
          </Button>
        </BulkActionBar>
      )}
      <ul className="space-y-2">
        {rows.map((c) => (
          <ClusterRow
            key={c.id}
            cluster={c}
            isSelected={selection.isSelected}
            onSelectCluster={(on) => selection.setMany(ids(c), on)}
            onSelectMember={(id) => selection.toggleOne(id)}
            onFile={() => setFiling({ key: c.id, ids: ids(c) })}
            onAttach={() => setAttaching({ key: c.id, ids: ids(c) })}
            onDismiss={(fixedBy) => void onDismiss(ids(c), fixedBy)}
          />
        ))}
      </ul>
      <AgedOutDigest automationId={automationId} digest={digest} />
      {dialog}
      {/* Keyed by target: each opening drafts afresh and starts a clean form. */}
      {filing && (
        <FileClusterDialog key={filing.key} automationId={automationId} reportIds={filing.ids}
          onClose={() => setFiling(null)} onFiled={selection.clear} />
      )}
      {attaching && (
        <AttachClusterDialog key={attaching.key} automationId={automationId} reportIds={attaching.ids}
          onClose={() => setAttaching(null)} />
      )}
    </div>
  )
}

/** A checkbox that can also read "some": `indeterminate` is a DOM property, not
 *  an attribute (same idiom as `ArchivedSessionList`'s select-all). */
function SelectBox({ label, checked, indeterminate = false, onChange }: {
  label: string
  checked: boolean
  indeterminate?: boolean
  onChange: (next: boolean) => void
}) {
  const ref = useRef<HTMLInputElement>(null)
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = indeterminate
  }, [indeterminate])
  return (
    <input
      ref={ref}
      type="checkbox"
      aria-label={label}
      checked={checked}
      onChange={(e) => onChange(e.target.checked)}
      className="mt-1 h-4 w-4 shrink-0 rounded-sm border-edge-strong accent-accent focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
    />
  )
}

function ClusterRow({ cluster: c, isSelected, onSelectCluster, onSelectMember, onFile, onAttach, onDismiss }: {
  cluster: ReportCluster
  isSelected: (id: string) => boolean
  onSelectCluster: (on: boolean) => void
  onSelectMember: (id: string) => void
  onFile: () => void
  onAttach: () => void
  onDismiss: (fixedBy?: number) => void
}) {
  const [open, setOpen] = useState(false)
  const membersId = useId()
  const picked = c.members.filter((m) => isSelected(m.id)).length
  return (
    <li data-cluster={c.id} className="rounded-md border border-edge p-2 text-sm">
      <div className="flex flex-wrap items-start gap-2">
        <SelectBox
          label={`Select cluster: ${c.title}`}
          checked={picked === c.count}
          indeterminate={picked > 0 && picked < c.count}
          onChange={onSelectCluster}
        />
        <button
          type="button"
          className="min-w-0 flex-1 text-left"
          aria-expanded={open}
          aria-controls={membersId}
          onClick={() => setOpen((o) => !o)}
        >
          <span className="flex items-center gap-2">
            <span className="min-w-0 truncate font-medium">{c.title}</span>
            <Badge count={c.count} aria-label={plural(c.count, 'report')} />
          </span>
          <span className="block truncate text-xs text-ink-muted">
            {c.category ?? '—'} · {c.impact ?? '—'} · last {when(c.lastReported)}
          </span>
        </button>
        <span className="flex flex-wrap gap-2">
          <Button type="button" size="sm" variant="secondary" onClick={onFile}>File as one issue</Button>
          <Button type="button" size="sm" variant="secondary" onClick={onAttach}>Attach…</Button>
          <Button type="button" size="sm" variant="danger" onClick={() => onDismiss()}>Dismiss</Button>
        </span>
      </div>
      {c.possiblyFixed.map((fix) => (
        <PossiblyFixed key={fix.number} fix={fix} onDismiss={() => onDismiss(fix.number)} />
      ))}
      {open && (
        <ul id={membersId} className="mt-2 space-y-1 border-t border-edge pt-2">
          {c.members.map((m) => (
            <li key={m.id} className="flex items-start gap-2">
              <SelectBox label={`Select report: ${m.title}`} checked={isSelected(m.id)}
                onChange={() => onSelectMember(m.id)} />
              <span className="min-w-0">
                <span className="block truncate">{m.title}</span>
                <span className="block truncate text-xs text-ink-muted">
                  {m.session ?? '?'} · {m.impact ?? '—'} · {when(m.reportedAt)}
                </span>
              </span>
            </li>
          ))}
        </ul>
      )}
    </li>
  )
}

/** A merged PR whose title matches the cluster, merged after its first report —
 *  a hint only; dismissing records the PR on every report. */
function PossiblyFixed({ fix, onDismiss }: { fix: PossibleFix; onDismiss: () => void }) {
  const ref = `#${fix.number}`
  return (
    <p className="mt-2 flex flex-wrap items-center gap-2 text-xs text-ink-muted">
      <span className="min-w-0 truncate">
        Possibly fixed by{' '}
        {fix.url
          ? <a href={fix.url} target="_blank" rel="noreferrer" className="text-accent underline">{ref}</a>
          : ref}
        {' '}— {fix.title}{fix.mergedAt && isValidTimestamp(fix.mergedAt) ? `, merged ${relTime(fix.mergedAt)}` : ''}
      </span>
      <Button type="button" size="sm" variant="ghost" onClick={onDismiss}>Dismiss as fixed by {ref}</Button>
    </p>
  )
}

/** Reports that aged out of the inbox: out of the way (collapsed, never badged),
 *  each one click from coming back. */
function AgedOutDigest({ automationId, digest }: { automationId: string; digest: InboxClusters['digest'] }) {
  const restore = useRestoreFeedbackReports(automationId)
  const [open, setOpen] = useState(false)
  const listId = useId()
  if (digest.count === 0) return null

  async function onRestore(r: InboxReport) {
    try {
      outcomeToast(await restore.mutateAsync({ ids: [r.id] }))
    } catch (e) {
      toast.error(extractMutationErrorMessage(e, 'Could not restore'))
    }
  }

  return (
    <section className="text-sm">
      <button type="button" className="text-ink-muted underline-offset-2 hover:underline"
        aria-expanded={open} aria-controls={listId} onClick={() => setOpen((o) => !o)}>
        Aged out ({digest.count})
      </button>
      {open && (
        <div id={listId} className="mt-2 space-y-1">
          <p className="text-xs text-ink-muted">
            Old reports whose problem nobody reported again. They are kept, not filed.
            {digest.count > digest.items.length && ` Showing the newest ${digest.items.length}.`}
          </p>
          <ul className="space-y-1">
            {digest.items.map((r) => (
              <li key={r.id} className="flex items-start gap-2">
                <span className="min-w-0 flex-1">
                  <span className="block truncate">{r.title}</span>
                  <span className="block truncate text-xs text-ink-muted">
                    {r.session ?? '?'} · {r.impact ?? '—'} · {when(r.reportedAt)}
                  </span>
                </span>
                <Button type="button" size="sm" variant="secondary" aria-label={`Restore ${r.title}`}
                  onClick={() => void onRestore(r)}>
                  Restore
                </Button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  )
}

function FileClusterDialog({ automationId, reportIds, onClose, onFiled }: {
  automationId: string
  reportIds: string[]
  onClose: () => void
  onFiled?: () => void
}) {
  const draft = useFeedbackClusterDraft(automationId, reportIds, true)
  return (
    <Dialog open onClose={onClose} title="File as one issue" size="lg"
      description={`One issue for ${plural(reportIds.length, 'report')}, with every report's evidence. Edit before filing.`}>
      {draft.isError && <p role="alert" className="text-sm text-danger">Could not draft the issue — the reports may have left the inbox.</p>}
      {!draft.data && !draft.isError && <p className="text-sm text-ink-muted">Drafting…</p>}
      {draft.data && (
        <FileForm automationId={automationId} reportIds={reportIds} draft={draft.data}
          onClose={onClose} onFiled={onFiled} />
      )}
    </Dialog>
  )
}

/** Mounted once the draft has answered, so the editable fields start from it. */
function FileForm({ automationId, reportIds, draft, onClose, onFiled }: {
  automationId: string
  reportIds: string[]
  draft: ClusterDraft
  onClose: () => void
  onFiled?: () => void
}) {
  const file = useFileFeedbackCluster(automationId)
  const [title, setTitle] = useState(draft.title)
  const [body, setBody] = useState(draft.body)
  const [error, setError] = useState<string | null>(null)
  const titleId = useId()
  const bodyId = useId()

  async function submit() {
    setError(null)
    try {
      const r = await file.mutateAsync({ ids: reportIds, title, body })
      outcomeToast(r)
      if (r.level === 'ok') {
        onFiled?.()
        onClose()
      } else setError(r.message)
    } catch (e) {
      setError(extractMutationErrorMessage(e, 'Could not file the issue'))
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <Field label="Title" htmlFor={titleId}>
        <Input id={titleId} value={title} onChange={(e) => setTitle(e.target.value)} />
      </Field>
      <Field label="Body" htmlFor={bodyId}>
        <Textarea id={bodyId} rows={14} className="font-mono text-xs" value={body} onChange={(e) => setBody(e.target.value)} />
      </Field>
      {error && <p role="alert" className="text-sm text-danger">{error}</p>}
      <div className="flex justify-end gap-3">
        <Button type="button" variant="ghost" onClick={onClose}>Cancel</Button>
        <Button type="button" variant="primary" size="lg" onClick={() => void submit()}
          disabled={!title.trim() || !body.trim()} loading={file.isPending}>
          File issue
        </Button>
      </div>
    </div>
  )
}

type AttachKind = 'issue' | 'session'

function AttachClusterDialog({ automationId, reportIds, onClose }: {
  automationId: string
  reportIds: string[]
  onClose: () => void
}) {
  const attach = useAttachFeedbackCluster(automationId)
  const sessions = useSessions()
  const [kind, setKind] = useState<AttachKind>('issue')
  const [issue, setIssue] = useState('')
  const [sessionId, setSessionId] = useState('')
  const [error, setError] = useState<string | null>(null)
  const name = useId()
  const issueId = useId()
  const sessionFieldId = useId()
  const live = (sessions.data ?? []).filter((s) => !INACTIVE_SESSION_STATUSES.has(s.status))
  const number = Number.parseInt(issue, 10)
  const target: ClusterAttachTarget | null = kind === 'issue'
    ? (Number.isInteger(number) && number > 0 ? { issue: number } : null)
    : (sessionId ? { sessionId } : null)

  async function submit() {
    if (!target) return
    setError(null)
    try {
      const r = await attach.mutateAsync({ ids: reportIds, ...target })
      outcomeToast(r)
      if (r.level === 'ok') onClose()
      else setError(r.message)
    } catch (e) {
      setError(extractMutationErrorMessage(e, 'Could not attach'))
    }
  }

  return (
    <Dialog open onClose={onClose} title={`Attach ${plural(reportIds.length, 'report')}`}
      description="Posts every report's evidence there, then takes them out of the inbox.">
      <div className="flex flex-col gap-3">
        <RadioGroup<AttachKind>
          name={name}
          legend="Attach to"
          value={kind}
          onChange={setKind}
          options={[{ value: 'issue', label: 'An existing issue' }, { value: 'session', label: 'A live session' }]}
        />
        {kind === 'issue' ? (
          <Field label="Issue number" htmlFor={issueId}>
            <Input id={issueId} inputMode="numeric" value={issue} onChange={(e) => setIssue(e.target.value)} placeholder="2909" />
          </Field>
        ) : (
          <Field label="Session" htmlFor={sessionFieldId}>
            <Select id={sessionFieldId} value={sessionId} onChange={(e) => setSessionId(e.target.value)}>
              <option value="">Pick a session…</option>
              {live.map((s) => <option key={s.id} value={s.id}>{s.name || s.id}</option>)}
            </Select>
          </Field>
        )}
        {error && <p role="alert" className="text-sm text-danger">{error}</p>}
        <div className="flex justify-end gap-3">
          <Button type="button" variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="button" variant="primary" size="lg" onClick={() => void submit()} disabled={!target} loading={attach.isPending}>
            Attach
          </Button>
        </div>
      </div>
    </Dialog>
  )
}
