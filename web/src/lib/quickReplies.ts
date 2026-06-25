// Hard-coded one-tap replies offered in the sparkle (✨) dropdown on every
// session, independent of the backend's dynamic context suggestions
// (`session.latestSuggestions`). The sparkle's primary click sends the smart
// suggestion (or "continue"); this list is the manual quick-reply fallback,
// always available so the dropdown is useful even when the backend has no
// suggestions.
//
// SINGLE SOURCE OF TRUTH — imported by both SessionCard and the in-session
// Composer. Do not inline a second copy in either component (the two resume
// affordances are already parallel implementations; a duplicated list is how
// they would silently drift). Edit the list here and both stay in sync.
export const QUICK_REPLIES = ['continue', 'approved', 'proceed', 'try again'] as const

export type QuickReply = (typeof QUICK_REPLIES)[number]
