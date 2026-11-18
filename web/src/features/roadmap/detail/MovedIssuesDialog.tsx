// "Moved issues…" (spec 2026-11-18 product repo lifecycle, "Moved-issue map"):
// the product's old → new issue pairs and whether each has migrated, the
// server's title-match suggestions between two of its repos, and manual pairs.
// Suggestions are offered UNticked — the server never auto-applies them, and
// neither does this dialog: only what the user ticks or types is posted.
import { useState } from 'react'
import { ISSUE_REF_RE, isActiveRepo, primaryRepoOf, type Project } from '@/api/pm'
import { useMovedRefs, useRecordMovedRefs, useSuggestMovedRefs } from '@/api/hooks'
import { extractMutationErrorMessage } from '@/api/queryClient'
import { Badge } from '@/ui/Badge'
import { Button } from '@/ui/Button'
import { Checkbox } from '@/ui/Checkbox'
import { Dialog } from '@/ui/Dialog'
import { Field } from '@/ui/Field'
import { Input } from '@/ui/Input'
import { SectionHeading } from '@/ui/SectionHeading'
import { Select } from '@/ui/Select'

type Pair = { old: string; new: string }

const pairKey = (p: Pair) => `${p.old}→${p.new}`

export function MovedIssuesDialog({
  project, open, onClose,
}: { project: Project; open: boolean; onClose: () => void }) {
  const slug = project.slug ?? ''
  // Old refs must live in a RETIRED repo (the server refuses an active one's with
  // 422 bad_pairs: migration releases the old issue's hold).
  const retiredRepos = project.repos.filter((r) => !isActiveRepo(r) && !!r.repo).map((r) => r.repo as string)
  const activeRepos = project.repos.filter((r) => isActiveRepo(r) && !!r.repo).map((r) => r.repo as string)
  // Issues move OUT of a retired repo INTO the primary — the usual case.
  const [from, setFrom] = useState(() => retiredRepos[0] ?? '')
  const [to, setTo] = useState(() => primaryRepoOf(project) ?? '')

  const stored = useMovedRefs(slug, { enabled: open })
  const suggested = useSuggestMovedRefs(slug, from, to)
  const record = useRecordMovedRefs(slug)

  const [ticked, setTicked] = useState<ReadonlySet<string>>(new Set())
  const [manual, setManual] = useState<Pair[]>([])
  const [oldRef, setOldRef] = useState('')
  const [newRef, setNewRef] = useState('')

  const suggestions = suggested.data?.pairs ?? []
  const selected: Pair[] = [
    ...suggestions.filter((s) => ticked.has(pairKey(s))).map((s) => ({ old: s.old, new: s.new })),
    ...manual,
  ]
  // One pair per old ref: a duplicate would collide on its key here and fail the
  // whole batch server-side.
  const selectedOlds = new Set(selected.map((p) => p.old))
  const manualOlds = new Set(manual.map((p) => p.old))
  const oldTaken = selectedOlds.has(oldRef.trim())
  const canAddManual = ISSUE_REF_RE.test(oldRef.trim()) && ISSUE_REF_RE.test(newRef.trim()) && !oldTaken
  const canApply = selected.length > 0 && !record.isPending

  return (
    <Dialog open={open} onClose={onClose} title="Moved issues" size="lg">
      <div className="flex flex-col gap-4 text-sm">
        <section aria-labelledby="mi-stored" className="flex flex-col gap-2">
          <SectionHeading id="mi-stored">Mapped</SectionHeading>
          {stored.isError
            ? <p role="alert" className="text-danger">{extractMutationErrorMessage(stored.error, 'Could not load the moved issues.')}</p>
            : (stored.data?.pairs.length ?? 0) === 0
            ? <p className="text-ink-muted">{stored.isLoading ? 'Loading…' : 'No issues mapped yet.'}</p>
            : (
              <ul aria-label="Moved issues" className="flex flex-col gap-1">
                {stored.data!.pairs.map((p) => (
                  <li key={p.old} className="flex items-center gap-2">
                    <span>{p.old} → {p.new}</span>
                    <Badge tone={p.status === 'migrated' ? 'success' : 'warning'}>{p.status}</Badge>
                  </li>
                ))}
              </ul>
            )}
        </section>

        <section aria-labelledby="mi-suggest" className="flex flex-col gap-2">
          <SectionHeading id="mi-suggest">Suggested by title</SectionHeading>
          <div className="flex flex-wrap gap-2">
            <Field
              label="From" htmlFor="mi-from" className="min-w-0 flex-1"
              help={retiredRepos.length === 0 ? 'Retire the old repository first' : undefined}
            >
              <Select id="mi-from" size="sm" value={from} disabled={retiredRepos.length === 0} onChange={(e) => setFrom(e.target.value)}>
                <option value="">Choose…</option>
                {retiredRepos.map((r) => <option key={r} value={r}>{r}</option>)}
              </Select>
            </Field>
            <Field label="To" htmlFor="mi-to" className="min-w-0 flex-1">
              <Select id="mi-to" size="sm" value={to} onChange={(e) => setTo(e.target.value)}>
                <option value="">Choose…</option>
                {activeRepos.map((r) => <option key={r} value={r}>{r}</option>)}
              </Select>
            </Field>
          </div>
          {suggested.isError
            ? <p className="text-danger">{extractMutationErrorMessage(suggested.error, 'Could not load suggestions.')}</p>
            : suggestions.length === 0
              ? <p className="text-ink-muted">{suggested.isLoading ? 'Loading…' : 'No title matches.'}</p>
              : suggestions.map((s, i) => (
                <Checkbox
                  key={pairKey(s)}
                  id={`mi-s-${i}`}
                  label={`${s.old} → ${s.new} · ${s.title}`}
                  checked={ticked.has(pairKey(s))}
                  // Already added by hand: ticking it too would duplicate the old ref.
                  disabled={manualOlds.has(s.old)}
                  onChange={(on) => setTicked((prev) => {
                    const next = new Set(prev)
                    if (on) next.add(pairKey(s))
                    else next.delete(pairKey(s))
                    return next
                  })}
                />
              ))}
        </section>

        <section aria-labelledby="mi-manual" className="flex flex-col gap-2">
          <SectionHeading id="mi-manual">Add a pair</SectionHeading>
          <form
            className="flex flex-wrap items-end gap-2"
            onSubmit={(e) => {
              e.preventDefault()
              if (!canAddManual) return
              setManual((prev) => [...prev, { old: oldRef.trim(), new: newRef.trim() }])
              setOldRef('')
              setNewRef('')
            }}
          >
            <Field
              label="Old issue" htmlFor="mi-old" className="min-w-0 flex-1"
              error={oldTaken ? 'Already selected' : undefined}
            >
              <Input id="mi-old" size="sm" placeholder="owner/repo#N" value={oldRef} onChange={(e) => setOldRef(e.target.value)} />
            </Field>
            <Field label="New issue" htmlFor="mi-new" className="min-w-0 flex-1">
              <Input id="mi-new" size="sm" placeholder="owner/repo#N" value={newRef} onChange={(e) => setNewRef(e.target.value)} />
            </Field>
            <Button type="submit" size="sm" variant="secondary" disabled={!canAddManual}>Add pair</Button>
          </form>
          {manual.length > 0 && (
            <ul aria-label="Pairs to apply" className="flex flex-col gap-1">
              {manual.map((p, i) => (
                <li key={pairKey(p)} className="flex items-center gap-2">
                  <span>{p.old} → {p.new}</span>
                  <Button
                    size="sm" variant="ghost" aria-label={`Remove ${p.old} → ${p.new}`}
                    onClick={() => setManual((prev) => prev.filter((_, j) => j !== i))}
                  >
                    Remove
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </section>

        {record.isError && (
          <p role="alert" className="text-danger">
            {extractMutationErrorMessage(record.error, 'Could not record the moved issues.')}
          </p>
        )}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>Close</Button>
          <Button
            type="button" size="lg" disabled={!canApply} loading={record.isPending}
            onClick={() => record.mutate(selected, {
              onSuccess: () => {
                setTicked(new Set())
                setManual([])
              },
            })}
          >
            Apply
          </Button>
        </div>
      </div>
    </Dialog>
  )
}
