// Pure copy helpers behind the hold taps — shared by the Roadmap's held card and
// the Review-and-decide dialog, kept out of the component files so fast refresh
// keeps working (react-refresh/only-export-components).
import { HOLD_LABEL } from '@/features/products/vocab'
import { tierLabel } from '@/session/modelName'

/** "held 3 h" / "held 12 days": coarse on purpose — the question is "how long
 *  has nothing happened", not a timestamp (`holdSetAt` is on the dialog). */
export function heldFor(hours: number | null): string | null {
  if (hours == null) return null
  if (hours < 1) return 'held under an hour'
  if (hours < 48) return `held ${Math.round(hours)} h`
  return `held ${Math.round(hours / 24)} days`
}

/** What to tell the person after an approve-escalation tap when the hold was
 *  released server-side but no session is running at the approved tier — the
 *  dispatch refused, or the gate parked it and the approval step did not land.
 *  Null when a session is simply running (the "Open session" link says so). */
export function dispatchOutcomeText(result: { status: string; approval?: string }, model?: string): string | null {
  if (result.status !== 'spawned') {
    return `Dispatch failed: ${result.status}. The hold was released; the next run uses the product's default model.`
  }
  if (result.approval === 'pending') {
    return `The session is parked for approval at ${model ? tierLabel(model) : 'that model'} — approve it on the Sessions page.`
  }
  return null
}

/** What a held card asks of a person (server `attentionKind`,
 *  `server/pm/escalation.py:attention_kind`). The two hold kinds keep their hold
 *  label; an approval-gated climb and an assessed checklist get their own. */
export const ATTENTION_LABEL: Record<string, string> = {
  model_approval: 'Model approval',
  checklist: 'Checklist for you',
  decision: HOLD_LABEL.decision,
  owner_action: HOLD_LABEL.owner_action,
}

export const attentionLabel = (kind: string | null | undefined): string =>
  kind ? (ATTENTION_LABEL[kind] ?? kind) : ''

/** The items of a checklist hold reason (`server/pm/closeout_assess.py:
 *  checklist_reason`: a `Left for you:` head, one `- [ ] item` line each, then an
 *  optional `(+N more)`), plus that remainder line. */
export function checklistItems(reason: string | null | undefined): { items: string[]; more: string | null } {
  const lines = (reason ?? '').split('\n').map((l) => l.trim())
  const items = lines.filter((l) => l.startsWith('- [ ] ')).map((l) => l.slice('- [ ] '.length))
  const more = lines.find((l) => /^\(\+\d+ more\)$/.test(l)) ?? null
  return { items, more }
}
