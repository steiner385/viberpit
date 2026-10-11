/**
 * The clustered inbox (#2936): Session Feedback's Needs you tab as one row per
 * problem rather than one per report. Fifty reports that were really a handful
 * of themes used to cost fifty reads and fifty confirmations; a cluster is read
 * once and settled once — filed as ONE issue with every report's evidence,
 * dismissed, or attached to an existing issue or a live session.
 *
 * Clustering is the server's (`server/automations/feedback_clusters.py`); this
 * only renders it and routes the four actions to `/clusters/*`.
 */
import { useId, useState } from 'react'
import { toast } from 'sonner'
import {
  useAttachFeedbackCluster, useDismissFeedbackCluster, useFeedbackClusterDraft,
  useFeedbackClusters, useFileFeedbackCluster, useSessions,
} from '@/api/hooks'
import type { ClusterAttachTarget, ClusterDraft, InboxClusters, ReportCluster } from '@/api/automations'
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
import { useConfirm } from '@/ui/useConfirm'
import { ParkedNotice } from './actions'
import { outcomeToast } from './outcomeToast'

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? '' : 's'}`

const ids = (c: ReportCluster) => c.members.map((m) => m.id)

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
  const [filing, setFiling] = useState<ReportCluster | null>(null)
  const [attaching, setAttaching] = useState<ReportCluster | null>(null)

  if (clusters.isLoading) return <p className="text-sm text-ink-muted">Loading…</p>
  if (clusters.isError || !clusters.data) return <p className="text-sm text-danger">Could not load the inbox.</p>
  const { clusters: rows, total, llm } = clusters.data
  if (rows.length === 0) return <p className="text-sm text-ink-muted">Nothing needs you.</p>

  async function onDismiss(c: ReportCluster) {
    const ok = await confirm({
      title: `Dismiss ${plural(c.count, 'report')}?`,
      description: 'They leave the inbox and will not be filed.',
      confirmLabel: 'Dismiss',
      tone: 'danger',
    })
    if (!ok) return
    try {
      outcomeToast(await dismiss.mutateAsync({ ids: ids(c) }))
    } catch (e) {
      toast.error(extractMutationErrorMessage(e, 'Could not dismiss'))
    }
  }

  return (
    <div className="space-y-3">
      {paused && <ParkedNotice heading={`Paused — ${total} parked`} />}
      <p className="text-sm text-ink-muted">
        {plural(total, 'report')} in {plural(rows.length, 'cluster')}
        {LLM_NOTE[llm] && <span className="block text-xs">{LLM_NOTE[llm]}</span>}
      </p>
      <ul className="space-y-2">
        {rows.map((c) => (
          <ClusterRow
            key={c.id}
            cluster={c}
            onFile={() => setFiling(c)}
            onAttach={() => setAttaching(c)}
            onDismiss={() => void onDismiss(c)}
          />
        ))}
      </ul>
      {dialog}
      {/* Keyed by cluster: each opening drafts afresh and starts a clean form. */}
      {filing && (
        <FileClusterDialog key={filing.id} automationId={automationId} cluster={filing} onClose={() => setFiling(null)} />
      )}
      {attaching && (
        <AttachClusterDialog key={attaching.id} automationId={automationId} cluster={attaching} onClose={() => setAttaching(null)} />
      )}
    </div>
  )
}

function ClusterRow({ cluster: c, onFile, onAttach, onDismiss }: {
  cluster: ReportCluster
  onFile: () => void
  onAttach: () => void
  onDismiss: () => void
}) {
  const [open, setOpen] = useState(false)
  const membersId = useId()
  return (
    <li data-cluster={c.id} className="rounded-md border border-edge p-2 text-sm">
      <div className="flex flex-wrap items-start gap-2">
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
          <Button type="button" size="sm" variant="danger" onClick={onDismiss}>Dismiss</Button>
        </span>
      </div>
      {open && (
        <ul id={membersId} className="mt-2 space-y-1 border-t border-edge pt-2">
          {c.members.map((m) => (
            <li key={m.id}>
              <span className="block truncate">{m.title}</span>
              <span className="block truncate text-xs text-ink-muted">
                {m.session ?? '?'} · {m.impact ?? '—'} · {when(m.reportedAt)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </li>
  )
}

function FileClusterDialog({ automationId, cluster, onClose }: {
  automationId: string
  cluster: ReportCluster
  onClose: () => void
}) {
  const draft = useFeedbackClusterDraft(automationId, ids(cluster), true)
  return (
    <Dialog open onClose={onClose} title="File as one issue" size="lg"
      description={`One issue for ${plural(cluster.count, 'report')}, with every report's evidence. Edit before filing.`}>
      {draft.isError && <p role="alert" className="text-sm text-danger">Could not draft the issue — the reports may have left the inbox.</p>}
      {!draft.data && !draft.isError && <p className="text-sm text-ink-muted">Drafting…</p>}
      {draft.data && <FileForm automationId={automationId} cluster={cluster} draft={draft.data} onClose={onClose} />}
    </Dialog>
  )
}

/** Mounted once the draft has answered, so the editable fields start from it. */
function FileForm({ automationId, cluster, draft, onClose }: {
  automationId: string
  cluster: ReportCluster
  draft: ClusterDraft
  onClose: () => void
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
      const r = await file.mutateAsync({ ids: ids(cluster), title, body })
      outcomeToast(r)
      if (r.level === 'ok') onClose()
      else setError(r.message)
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

function AttachClusterDialog({ automationId, cluster, onClose }: {
  automationId: string
  cluster: ReportCluster
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
      const r = await attach.mutateAsync({ ids: ids(cluster), ...target })
      outcomeToast(r)
      if (r.level === 'ok') onClose()
      else setError(r.message)
    } catch (e) {
      setError(extractMutationErrorMessage(e, 'Could not attach'))
    }
  }

  return (
    <Dialog open onClose={onClose} title={`Attach ${plural(cluster.count, 'report')}`}
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
