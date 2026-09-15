import type { PromptOrigin, PromptOriginKind } from './events'

/**
 * Display names for the senders in `ORIGIN_KINDS` (server/sessions/prompt_origin.py).
 * THE map — the transcript card and anything else that names a sender reads it from
 * here, so a new kind cannot end up labelled two different ways on two surfaces.
 *
 * "Completion check" rather than "Completion nudge": the name a person sees should say
 * what the message is doing to their session, not name the internal mechanism.
 */
const LABELS: Record<PromptOriginKind, string> = {
  autopilot: 'Autopilot',
  completion: 'Completion check',
  assistant: 'AI assistant',
  automation: 'Automation',
}

/** A sender's own `label` wins (it names the specific automation, e.g. "PR Doctor"). */
export function originLabel(origin: PromptOrigin): string {
  return origin.label ?? LABELS[origin.kind] ?? LABELS.automation
}

/**
 * The hover explanation. Spells out the thing the whole feature exists to say — that a
 * person did not write this — because the visual language alone has to be learned once.
 */
export function originTitle(origin: PromptOrigin): string {
  const who = originLabel(origin)
  return origin.inferred
    ? `${who} sent this prompt — you did not type it. Reconstructed from automation `
      + `records after the fact, so the attribution is a close match rather than a `
      + `recorded one.`
    : `${who} sent this prompt — you did not type it.`
}
