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
      className="min-w-0 flex-shrink truncate text-xs text-gray-500 dark:text-gray-400"
      title={`Session goal (${label}): ${text}`}
    >
      {text}
    </span>
  )
}
