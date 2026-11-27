/**
 * Wording for a hold a daemon restart orphaned (spec 002 FR-406, #947): the kind's
 * name, its content as plain text, and the "Ask again" prompt. Pure — shared by
 * `LostHoldBanner` (the strip and the prompt it sends) and `SessionTimeline` (the
 * `hold_lost` line); both consumers are lazy, so this never enters the initial graph.
 * Content is untrusted text, never markup, and every line is capped.
 */
import type { LostHold } from '@/api/types'

const KIND_LABEL: Record<string, string> = {
  permission: 'permission request',
  question: 'question',
  plan: 'plan approval',
}

/** The human name of a hold kind; a missing or non-string kind is just "hold". */
export function holdKindLabel(kind: unknown): string {
  if (typeof kind !== 'string' || !kind.trim()) return 'hold'
  return KIND_LABEL[kind] ?? kind.replace(/_/g, ' ')
}

const PLAN_PREVIEW_CHARS = 200
/** Any other rendered or prompted line (command, path, question, item, label). */
const LINE_CHARS = 300

const str = (v: unknown): string | null => (typeof v === 'string' && v.trim() ? v : null)
const rec = (v: unknown): Record<string, unknown> =>
  (v && typeof v === 'object' && !Array.isArray(v)) ? (v as Record<string, unknown>) : {}
const clip = (s: string, max = LINE_CHARS): string => (s.length > max ? `${s.slice(0, max)}…` : s)

/** One line for a tool's input: Bash → command, file tools → path, else the first
 *  string field. Nothing when the input has no string to show. */
function inputLine(input: Record<string, unknown>): string | null {
  const direct = str(input.command) ?? str(input.file_path) ?? str(input.path)
  if (direct) return direct
  for (const v of Object.values(input)) {
    const s = str(v)
    if (s) return s
  }
  return null
}

/** The lines of a hold's content, one per thing the agent was waiting on. A question
 *  hold lists each question with its option LABELS only (never descriptions), marked
 *  as options so a lone label cannot read as a chosen answer. */
export function lostHoldLines(hold: LostHold): string[] {
  const c = rec(hold.content)
  if (hold.kind === 'question') {
    const qs = Array.isArray(c.questions) ? c.questions : []
    return qs.flatMap((q) => {
      const r = rec(q)
      const header = str(r.header)
      const question = str(r.question)
      if (!header && !question) return []
      const labels = (Array.isArray(r.options) ? r.options : [])
        .map((o) => str(rec(o).label))
        .filter((l): l is string => l !== null)
        .map((l) => clip(l))
      const text = clip(header && question ? `${header}: ${question}` : (header ?? question)!)
      return [labels.length ? `${text} (options: ${labels.join(' / ')})` : text]
    })
  }
  const input = rec(c.input)
  if (hold.kind === 'plan') {
    const plan = str(input.plan)
    return plan ? [clip(plan.trim(), PLAN_PREVIEW_CHARS)] : []
  }
  // permission (and any kind that carries a tool call)
  const tool = str(c.tool)
  const items = Array.isArray(c.items) ? c.items.map(str).filter((s): s is string => s !== null) : []
  const what = items.length ? clip(items.map((i) => clip(i)).join(', ')) : inputLine(input)
  if (tool && what) return [`${clip(tool)}: ${clip(what)}`]
  if (tool) return [clip(tool)]
  return what ? [clip(what)] : []
}

/** The hold's content as plain text — the banner body and what "Ask again" quotes. */
export function summarizeLostHold(hold: LostHold): string {
  return lostHoldLines(hold).join('\n')
}

/** "{kind label}", plus " (truncated)" when the server cut the content down. */
export function lostHoldHeading(hold: LostHold): string {
  const truncated = rec(hold.content).truncated === true
  return `${holdKindLabel(hold.kind)}${truncated ? ' (truncated)' : ''}`
}

/**
 * The recovery prompt. It names the kind and quotes the content, and it says the
 * hold was never seen — it does NOT answer it (FR-306: the system never decides a
 * hold on the user's behalf). The agent re-raises the hold if it is still relevant.
 */
export function askAgainPrompt(hold: LostHold): string {
  const summary = summarizeLostHold(hold)
  return [
    `When the daemon restarted you were waiting on a ${lostHoldHeading(hold)} that I never saw — `
    + 'nothing was approved or answered:',
    ...(summary ? [summary] : []),
    'If it is still relevant, raise it again; otherwise tell me where things stand.',
  ].join('\n')
}
