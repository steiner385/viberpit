import type { AccountUsage, UsageWindow } from '@/api/types'

/**
 * Reading an account's quota windows — the single source of truth for it.
 *
 * The wire shape is a LIST because a provider reports the allowances it actually
 * meters (Anthropic: session + weekly; Copilot: one premium-request window).
 * Every surface that used to reach for `usage.session` / `usage.weekly` reads
 * through here instead, so none of them can quietly reintroduce a fixed pair of
 * dimensions or render a single-allowance provider's number twice.
 */

/** Windows to render, in provider order. Empty when the read failed. */
export function usageWindows(usage: AccountUsage | null | undefined): UsageWindow[] {
  if (!usage || usage.availability !== 'available') return []
  return usage.windows ?? []
}

/** True when the snapshot carries trustworthy windows. */
export function usageKnown(usage: AccountUsage | null | undefined): boolean {
  return usageWindows(usage).length > 0
}

/** One window by key ('session' | 'weekly' | 'premium' | …), or null. */
export function windowByKey(
  usage: AccountUsage | null | undefined,
  key: string,
): UsageWindow | null {
  return usageWindows(usage).find((w) => w.key === key) ?? null
}

/**
 * The window that runs out FIRST — the most-used one. Mirrors the server's
 * `QuotaSnapshot.binding_window`, including its tie rule: on equal usage the
 * EARLIER window in provider order wins (`>` , not `>=`).
 */
export function bindingWindow(usage: AccountUsage | null | undefined): UsageWindow | null {
  const windows = usageWindows(usage)
  if (windows.length === 0) return null
  return windows.reduce((best, w) => (w.pct > best.pct ? w : best), windows[0])
}

/**
 * The window a per-account RESERVE is measured against: the longest one
 * (Anthropic's weekly), or the only one for a single-allowance provider.
 * Mirrors `QuotaSnapshot.reserve_window`.
 */
export function reserveWindow(usage: AccountUsage | null | undefined): UsageWindow | null {
  const windows = usageWindows(usage)
  if (windows.length === 0) return null
  if (windows.length === 1) return windows[0]
  const timed = windows.filter((w) => w.durationSeconds != null)
  if (timed.length === 0) return null
  return timed.reduce((best, w) => ((w.durationSeconds ?? 0) > (best.durationSeconds ?? 0) ? w : best), timed[0])
}
