import type { AgentCapabilities, ModelOption, Session } from '@/api/types'
import { canUseEngine } from '@/session/engineCapabilities'

const NO_EFFORTS: readonly string[] = []

/**
 * The reasoning efforts a session can pick and the one in force — the ONE reading
 * shared by the prompt row's effort picker and the error card's "Lower effort"
 * (D-19), so the two cannot offer different levels.
 *
 * The chosen model's catalog row (Codex) wins. Otherwise a Claude session whose
 * engine REPORTS live flag settings gets the levels the CLI listed for its current
 * model (`supportedEffortLevels`) — reported, not assumed: `canUseEngine` treats an
 * unknown capability as present, which is right for a control the catalog vouches
 * for and wrong for this one. No list yet (or an empty one) = no efforts. A null
 * model is the account default, which the catalog flags (Codex) when it knows it.
 * `efforts` is in catalog order, lowest first.
 */
export function effortChoices(
  model: string | null,
  session: Partial<Session> | undefined,
  caps: AgentCapabilities | undefined,
  models: readonly ModelOption[],
): { efforts: readonly string[]; effort: string | null } {
  const chosen = models.find((m) => m.id === model) ?? (model == null ? models.find((m) => m.default) : undefined)
  const catalog = canUseEngine(caps, 'reasoningEffort') ? chosen?.reasoningEfforts ?? [] : []
  const efforts = catalog.length > 0
    ? catalog
    : caps?.flagSettings === true ? session?.supportedEffortLevels ?? NO_EFFORTS : NO_EFFORTS
  return { efforts, effort: session?.reasoningEffort ?? chosen?.defaultReasoningEffort ?? null }
}

/** The level one below `effort` in `efforts`; null at the floor, or when the
 *  current level is unknown or not offered (nothing honest to step down from). */
export function lowerEffort(efforts: readonly string[], effort: string | null): string | null {
  const i = effort === null ? -1 : efforts.indexOf(effort)
  return i > 0 ? efforts[i - 1] : null
}
