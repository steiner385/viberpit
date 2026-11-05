// The one place the milestones view turns payload fields into words, so the
// page's header strip and the product Overview's summary line cannot drift.
import type {
  ConditionOp, ConditionStatus, ConditionValue, GateSummary, Milestone, MilestoneCondition, MilestoneLeaves,
  MilestonesView,
} from '@/api/milestones'
import type { Tone } from '@/ui/Badge'

/** Mirrors `server/automations/milestones_refresh.py:AUTOMATION_ID`. */
export const MILESTONES_REFRESH_ID = 'milestones-refresh'
/** The definition file a product adds to get a ladder; mirrors `server/pm/milestone_def.py:MILESTONES_PATH`. */
export const MILESTONES_FILE = 'docs/product/milestones.yaml'

/** Glyph + text, never colour alone. The tone only tints the glyph. */
export const STATUS_META: Record<ConditionStatus, { glyph: string; text: string; tone: Tone }> = {
  met: { glyph: '✔', text: 'Met', tone: 'success' },
  unmet: { glyph: '✖', text: 'Not met', tone: 'danger' },
  unknown: { glyph: '?', text: 'Unknown', tone: 'warning' },
  info: { glyph: '·', text: 'Measured', tone: 'neutral' },
}

const OP_SYMBOL: Record<ConditionOp, string> = { eq: '=', ne: '≠', lt: '<', le: '≤', gt: '>', ge: '≥' }

const num = (v: ConditionValue | null, k: string): number | null => {
  const x = v ? (v as Record<string, unknown>)[k] : undefined
  return typeof x === 'number' ? x : null
}

/** "293 → 0" for an eq target, "2 of ≥3" otherwise, "8 of 9 closed" for refs,
 *  a bare count with no target; null when there is nothing to show. */
export function conditionValue(c: Pick<MilestoneCondition, 'value' | 'op' | 'target'>, closedWord = 'closed'): string | null {
  const done = num(c.value, 'done')
  const total = num(c.value, 'total')
  if (done !== null && total !== null) return closedWord ? `${done} of ${total} ${closedWord}` : `${done} of ${total}`
  const n = num(c.value, 'n')
  if (n === null) return null
  if (c.op && c.target !== null) {
    return c.op === 'eq' ? `${n} → ${c.target}` : `${n} of ${OP_SYMBOL[c.op]}${c.target}`
  }
  return String(n)
}

/** "1 of 4 gate conditions met · 1 unknown". */
export function gateLine(s: GateSummary): string {
  const base = `${s.met} of ${s.total} gate condition${s.total === 1 ? '' : 's'} met`
  return s.unknown > 0 ? `${base} · ${s.unknown} unknown` : base
}

/** "17 of 18 done · 1 in flight", plus "· N not started" when there are any. */
export function leavesLine(l: MilestoneLeaves): string {
  const parts = [`${l.done} of ${l.total} done`, `${l.inFlight} in flight`]
  if (l.notStarted > 0) parts.push(`${l.notStarted} not started`)
  return parts.join(' · ')
}

/** "Phase 0 · Household gate" (whichever of the two the file gave). */
export function milestoneTitle(m: Pick<Milestone, 'key' | 'label' | 'name'>): string {
  return [m.label, m.name].filter(Boolean).join(' · ') || m.key
}

const day = (iso: string | null) =>
  iso ? new Date(iso).toLocaleDateString([], { month: 'short', day: 'numeric', year: 'numeric' }) : '—'

/** A card's status line: "Current · <status note>", "Done · entered … exited …", "Upcoming". */
export function statusLine(m: Pick<Milestone, 'state' | 'statusNote' | 'enteredAt' | 'exitedAt'>): string {
  if (m.state === 'current') return m.statusNote ? `Current · ${m.statusNote}` : 'Current'
  if (m.state === 'done') return `Done · entered ${day(m.enteredAt)} · exited ${day(m.exitedAt)}`
  return 'Upcoming'
}

export type LoadedView = Extract<MilestonesView, { milestones: Milestone[] }>

/** The loaded ladder, or null for "no ladder" and for anything else the read
 *  answered with (the e2e catch-all hands unknown `/pm/projects/*` reads a
 *  project object). */
export function loadedView(v: MilestonesView | undefined): LoadedView | null {
  return v && v.ladder && Array.isArray((v as LoadedView).milestones) ? (v as LoadedView) : null
}

export function currentMilestone(v: LoadedView): Milestone | null {
  return v.milestones.find((m) => m.key === v.ladder.currentKey) ?? null
}
