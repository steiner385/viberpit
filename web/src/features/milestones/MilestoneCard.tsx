// One rung of the ladder (spec 2026-11-05 §9): header (label, name, audience,
// platform chips, status line), the gate, the leaves bar, and the open leaves.
// The current card is the `aria-current="step"`; done cards are muted. The current
// card edits its status line and advances; a card not yet done ticks its manual
// conditions; a done card is a record and offers nothing.
import { useId, useState } from 'react'
import type { AdvanceMutation, Milestone, MilestoneLeaf } from '@/api/milestones'
import { Banner } from '@/ui/Banner'
import { Button } from '@/ui/Button'
import { Card } from '@/ui/Card'
import { cn } from '@/lib/utils'
import { ConditionRow } from './ConditionRow'
import { AdvanceDialog } from './AdvanceDialog'
import { LeavesBar } from './LeavesBar'
import { StatusNoteEditor } from './StatusNoteEditor'
import { milestoneTitle, statusLine } from './format'

/** Open leaves shown on a card before "+N more" (spec §9; print caps at 4 in Phase 2). */
const OPEN_LEAVES_CAP = 6

function LeafRow({ leaf }: { leaf: MilestoneLeaf }) {
  const title = leaf.title ?? leaf.ref
  return (
    <li className="flex flex-wrap items-baseline gap-x-2 text-xs">
      {leaf.url
        ? <a href={leaf.url} target="_blank" rel="noreferrer" className="min-w-0 truncate text-ink hover:underline">{title}</a>
        : <span className="min-w-0 truncate text-ink">{title}</span>}
      {leaf.prs.map((pr) => pr.url && (
        <a key={pr.url} href={pr.url} target="_blank" rel="noreferrer" className="text-accent hover:underline">
          PR #{pr.number}
        </a>
      ))}
      {leaf.state === 'not_started' && <span className="text-ink-faint">not started</span>}
    </li>
  )
}

export function MilestoneCard({ milestone: m, slug, next, advance }: {
  milestone: Milestone
  slug: string
  /** The page's advance mutation (its error must outlive this card). */
  advance: AdvanceMutation
  /** The rung an Advance moves to (null: it completes the ladder). Read only on the current card. */
  next: Milestone | null
}) {
  const headingId = useId()
  const [advancing, setAdvancing] = useState(false)
  const current = m.state === 'current'
  const done = m.state === 'done'
  const open = [
    ...m.leaves.items.filter((l) => l.state === 'in_flight'),
    ...m.leaves.items.filter((l) => l.state === 'not_started'),
  ]
  const hidden = open.length - OPEN_LEAVES_CAP
  const epicUrl = m.epics[0]?.url ?? null

  return (
    <Card
      as="li"
      aria-labelledby={headingId}
      aria-current={current ? 'step' : undefined}
      data-state={m.state ?? 'upcoming'}
      padding="sm"
      className={cn(
        'flex min-w-0 flex-col gap-3',
        current && 'border-accent ring-1 ring-accent',
        done && 'bg-canvas text-ink-muted',
      )}
    >
      <header className="flex flex-col gap-1">
        <h2 id={headingId} className={cn('text-sm font-semibold', done ? 'text-ink-muted' : 'text-ink')}>
          {milestoneTitle(m)}
        </h2>
        {m.audience && <p className="text-xs text-ink-muted">{m.audience}</p>}
        {m.platforms.length > 0 && (
          <ul aria-label="Platforms" className="flex flex-wrap gap-1">
            {m.platforms.map((p, i) => (
              <li key={`${p.name}-${i}`} className="rounded-pill border border-edge px-2 py-0.5 text-xs text-ink-muted">
                {p.off
                  ? <><s>{p.name}</s>{` (${p.note ?? 'off'})`}</>
                  : p.name}
              </li>
            ))}
          </ul>
        )}
        {current
          ? <StatusNoteEditor slug={slug} milestone={m} />
          : <p className="text-xs font-medium text-ink-muted">{statusLine(m)}</p>}
      </header>

      {done && m.exitOverride && (
        <Banner tone="warning" role="none">Advanced by override: {m.exitOverride.reason}</Banner>
      )}

      {m.gate.length > 0
        ? <ul aria-label="Gate" className="flex flex-col divide-y divide-edge">
            {m.gate.map((c) => <ConditionRow key={c.key} condition={c} manage={done ? undefined : { slug, scope: m.key }} />)}
          </ul>
        : <p className="text-xs text-ink-muted">No gate conditions.</p>}

      {m.leaves.total > 0 && (
        <div className="flex flex-col gap-2">
          <LeavesBar leaves={m.leaves} />
          {open.length > 0 && (
            <ul aria-label="Open leaves" className="flex flex-col gap-1">
              {open.slice(0, OPEN_LEAVES_CAP).map((l) => <LeafRow key={l.ref} leaf={l} />)}
            </ul>
          )}
          <div className="flex flex-wrap gap-x-3 text-xs text-ink-muted">
            {hidden > 0 && (epicUrl
              ? <a href={epicUrl} target="_blank" rel="noreferrer" className="text-accent hover:underline">+{hidden} more</a>
              : <span>+{hidden} more</span>)}
            {m.leaves.done > 0 && <span>{m.leaves.done} done</span>}
          </div>
        </div>
      )}

      {current && (
        <div className="mt-auto flex flex-col gap-2 border-t border-edge pt-3">
          <Button type="button" size="sm" variant={m.gateSummary.ready ? 'primary' : 'secondary'}
            onClick={() => setAdvancing(true)}>
            {m.gateSummary.ready ? 'Advance' : 'Advance with override…'}
          </Button>
          {advancing && <AdvanceDialog open onClose={() => setAdvancing(false)} advance={advance} milestone={m} next={next} />}
        </div>
      )}
    </Card>
  )
}
