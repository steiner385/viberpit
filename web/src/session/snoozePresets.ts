// web/src/session/snoozePresets.ts
//
// The card menu's Snooze choices, as pure data. Times are computed in the
// VIEWER'S local time ("tomorrow 9am" means the user's morning) and sent as ISO
// UTC, which is what `POST /sessions/{id}/snooze` takes. `until: null` is the
// indefinite snooze — only a human may set it (the session tool must name a time).

import { formatResetTime } from '@/lib/formatReset'

/** A snooze's wake time as list/banner copy: "until Fri Nov 7, 9:00 AM", or
 *  "until you wake it" for an indefinite snooze. */
export function snoozeUntilLabel(until: string | null | undefined): string {
  const when = formatResetTime(until)
  return when ? `until ${when}` : 'until you wake it'
}

export interface SnoozePreset {
  /** Stable key, for test ids and React keys. */
  id: '1h' | 'tomorrow' | 'monday' | 'manual'
  /** Menu label. */
  label: string
  /** ISO UTC wake instant; null = until someone wakes it. */
  until: string | null
}

const MORNING_HOUR = 9

/** `now` + `days` calendar days, at 09:00 local. the Date constructor's day overflow handles
 *  month/year ends, and building from local fields keeps DST days honest. */
function morningAfter(now: Date, days: number): Date {
  return new Date(now.getFullYear(), now.getMonth(), now.getDate() + days, MORNING_HOUR, 0, 0, 0)
}

/** Days from `now` to the NEXT Monday — never today, so on a Monday it is a week. */
function daysToNextMonday(now: Date): number {
  const dow = now.getDay() // 0 = Sunday … 1 = Monday
  const ahead = (1 - dow + 7) % 7
  return ahead === 0 ? 7 : ahead
}

/** The four presets, in menu order, relative to `now`. */
export function snoozePresets(now: Date = new Date()): SnoozePreset[] {
  return [
    { id: '1h', label: 'Snooze 1 hour', until: new Date(now.getTime() + 60 * 60 * 1000).toISOString() },
    { id: 'tomorrow', label: 'Snooze until tomorrow 9am', until: morningAfter(now, 1).toISOString() },
    { id: 'monday', label: 'Snooze until Monday 9am', until: morningAfter(now, daysToNextMonday(now)).toISOString() },
    { id: 'manual', label: 'Snooze until I wake it', until: null },
  ]
}
