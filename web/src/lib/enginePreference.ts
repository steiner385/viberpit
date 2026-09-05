/**
 * The dispatch bar's remembered Agent choice.
 *
 * Same shape as `lib/lastRepo.ts` deliberately — one tiny module per persisted
 * preference, every access wrapped so a blocked/unavailable localStorage
 * (private mode, quota, SSR) degrades to "no remembered choice" instead of
 * crashing the dock.
 *
 * `getPreferredEngine()` returns null, NOT 'claude_code', until the user has
 * explicitly picked one. The launch UI must default to Claude Code for upgrade
 * safety, and a stored default this module invented would be indistinguishable
 * from a real choice — which also matters because a persisted engine the server
 * NO LONGER offers must never be selected (the caller checks the offer list;
 * this module only remembers).
 */
import { isAgentEngine, type AgentEngine } from '@/api/types'

export const ENGINE_PREFERENCE_KEY = 'ccswitch.engine'

/** The user's explicit Agent choice, or null when they have never made one
 *  (or stored something this build no longer recognises). */
export function getPreferredEngine(): AgentEngine | null {
  try {
    const raw = window.localStorage.getItem(ENGINE_PREFERENCE_KEY)
    return isAgentEngine(raw) ? raw : null
  } catch {
    return null
  }
}

export function setPreferredEngine(engine: AgentEngine): void {
  try {
    window.localStorage.setItem(ENGINE_PREFERENCE_KEY, engine)
  } catch {
    /* ignore — persistence is best-effort */
  }
}
