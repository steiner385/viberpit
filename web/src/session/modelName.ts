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
const TIERS: Record<string, string> = { opus: 'Opus', sonnet: 'Sonnet', haiku: 'Haiku', fable: 'Fable' }

export function modelName(id: string | null | undefined): string {
  if (!id) return ''
  // <min> is 1–2 digits and must end a segment, so a date suffix (-20260101) or a
  // context tag ([1m]) is never mistaken for it.
  const m = /^claude-(opus|sonnet|haiku|fable)-(\d+)(?:-(\d{1,2}))?(?:-|\[|$)/.exec(id)
  if (!m) return id
  return m[3] ? `${TIERS[m[1]]} ${m[2]}.${m[3]}` : `${TIERS[m[1]]} ${m[2]}`
}
