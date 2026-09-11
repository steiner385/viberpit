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
  // One section, so one set: the Automations and Insights chips merged when
  // finish-line row 4 merged those sections into Delivery's two scopes. Every
  // string was already backed by an eval case and stays backed by the same one
  // (`starters.coverage.test.ts`).
  delivery: [
    'What did autopilot do in the last hour?',
    'Which automations are failing?',
    'What is the PR doctor proposing?',
    'What regressed in CI recently?',
    'Which repo has the slowest checks?',
  ],
  // One section, so one set: the Products and Initiatives chips merged when
  // Roadmap Phase 3 merged the sections, and the Triage chips joined them when
  // finish-line row 3 made the queue this section's Inbox scope. (Triage's
  // second chip was already this set's first, so only one string moved.) Every
  // string here must also be a prompt in the assistant eval corpus
  // (`starters.coverage.test.ts`).
  roadmap: [
    'Which project has the most open work?',
    'Which initiatives have no active session?',
    'Summarize initiative progress',
    'What is blocked by a dependency?',
    "What's at the top of the priority queue?",
    "What's waiting in the triage inbox?",
  ],
  settings: [
    'Which accounts are near their weekly limit?',
    'How many hosts are online?',
    'Notify me if any account gets limited',
  ],
}

/** Shown instead of a route's set whenever a session is open — the assistant's most
 *  useful starting point is then that session, not the section it lives in. */
const SESSION_OPEN: readonly string[] = [
  'Why is this session stuck?',
  'Draft the next prompt for this session',
  'Summarize this session so far',
  'Tell me when this session finishes',
]

/** The starter chips for the current view: session-specific when a session is open,
 *  otherwise the route's section (falling back to `sessions` for an unmapped route —
 *  e.g. `/` before a redirect settles). */
export function startersFor(route: string, openSessionId: string | null): readonly string[] {
  if (openSessionId) return SESSION_OPEN
  const def = navDefForPath(route)
  return def ? STARTERS[def.id] : STARTERS.sessions
}
