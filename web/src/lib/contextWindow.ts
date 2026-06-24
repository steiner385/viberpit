/**
 * Context-window math for the session status strip.
 *
 * "Remaining context" is the share of the model's context window still free,
 * approximated by the most recent turn's input footprint (fresh input + cache
 * reads — the tokens that re-enter the window each turn).
 */

const DEFAULT_WINDOW = 200_000
const LONG_WINDOW = 1_000_000

/** Token totals the reducer accumulates from `turnEnd.usage` (see reducer state). */
export interface UsageTotals {
  input: number
  output: number
  cacheRead: number
  costUsd: number
}

/**
 * Context-window size (tokens) for a model id. Models advertising the `[1m]`
 * long-context marker get 1M; everything else (and unknown/null) gets 200k.
 */
export function modelContextWindow(model: string | null): number {
  if (model && model.includes('[1m]')) return LONG_WINDOW
  return DEFAULT_WINDOW
}

/**
 * Percent of the context window still available, 0–100 (rounded), or null when
 * no usage is known yet. Clamped to [0, 100].
 */
export function remainingContextPct(usage: UsageTotals | null, model: string | null): number | null {
  if (usage == null) return null
  const window = modelContextWindow(model)
  const used = usage.input + usage.cacheRead
  const remaining = ((window - used) / window) * 100
  return Math.min(100, Math.max(0, Math.round(remaining)))
}

/**
 * Exact context-window occupancy reported by the SDK (the `/context` data),
 * carried by the `contextUsage` event. `maxTokens` is the EFFECTIVE max (may be
 * trimmed by the autocompact buffer), so remaining hits 0 right as compaction looms.
 */
export interface ContextUsageExact {
  usedTokens: number
  maxTokens: number
}

/**
 * Percent of the context window still available from EXACT usage, 0–100 (rounded,
 * clamped), or null when there is no exact reading or no usable denominator. This is
 * the source of truth for the live ctx% readout; `remainingContextPct` (above) is the
 * per-call approximation used only as a fallback for historic/detached sessions.
 */
export function remainingFromContextUsage(usage: ContextUsageExact | null): number | null {
  if (usage == null || !(usage.maxTokens > 0)) return null
  const remaining = ((usage.maxTokens - usage.usedTokens) / usage.maxTokens) * 100
  return Math.min(100, Math.max(0, Math.round(remaining)))
}

export type ContextSeverity = 'neutral' | 'warning' | 'danger'

/**
 * Severity for a remaining-context percentage (100 = window empty). Drives the
 * color escalation on the session's `ctx %` readout. `null` (no usage yet) is
 * neutral so the readout stays calm until there is real data.
 */
export function contextSeverity(remainingPct: number | null): ContextSeverity {
  if (remainingPct == null) return 'neutral'
  if (remainingPct <= 10) return 'danger'
  if (remainingPct <= 25) return 'warning'
  return 'neutral'
}
