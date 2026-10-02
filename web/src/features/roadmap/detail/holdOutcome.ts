// Pure copy helpers behind the hold taps — shared by the Roadmap's held card and
// the Review-and-decide dialog, kept out of the component files so fast refresh
// keeps working (react-refresh/only-export-components).
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
