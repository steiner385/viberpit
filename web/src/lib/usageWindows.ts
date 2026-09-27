import type { AccountUsage, UsageWindow } from '@/api/types'
import { modelTier } from '@/session/modelName'

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

/** Wording for the server's `quota.REASON_*` codes (src/viberpit/quota.py). */
const REASON_WORDING: Record<string, string> = {
  setup_token_scope:
    'Setup-token lacks the user:profile scope — re-authenticate to restore usage reporting',
}

/**
 * Why there are no windows to render, or null when there are.
 *
 * Lives here rather than at a call site because "no windows" has several very
 * different causes and a blank badge cannot tell them apart — which is exactly
 * how a permanently unreadable account (setup-token credential, no `user:profile`
 * scope) looked identical to one whose poll had simply not landed yet.
 */
export function usageUnavailableReason(usage: AccountUsage | null | undefined): string | null {
  if (usageWindows(usage).length > 0) return null
  if (!usage) return 'Usage not read yet'
  // A specific reason from the server beats the availability wording: the same
  // 'unsupported' covers "no quota API" (nothing to do) and "setup-token minted
  // without user:profile" (re-authenticate). Unknown codes fall through.
  const specific = usage.reason ? REASON_WORDING[usage.reason] : undefined
  if (specific) return specific
  switch (usage.availability) {
    case 'unsupported':
      return 'Quota not reported for this credential'
    case 'auth_error':
      return 'Credentials rejected — re-authenticate'
    default:
      return 'Usage unavailable'
  }
}

/** One window by key ('session' | 'weekly' | 'premium' | …), or null. */
export function windowByKey(
  usage: AccountUsage | null | undefined,
  key: string,
): UsageWindow | null {
  return usageWindows(usage).find((w) => w.key === key) ?? null
}

/**
 * The windows that meter `model` — mirrors `QuotaSnapshot.windows_for`. A window
 * scoped to specific models applies when the session's TIER is listed; a model
 * with no fixed tier (null, 'default', 'best', unknown id) matches every window,
 * the conservative rule the server uses for the same reason.
 */
export function windowsFor(usage: AccountUsage | null | undefined, model: string | null | undefined): UsageWindow[] {
  const tier = modelTier(model)
  return usageWindows(usage).filter((w) => {
    if (w.scope !== 'model' || !w.appliesToModels || w.appliesToModels.length === 0) return true
    if (tier == null) return true
    return w.appliesToModels.includes(tier)
  })
}

/**
 * The window that runs out FIRST — the most-used one. Mirrors the server's
 * `QuotaSnapshot.binding_window`, including its tie rule: on equal usage the
 * EARLIER window in provider order wins (`>` , not `>=`). `model` narrows to
 * the windows that actually meter it (see `windowsFor`); omitted, every
 * window is a candidate.
 */
export function bindingWindow(usage: AccountUsage | null | undefined, model?: string | null): UsageWindow | null {
  const windows = windowsFor(usage, model)
  if (windows.length === 0) return null
  return windows.reduce((best, w) => (w.pct > best.pct ? w : best), windows[0])
}

/**
 * The window a per-account RESERVE is measured against: the longest one
 * (Anthropic's weekly), or the only one for a single-allowance provider.
 * Mirrors `QuotaSnapshot.reserve_window`. A model-scoped window (e.g. a
 * per-tier weekly) never stands in for the account-wide reserve; only when
 * every reported window is model-scoped does the reserve fall back to them.
 */
export function reserveWindow(usage: AccountUsage | null | undefined): UsageWindow | null {
  const all = usageWindows(usage)
  if (all.length === 0) return null
  const windows = all.filter((w) => w.scope !== 'model')
  const pool = windows.length > 0 ? windows : all
  if (pool.length === 1) return pool[0]
  const timed = pool.filter((w) => w.durationSeconds != null)
  if (timed.length === 0) return null
  return timed.reduce((best, w) => ((w.durationSeconds ?? 0) > (best.durationSeconds ?? 0) ? w : best), timed[0])
}

/** Compact labels for account-wide windows; a model window keeps the provider's
 *  label ("Fable weekly") because the model IS the information. */
const ACCOUNT_ABBR: Record<string, string> = { session: 'sess', weekly: 'wk', premium: 'prem' }

/** Display label for a window: the provider's own label for a model-scoped
 *  window, else the tight abbreviation the status strip needs, falling back
 *  to the provider's label for anything unabbreviated. Single source for
 *  every surface that used to keep its own key→abbreviation map. */
export function windowLabel(w: UsageWindow): string {
  if (w.scope === 'model') return w.label
  return ACCOUNT_ABBR[w.key] ?? w.label
}
