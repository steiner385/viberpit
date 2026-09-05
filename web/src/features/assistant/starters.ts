// Starter chips shown above the AI assistant's empty composer — one set per nav
// section (so the suggestion always matches whatever the user is looking at) plus a
// session-specific set that takes over once a session is open. `Record<NavId, …>` is
// exhaustive over `@/shell/navDefs`'s one nav map, so a new section fails to compile
// here until it has starters — the same drift guard the nav map itself documents.
import type { NavId } from '@/shell/navDefs'
import { navDefForPath } from '@/shell/navDefs'

export const STARTERS: Record<NavId, readonly string[]> = {
  sessions: [
    'What needs my attention?',
    'Summarize what every session is doing',
    'Which sessions are limited or queued?',
  ],
  pipeline: [
    'Which PRs are blocked, and why?',
    'Which checks are flakiest this week?',
  ],
  automations: [
    'What did autopilot do in the last hour?',
    'Which automations are failing?',
  ],
  // INTERIM (audit gap G1). Initiatives and projects had chips no tool could serve —
  // the assistant has no SDLC-initiative or PM-product tools yet, so tapping one bought
  // an apology. These stand in until plan Task 6.5 restores section-specific chips on
  // top of Pack P; the conformance tests (server + web) hold the line that every chip
  // is backed by a tool and an eval case.
  initiatives: [
    'What needs my attention?',
    'Which PRs are blocked, and why?',
  ],
  projects: [
    'What needs my attention?',
    'Which sessions are limited or queued?',
  ],
  archived: [
    'Which archived sessions never opened a PR?',
    'What did I archive today?',
  ],
  insights: [
    'What regressed in CI recently?',
    'Which repo has the slowest checks?',
  ],
  settings: [
    'Which accounts are near their weekly limit?',
    'How many hosts are online?',
  ],
}

/** Shown instead of a route's set whenever a session is open — the assistant's most
 *  useful starting point is then that session, not the section it lives in. */
const SESSION_OPEN: readonly string[] = [
  'Why is this session stuck?',
  'Draft the next prompt for this session',
  'Summarize this session so far',
]

/** The starter chips for the current view: session-specific when a session is open,
 *  otherwise the route's section (falling back to `sessions` for an unmapped route —
 *  e.g. `/` before a redirect settles). */
export function startersFor(route: string, openSessionId: string | null): readonly string[] {
  if (openSessionId) return SESSION_OPEN
  const def = navDefForPath(route)
  return def ? STARTERS[def.id] : STARTERS.sessions
}
