import { EmptyState as Empty } from '@/ui/EmptyState'

/**
 * The Sessions list's zero-state.
 *
 * It carried a large "New Session" `Button` until the create control moved to
 * the foot of `SessionListColumn`. The two then sat a few pixels apart — one
 * centred in the scroll band, one pinned directly below it — both firing the
 * same `onNew`. Two buttons for one action in one column is the duplication the
 * foot button was introduced to end, so this is copy now and the foot button is
 * the single affordance. That is why there is no `onNew` prop here, and why
 * `SessionList` no longer threads one through.
 *
 * Deliberately title-only — no `description` either. With no sessions there is
 * none open, so the dispatch bar fills the bottom of the pane (the foot button
 * only renders while a session is open), and a sentence naming it would caption
 * a control already in the reader's eyeline.
 */
export function EmptyState() {
  return <Empty title="No sessions yet" />
}
