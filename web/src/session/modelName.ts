// Friendly display name for a Claude model id. Parses the `claude-<tier>-<maj>[-<min>]`
// shape (claude-opus-4-8 → "Opus 4.8"; the Claude 5 family drops the minor:
// claude-fable-5 → "Fable 5"); anything that doesn't match is returned verbatim so
// unknown/future ids still render legibly.
//
// Note: this is deliberately separate from the model-picker's `{id,label}` list
// (server-provided short aliases like `opus` → "Opus"). That list is the set of
// *selectable* models; this formatter renders the *full* model id that
// AssistantMessage.model carries into a transcript, including historical or
// no-longer-selectable ids the picker never lists.
export const TIERS: Record<string, string> = { opus: 'Opus', sonnet: 'Sonnet', haiku: 'Haiku', fable: 'Fable' }

export function modelName(id: string | null | undefined): string {
  if (!id) return ''
  // <min> is 1–2 digits and must end a segment, so a date suffix (-20260101) or a
  // context tag ([1m]) is never mistaken for it.
  const m = /^claude-(opus|sonnet|haiku|fable)-(\d+)(?:-(\d{1,2}))?(?:-|\[|$)/.exec(id)
  if (!m) return id
  return m[3] ? `${TIERS[m[1]]} ${m[2]}.${m[3]}` : `${TIERS[m[1]]} ${m[2]}`
}

/**
 * Web mirror of the server `model_tier` function.
 * Matches `claude-(opus|sonnet|haiku|fable)-…`, bare aliases in TIERS, and the
 * `opusplan` alias (→ 'opus'). Returns null for unknown/null/undefined ids.
 * Client-side hint only — the server policy is authoritative.
 */
export function modelTier(id: string | null | undefined): keyof typeof TIERS | null {
  if (!id) return null
  // Strip context-size suffix like [1m] before matching
  const base = id.trim().toLowerCase().split('[')[0]
  const m = /^claude-(opus|sonnet|haiku|fable)-/.exec(base)
  if (m) return m[1] as keyof typeof TIERS
  if (base in TIERS) return base as keyof typeof TIERS
  if (base === 'opusplan') return 'opus'
  return null
}

/**
 * Display label for a model alias or pinned id: the full "Opus 5" form when the id
 * matches, else the tier's capitalised name ("fable" -> "Fable"), else verbatim.
 * Single source of truth for model labels in the UI.
 */
export function tierLabel(id: string | null | undefined): string {
  if (!id) return 'this model'
  const full = modelName(id)
  if (full && full !== id) return full
  return TIERS[id.toLowerCase()] ?? id
}

/**
 * Returns true when the model id belongs to the default gated tier (fable).
 * Client-side hint only; the server policy is authoritative.
 */
export function isGatedTier(id: string | null | undefined): boolean {
  return modelTier(id) === 'fable'
}
