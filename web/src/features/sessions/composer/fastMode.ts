/**
 * Fast mode (D-14): the one copy of its cost sentence and of the "first use" note's
 * persisted flag. The sentence is the ⚡ toggle's tooltip AND the inline note the first
 * time fast mode is on in this browser — owner decision 2026-11-04: a toggle, with a
 * cost note, because it bills usage credits at a higher rate outside the plan.
 */
import { storageKey } from '@/lib/storageKey'

export const FAST_MODE_COPY =
  'Fast mode — faster output on the same model. Bills usage credits at a higher rate, outside your plan.'

/** What the CLI is actually doing when fast mode is on but not running (#3254): the ⚡
 *  tooltip's first line. Null = nothing to add (agrees, off, or not yet reported). */
export function fastModeLiveNote(
  on: boolean,
  liveState: string | null | undefined,
  disabledReason: string | null | undefined,
): string | null {
  if (!on) return null
  if (liveState === 'cooldown') return 'Cooling down — standard speed for ~10 min.'
  if (liveState !== 'off') return null
  return `Disabled by the CLI${disabledReason ? `: ${disabledReason.replace(/_/g, ' ')}` : ''}.`
}

/** Set once the note has been shown in this browser. */
export const FAST_NOTE_SEEN_KEY = storageKey('fastMode.noteSeen')

export function hasSeenFastNote(): boolean {
  try {
    return window.localStorage.getItem(FAST_NOTE_SEEN_KEY) === '1'
  } catch {
    // Blocked storage: never nag on every render — treat the note as seen.
    return true
  }
}

export function markFastNoteSeen(): void {
  try {
    window.localStorage.setItem(FAST_NOTE_SEEN_KEY, '1')
  } catch {
    /* best-effort, like every other preference */
  }
}
