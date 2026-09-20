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
      // Capped (not just `flex-shrink`) on a narrow phone: a long goal beside a
      // long title would otherwise shrink roughly proportionally to the
      // title's own share, still starving it (issue #1400). The cap gives the
      // title first claim on the row; the full goal stays one tap away via
      // the native `title` tooltip below (desktop) — narrow's own regression
      // test asserts this span never exceeds the cap.
      className="min-w-0 flex-shrink truncate narrow:max-w-[5rem] text-xs text-ink-muted"
      title={`Session goal (${label}): ${text}`}
    >
      {text}
    </span>
  )
}
