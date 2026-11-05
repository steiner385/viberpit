// The side column's owner notes (spec 2026-11-05 §9, M11). One component, two
// panels: **Needs you** (open asks, the current milestone's first, each with Done
// and Drop; done and dropped ones collapsed under Resolved, each with Reopen) and **Decisions** (locked, then proposed; superseded collapsed). The
// page places them around Numbers: Needs you, Numbers, Decisions. Either panel
// adds a note of either kind through the one Add dialog.
import { useId, useState } from 'react'
import type {
  AskStatus, DecisionStatus, MilestoneNote, NoteKind,
} from '@/api/milestones'
import { NOTE_REF_MAX, NOTE_TEXT_MAX, milestoneErrorText, useAddNote, useUpdateNote } from '@/api/milestones'
import { isSafeHttpUrl } from '@/lib/safeUrl'
import { Button } from '@/ui/Button'
import { Dialog } from '@/ui/Dialog'
import { Field } from '@/ui/Field'
import { Input } from '@/ui/Input'
import { SectionHeading } from '@/ui/SectionHeading'
import { Select } from '@/ui/Select'
import { Textarea } from '@/ui/Textarea'
import { type LoadedView, milestoneTitle } from './format'

const NOTE_GRID = 'grid gap-2 lg:grid-cols-2 2xl:grid-cols-1'

type NoteAction = { label: string; status: DecisionStatus | AskStatus }

/** The moves each note offers, by kind and status. */
function actionsFor(n: MilestoneNote): NoteAction[] {
  if (n.kind === 'ask') {
    return n.status === 'open'
      ? [{ label: 'Done', status: 'done' }, { label: 'Drop', status: 'dropped' }]
      : [{ label: 'Reopen', status: 'open' }]
  }
  if (n.status === 'proposed') return [{ label: 'Lock', status: 'locked' }, { label: 'Supersede', status: 'superseded' }]
  if (n.status === 'locked') return [{ label: 'Supersede', status: 'superseded' }]
  return []
}

function NoteItem({ note: n, where, onAct, busy }: {
  note: MilestoneNote
  where: string
  onAct: (status: DecisionStatus | AskStatus) => void
  busy: boolean
}) {
  const actions = actionsFor(n)
  return (
    <li className="flex min-w-0 flex-col gap-1 rounded-md border border-edge bg-surface p-3 text-sm">
      <p className="text-ink [overflow-wrap:anywhere]">
        {n.ref && <span className="mr-1.5 font-mono text-xs text-ink-muted">{n.ref}</span>}
        {/* Only an absolute http(s) link becomes an href (stored-XSS guard). */}
        {isSafeHttpUrl(n.url)
          ? <a href={n.url} target="_blank" rel="noreferrer" className="hover:underline">{n.text}</a>
          : n.text}
      </p>
      <p className="text-xs text-ink-muted [overflow-wrap:anywhere]">
        {where}
        {n.kind === 'decision' && n.status === 'proposed' && <> · <span className="font-medium text-warning">Proposed</span></>}
      </p>
      {actions.length > 0 && (
        <div className="flex flex-wrap gap-2 pt-1">
          {actions.map((a) => (
            <Button key={a.status} type="button" size="sm" variant="secondary" disabled={busy}
              aria-label={`${a.label}: ${n.text}`} onClick={() => onAct(a.status)}>
              {a.label}
            </Button>
          ))}
        </div>
      )}
    </li>
  )
}

function AddNoteDialog({ onClose, slug, view, kind: initialKind }: {
  onClose: () => void
  slug: string
  view: LoadedView
  kind: NoteKind
}) {
  const add = useAddNote(slug)
  const id = useId()
  const [kind, setKind] = useState<NoteKind>(initialKind)
  const [status, setStatus] = useState<DecisionStatus>('locked')
  const [text, setText] = useState('')
  const [ref, setRef] = useState('')
  const [url, setUrl] = useState('')
  const [milestoneKey, setMilestoneKey] = useState('')
  const error = milestoneErrorText(add.error)
  // The server refuses a note on a removed rung; the view keeps only the current
  // one of those, flagged by `currentRemoved`.
  const { currentKey, currentRemoved } = view.ladder
  const pickable = view.milestones.filter((m) => !(currentRemoved && m.key === currentKey))
  const ready = text.trim() !== ''

  const close = () => {
    add.reset()
    onClose()
  }
  const submit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!ready) return
    add.mutate({
      kind,
      text: text.trim(),
      ref: ref.trim() || null,
      url: url.trim() || null,
      milestoneKey: milestoneKey || null,
      ...(kind === 'decision' ? { status } : {}),
    }, { onSuccess: close })
  }

  return (
    <Dialog open onClose={close} title="Add a note">
      <form onSubmit={submit} className="flex flex-col gap-4">
        {/* Selects, not RadioGroup: RadioGroup shares a startup chunk, and importing
            it here splits that chunk onto the critical path (bundle budget). */}
        <Field label="Kind" htmlFor={`${id}-kind`}>
          <Select id={`${id}-kind`} value={kind} onChange={(e) => setKind(e.target.value as NoteKind)}>
            <option value="ask">Ask</option>
            <option value="decision">Decision</option>
          </Select>
        </Field>
        {kind === 'decision' && (
          <Field label="Status" htmlFor={`${id}-status`}>
            <Select id={`${id}-status`} value={status} onChange={(e) => setStatus(e.target.value as DecisionStatus)}>
              <option value="locked">Locked</option>
              <option value="proposed">Proposed</option>
            </Select>
          </Field>
        )}
        <Field label="Text" htmlFor={`${id}-text`} required>
          <Textarea id={`${id}-text`} rows={3} maxLength={NOTE_TEXT_MAX} value={text}
            onChange={(e) => setText(e.target.value)} />
        </Field>
        <Field label="Reference (optional)" htmlFor={`${id}-ref`} help="A short handle, such as D3 or Q2.">
          <Input id={`${id}-ref`} maxLength={NOTE_REF_MAX} value={ref} onChange={(e) => setRef(e.target.value)} />
        </Field>
        <Field label="Link (optional)" htmlFor={`${id}-url`}>
          <Input id={`${id}-url`} type="url" inputMode="url" placeholder="https://" value={url}
            onChange={(e) => setUrl(e.target.value)} />
        </Field>
        <Field label="Milestone" htmlFor={`${id}-milestone`}>
          <Select id={`${id}-milestone`} value={milestoneKey} onChange={(e) => setMilestoneKey(e.target.value)}>
            <option value="">Whole ladder</option>
            {pickable.map((m) => <option key={m.key} value={m.key}>{milestoneTitle(m)}</option>)}
          </Select>
        </Field>
        {error && <p role="alert" className="text-sm text-danger">{error}</p>}
        <div className="flex flex-wrap justify-end gap-2">
          <Button type="button" variant="secondary" size="lg" onClick={close}>Cancel</Button>
          <Button type="submit" size="lg" disabled={!ready} loading={add.isPending}>Add note</Button>
        </div>
      </form>
    </Dialog>
  )
}

export function NotesPanel({ kind, slug, view }: { kind: NoteKind; slug: string; view: LoadedView }) {
  const update = useUpdateNote(slug)
  const headingId = useId()
  const [adding, setAdding] = useState(false)
  const error = milestoneErrorText(update.error)

  const titles = new Map(view.milestones.map((m) => [m.key, milestoneTitle(m)]))
  const where = (n: MilestoneNote) => n.milestoneKey === null ? 'Whole ladder' : titles.get(n.milestoneKey) ?? n.milestoneKey
  const act = (n: MilestoneNote) => (status: DecisionStatus | AskStatus) => update.mutate({ id: n.id, status })
  const item = (n: MilestoneNote) => (
    <NoteItem key={n.id} note={n} where={where(n)} onAct={act(n)} busy={update.isPending} />
  )

  const current = view.ladder.currentKey
  const asks = view.notes.asks.filter((n) => n.status === 'open')
  const shown = kind === 'ask'
    // The current milestone's asks first; the server's oldest-first order otherwise.
    ? [...asks.filter((n) => current !== null && n.milestoneKey === current),
       ...asks.filter((n) => current === null || n.milestoneKey !== current)]
    : view.notes.decisions.filter((n) => n.status !== 'superseded')
  // Collapsed under the main list: superseded decisions, or resolved (done / dropped) asks.
  const folded = kind === 'decision'
    ? view.notes.decisions.filter((n) => n.status === 'superseded')
    : view.notes.asks.filter((n) => n.status !== 'open')
  const foldedTitle = kind === 'decision' ? 'Superseded' : 'Resolved'
  const title = kind === 'ask' ? 'Needs you' : 'Decisions'

  return (
    <section aria-labelledby={headingId} className="flex min-w-0 flex-col gap-2">
      <SectionHeading
        id={headingId}
        actions={
          <Button type="button" size="sm" variant="ghost" onClick={() => setAdding(true)}
            aria-label={kind === 'ask' ? 'Add an ask' : 'Add a decision'}>
            Add
          </Button>
        }
      >
        {title}
      </SectionHeading>
      {shown.length > 0
        // Stacked under the ladder below 2xl, two notes a row; beside it, a column (as Numbers).
        ? <ul aria-label={kind === 'ask' ? 'Open asks' : 'Decisions'} className={NOTE_GRID}>{shown.map(item)}</ul>
        : <p className="text-sm text-ink-muted">{kind === 'ask' ? 'Nothing needs you.' : 'No decisions yet.'}</p>}
      {folded.length > 0 && (
        <details className="text-sm">
          <summary className="cursor-pointer text-ink-muted">{foldedTitle} ({folded.length})</summary>
          <ul aria-label={kind === 'decision' ? 'Superseded decisions' : 'Resolved asks'}
            className={`mt-2 opacity-75 ${NOTE_GRID}`}>{folded.map(item)}</ul>
        </details>
      )}
      {error && <p role="alert" className="text-xs text-danger">{error}</p>}
      {adding && <AddNoteDialog onClose={() => setAdding(false)} slug={slug} view={view} kind={kind} />}
    </section>
  )
}
