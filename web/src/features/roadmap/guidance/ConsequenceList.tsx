/**
 * THE renderer for a server-computed consequence (spec 2026-09-17 §5): what a
 * tap changes now, what the loop does afterwards and when, the two cost badges
 * (a session starts / GitHub is written) and the reverse action. Every dialog and
 * the inbox pane render through this; nothing else formats a consequence.
 */
import type { Consequence, GuidanceStep, GuidanceWhen } from '@/api/sdlc'
import { Badge } from '@/ui/Badge'

/** "in about a minute" is derived from the tick the server reports, never a
 *  literal (spec D6). Without a tick (the inbox: the Backlog Runner's cadence is
 *  not the synthesizer's), say only that it is the next run. */
// eslint-disable-next-line react-refresh/only-export-components -- shared helper, not a component; WhatHappensNext.tsx imports it
export function whenLabel(when: GuidanceWhen, tickSeconds?: number): string | null {
  if (when !== 'next_tick') return null
  if (tickSeconds === undefined) return 'on the next automation run'
  if (tickSeconds < 90) return 'in about a minute'
  return `in about ${Math.round(tickSeconds / 60)} minutes`
}

export function StepRow({ step }: { step: GuidanceStep }) {
  return (
    <li className="flex flex-wrap items-center gap-1.5">
      <span>{step.text}</span>
      {step.spawnsSession && <Badge tone="warning">starts a session</Badge>}
      {step.writesGithub && <Badge tone="info">writes to GitHub</Badge>}
    </li>
  )
}

function Steps({ label, steps }: { label: string; steps: GuidanceStep[] }) {
  if (steps.length === 0) return null
  return (
    <div className="flex flex-col gap-1">
      <p className="text-xs font-medium uppercase text-ink-muted">{label}</p>
      <ul className="flex flex-col gap-1">
        {steps.map((s, i) => <StepRow key={i} step={s} />)}
      </ul>
    </div>
  )
}

export function ConsequenceList({ consequence, tickSeconds, heading }: {
  consequence: Consequence
  tickSeconds?: number
  heading?: string
}) {
  const later = whenLabel(consequence.when, tickSeconds)
  return (
    <div className="flex flex-col gap-2 text-sm text-ink">
      {heading && <p className="font-medium">{heading}</p>}
      {!consequence.available && consequence.unavailableReason && (
        <p className="text-ink-muted">{consequence.unavailableReason}</p>
      )}
      <Steps label="Now" steps={consequence.immediate} />
      <Steps label={later ? `Then · ${later}` : 'Then'} steps={consequence.then} />
      {consequence.reversal && <p className="text-xs text-ink-muted">Reverse: {consequence.reversal}</p>}
    </div>
  )
}
