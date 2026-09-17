/**
 * The Track tab's outlook card: what happens to this initiative with NO human
 * action — who acts (the machine, you, nobody), when, the steps, and the
 * tolerance / kill-switch reason. Replaces the one-line `nextActionLabel`.
 */
import type { InitiativeGuidance } from '@/api/sdlc'
import { Badge, type Tone } from '@/ui/Badge'
import { StepRow, whenLabel } from './ConsequenceList'

const ACTOR: Record<string, { label: string; tone: Tone }> = {
  machine: { label: 'Automatic', tone: 'info' },
  you: { label: 'Your move', tone: 'warning' },
  nobody: { label: 'Nothing to do', tone: 'neutral' },
}

export function WhatHappensNext({ guidance }: { guidance: InitiativeGuidance }) {
  const m = guidance.machine
  if (!m) return null
  const actor = ACTOR[m.actor] ?? ACTOR.nobody!
  const later = whenLabel(m.when, guidance.tickSeconds)
  return (
    <section aria-labelledby="what-next-heading" className="flex flex-col gap-2 rounded-md border border-edge bg-surface p-3">
      <div className="flex flex-wrap items-center gap-2">
        <h2 id="what-next-heading" className="text-sm font-semibold text-ink">What happens next</h2>
        <Badge tone={actor.tone}>{actor.label}</Badge>
        {later && <span className="text-xs text-ink-muted">{later}</span>}
      </div>
      <ul className="flex flex-col gap-1 text-sm text-ink">
        {m.steps.map((s, i) => <StepRow key={i} step={s} />)}
      </ul>
      <p className="text-xs text-ink-muted">{m.because}</p>
    </section>
  )
}
