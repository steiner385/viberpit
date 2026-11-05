// The current milestone's one-line status ("Current · the long phase by design"),
// edited in place (spec 2026-11-05 §9 actions). Enter saves, Escape cancels, an
// empty line clears it. The cap mirrors the server's.
import { useId, useState } from 'react'
import type { Milestone } from '@/api/milestones'
import { STATUS_NOTE_MAX, milestoneErrorText, useSetStatusNote } from '@/api/milestones'
import { Button } from '@/ui/Button'
import { Input } from '@/ui/Input'
import { statusLine } from './format'

export function StatusNoteEditor({ slug, milestone: m }: { slug: string; milestone: Milestone }) {
  const save = useSetStatusNote(slug)
  const id = useId()
  const [draft, setDraft] = useState<string | null>(null)
  const error = milestoneErrorText(save.error)

  const cancel = () => {
    save.reset()
    setDraft(null)
  }
  const commit = () => {
    if (draft === null) return
    save.mutate({ key: m.key, statusNote: draft.trim() || null }, { onSuccess: () => setDraft(null) })
  }

  if (draft === null) {
    return (
      <div className="flex items-center gap-1">
        <p className="min-w-0 flex-1 text-xs font-medium text-accent [overflow-wrap:anywhere]">
          {statusLine(m)}
        </p>
        {/* A text button, not a pencil icon: a lucide icon shared with the startup
            chunks splits into a chunk of its own on the critical path (bundle budget). */}
        <Button type="button" size="sm" variant="ghost" className="-my-2 h-7 shrink-0 px-2 text-xs"
          aria-label="Edit status line" onClick={() => setDraft(m.statusNote ?? '')}>
          Edit
        </Button>
      </div>
    )
  }

  return (
    <form
      onSubmit={(e) => { e.preventDefault(); commit() }}
      className="flex flex-col gap-1"
    >
      <label htmlFor={id} className="sr-only">Status line</label>
      <Input
        id={id}
        size="sm"
        autoFocus
        maxLength={STATUS_NOTE_MAX}
        value={draft}
        placeholder="One line on where this stands"
        aria-describedby={`${id}-count`}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          // Escape must not reach a parent dialog/drawer; Enter saves here, not by
          // implicit submission, so one keypress is one write.
          if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); cancel() }
          if (e.key === 'Enter') { e.preventDefault(); commit() }
        }}
      />
      <div className="flex flex-wrap items-center gap-2">
        <span id={`${id}-count`} className="mr-auto text-xs text-ink-faint tabular-nums">
          {draft.length}/{STATUS_NOTE_MAX}
        </span>
        <Button type="button" size="sm" variant="ghost" onClick={cancel}>Cancel</Button>
        <Button type="submit" size="sm" loading={save.isPending}>Save</Button>
      </div>
      {error && <p role="alert" className="text-xs text-danger">{error}</p>}
    </form>
  )
}
