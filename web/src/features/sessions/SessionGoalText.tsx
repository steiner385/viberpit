/**
 * SessionGoalText — the reckoned session goal, rendered in smaller subdued text
 * next to the session title in the header (mobile SessionView + desktop detail).
 *
 * Hidden when the goal is just the title fallback (goalSource "initial_prompt"
 * resolves to the session name) — duplicating the title beside itself adds no
 * information. Once a distinct goal exists (explicit /goal, or a future inferred
 * goal), it appears automatically.
 */

interface Props {
  goal?: string | null
  source?: string | null
  title: string
}

const SOURCE_LABEL: Record<string, string> = {
  explicit: 'set via /goal',
  inferred: 'inferred from the conversation',
  initial_prompt: 'derived from the session title',
}

export function SessionGoalText({ goal, source, title }: Props) {
  const text = (goal ?? '').trim()
  if (!text || text === title.trim()) return null
  const label = SOURCE_LABEL[source ?? ''] ?? 'reckoned'
  return (
    <span
      data-testid="session-goal"
      // The title has first claim on the row (issue #1400; 2026-09-29 critique
      // P2 measured the title at 124px beside ~280px of goal on desktop and at
      // 23px — "I…" — on a phone). Flex shrinks proportionally to content
      // width, so a long goal keeps MORE of the row than a short title unless
      // told otherwise: `shrink-[4]` makes this span give up width four times
      // faster than the title, and the 40% cap bounds it whatever the lengths.
      // Below `narrow:` it is hidden outright — Back, the chevrons, the pill and
      // the ⋯ menu already leave the title a few characters; the goal is on the
      // list card and one tap away, and its tooltip cannot be reached by touch.
      className="min-w-0 shrink-[4] max-w-[40%] truncate narrow:hidden text-xs text-ink-muted"
      title={`Session goal (${label}): ${text}`}
    >
      {text}
    </span>
  )
}
