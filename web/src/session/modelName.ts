// Friendly display name for a Claude model id. Parses the `claude-<tier>-<maj>-<min>`
// shape (e.g. claude-opus-4-8 → "Opus 4.8"); anything that doesn't match is returned
// verbatim so unknown/future ids still render legibly.
//
// Note: this is deliberately separate from the model-picker's `{id,label}` list
// (server-provided short aliases like `opus` → "Opus"). That list is the set of
// *selectable* models; this formatter renders the *full* model id that
// AssistantMessage.model carries into a transcript, including historical or
// no-longer-selectable ids the picker never lists.
const TIERS: Record<string, string> = { opus: 'Opus', sonnet: 'Sonnet', haiku: 'Haiku', fable: 'Fable' }

export function modelName(id: string | null | undefined): string {
  if (!id) return ''
  const m = /^claude-(opus|sonnet|haiku|fable)-(\d+)-(\d+)/.exec(id)
  if (!m) return id
  return `${TIERS[m[1]]} ${m[2]}.${m[3]}`
}
