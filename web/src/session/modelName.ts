// Friendly display names for the model ids the app renders, for EVERY engine.
//
// Claude ids parse the `claude-<tier>-<maj>[-<min>]` shape (claude-opus-4-8 →
// "Opus 4.8"; the Claude 5 family drops the minor: claude-fable-5 → "Fable 5").
// Codex/OpenAI ids parse the `gpt-<version>[-<word>…]` shape the app-server's
// model catalog serves (gpt-5.1-codex → "GPT-5.1 Codex", which is the label
// `server/codex/catalog.py` itself produces). Anything that matches neither is
// returned verbatim so unknown/future ids still render legibly.
//
// Note: this is deliberately separate from the model-picker's `{id,label}` list
// (server-provided short aliases like `opus` → "Opus"). That list is the set of
// *selectable* models; this formatter renders the *full* model id that
// AssistantMessage.model carries into a transcript, including historical or
// no-longer-selectable ids the picker never lists.
import type { AgentEngine } from '@/api/types'

export const TIERS: Record<string, string> = { opus: 'Opus', sonnet: 'Sonnet', haiku: 'Haiku', fable: 'Fable' }

/** The engine a session runs on when nothing says otherwise (mirrors the
 *  `engine ?? 'claude_code'` default the create-session hook sends). */
const DEFAULT_ENGINE: AgentEngine = 'claude_code'

/** Operator-facing name of each engine. One map, so a chip, a menu and a
 *  tooltip can never disagree about what the agent is called. */
export const ENGINE_LABELS: Record<string, string> = {
  claude_code: 'Claude Code',
  codex: 'Codex',
}

/** Which engine's naming rules a raw model id follows. Shape-only: when the
 *  session record knows its `engine`, that is authoritative and is passed in. */
export type ModelFamily = 'claude' | 'openai' | 'unknown'

// <min> is 1–2 digits and must end a segment, so a date suffix (-20260101) or a
// context tag ([1m]) is never mistaken for it.
const CLAUDE_ID = /^claude-(opus|sonnet|haiku|fable)-(\d+)(?:-(\d{1,2}))?(?:-|\[|$)/
// `gpt-5`, `gpt-5-codex`, `gpt-5.1-codex-mini` — a numeric version followed by
// zero or more lower-case words. `gpt-4o` deliberately does NOT match (the "o"
// is not a version separator), so it keeps rendering verbatim.
const OPENAI_ID = /^gpt-(\d+(?:\.\d+)?)((?:-[a-z0-9]+)*)$/

/** Engine family implied by a model id alone. */
export function modelFamily(id: string | null | undefined): ModelFamily {
  if (!id) return 'unknown'
  const base = id.trim().toLowerCase().split('[')[0]
  if (CLAUDE_ID.test(base) || base in TIERS || base === 'opusplan') return 'claude'
  if (OPENAI_ID.test(base)) return 'openai'
  return 'unknown'
}

/** `gpt-5.1-codex-mini` → "GPT-5.1 Codex Mini"; anything else verbatim. */
function openAiModelName(id: string): string {
  const m = OPENAI_ID.exec(id.trim().toLowerCase())
  if (!m) return id
  const words = m[2] ? m[2].slice(1).split('-') : []
  const suffix = words.map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(' ')
  return suffix ? `GPT-${m[1]} ${suffix}` : `GPT-${m[1]}`
}

/**
 * Friendly display name for a model id. `engine` is the session record's engine
 * when the caller has one — it pins the family for an id whose shape is
 * ambiguous; without it the id's own shape decides.
 */
export function modelName(id: string | null | undefined, engine?: AgentEngine | null): string {
  if (!id) return ''
  const family = engine === 'codex' ? 'openai' : engine === 'claude_code' ? 'claude' : modelFamily(id)
  if (family === 'openai') return openAiModelName(id)
  const m = CLAUDE_ID.exec(id)
  if (!m) return id
  return m[3] ? `${TIERS[m[1]]} ${m[2]}.${m[3]}` : `${TIERS[m[1]]} ${m[2]}`
}

/**
 * Web mirror of the server `model_tier` function.
 * Matches `claude-(opus|sonnet|haiku|fable)-…`, bare aliases in TIERS, and the
 * `opusplan` alias (→ 'opus'). Returns null for unknown/null/undefined ids —
 * including every Codex id, which is truthful: tiers are a Claude-model concept
 * (`server/sessions/model_policy.py` gates on them) and no OpenAI id has one.
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
 * Display label for a model alias or pinned id: the full "Opus 5" / "GPT-5
 * Codex" form when the id parses, else the tier's capitalised name ("fable" ->
 * "Fable"), else verbatim. Single source of truth for model labels in the UI.
 */
export function tierLabel(id: string | null | undefined, engine?: AgentEngine | null): string {
  if (!id) return 'this model'
  const full = modelName(id, engine)
  if (full && full !== id) return full
  return TIERS[id.toLowerCase()] ?? id
}

/**
 * Returns true when the model id belongs to the default gated tier (fable).
 * Never true for a non-Claude engine: the approval gate it mirrors matches on
 * Claude tiers, so a Codex session can never be parked by it.
 * Client-side hint only; the server policy is authoritative.
 */
export function isGatedTier(id: string | null | undefined, engine?: AgentEngine | null): boolean {
  if (engine != null && engine !== 'claude_code') return false
  return modelTier(id) === 'fable'
}

/** Operator-facing name of the engine a session runs on. */
export function engineLabel(engine: AgentEngine | null | undefined): string {
  const key = engine ?? DEFAULT_ENGINE
  return ENGINE_LABELS[key] ?? key
}

/**
 * The engine label to SHOW on an identity surface, or null when there is
 * nothing worth saying. Null for the default Claude Code engine: a deployment
 * that runs only Claude must not grow an "agent" chip on every card.
 */
export function engineBadge(engine: AgentEngine | null | undefined): string | null {
  if (!engine || engine === DEFAULT_ENGINE) return null
  return engineLabel(engine)
}
