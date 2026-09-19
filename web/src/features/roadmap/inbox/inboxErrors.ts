/**
 * What to say when one or both of the Inbox's two lists failed to load.
 *
 * The queue is two independent fetches (`/pm/triage`, `/pm/decisions`) rendered
 * as one list, so an error line must name the half that broke: the surface still
 * shows — and still acts on — whatever the other half returned, and "Could not
 * load the inbox" over five perfectly good classification rows is a lie that
 * stops people using them.
 *
 * In a module of its own (not beside `InboxScopeBody`) so the list column and the
 * detail pane share ONE spelling of the copy; a component file may only export
 * components (`react-refresh/only-export-components`).
 */

/** Both halves gone — or a surface that ORs the two into one flag, which is what
 *  the list column does: it interleaves the kinds, so it blames the whole inbox. */
export const INBOX_LOAD_ERROR = 'Could not load the inbox.'

/** `null` = nothing failed. */
export function inboxErrorTitle(triageFailed: boolean, decisionsFailed: boolean): string | null {
  if (triageFailed && decisionsFailed) return INBOX_LOAD_ERROR
  if (triageFailed) return 'Could not load the classification queue.'
  if (decisionsFailed) return 'Could not load the decisions.'
  return null
}
