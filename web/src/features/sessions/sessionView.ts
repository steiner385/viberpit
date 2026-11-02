import { useSearchParamState } from '@/lib/useSearchParamState'

/**
 * The session's views (`docs/PAGE-TABS.md` §2: a Workspace view toggle, stored
 * in `?view=` rather than `?tab=` because it is not the page's partition by
 * question). Spec 2026-11-01 §2 orders them Conversation | Changes | … | Browser.
 *
 * One owner for the list and the param, because two components drive it:
 * `SessionBody`'s tab strip and the header's `+N −M` chip, which opens Changes.
 * Which views are actually LISTED is `SessionBody`'s call (Changes needs a
 * non-empty diff, Browser the X display, Details a pane under 64rem — wider,
 * its cards are in the rail); a view that is not listed falls back to the
 * conversation. Details is last (D-6).
 */
export const SESSION_VIEWS = ['conversation', 'changes', 'browser', 'details'] as const
export type SessionView = (typeof SESSION_VIEWS)[number]

export function useSessionView(): [SessionView, (v: SessionView) => void] {
  return useSearchParamState<SessionView>('view', SESSION_VIEWS, 'conversation')
}
