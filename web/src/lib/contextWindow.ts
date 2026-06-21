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
