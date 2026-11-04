/**
 * The drawer header's scope line (D-12): while a session is open, the Assistant is a
 * side chat about it — "About: <title>" with the session's status dot. Outside a
 * session (or before the session list has the record) it renders nothing, and the
 * drawer reads exactly as it always has.
 *
 * The open session is the one the drawer's context already names (`useOpenSessionId`,
 * the same value `AssistantSheet` sends as `sessionId`), so the line and the scope the
 * server enforces cannot disagree.
 */
import { useSessions } from '@/api/hooks'
import { useSessionPromptFlight } from '@/lib/promptFlight'
import { displayNameOf } from '@/session/displayName'
import { sessionTriage } from '@/session/triageTier'
import type { Session } from '@/api/types'
import { SessionStatusDot } from '@/features/sessions/SessionStatusDot'

export function AssistantScope({ sessionId }: { sessionId: string | null }) {
  const { data: sessions } = useSessions()
  const session = sessionId ? sessions?.find((s) => s.id === sessionId) : undefined
  return session ? <ScopeLine session={session} /> : null
}

function ScopeLine({ session }: { session: Session }) {
  // The session card's own tier read (same function, same prompt-flight input), so
  // the dot here and the dot on its card are always the same colour.
  const tier = sessionTriage(session, useSessionPromptFlight(session.id)).tier
  const title = displayNameOf(session)
  return (
    <p data-testid="assistant-scope" className="mt-0.5 flex min-w-0 items-center gap-1.5 text-xs text-ink-muted" title={`About: ${title}`}>
      <SessionStatusDot tier={tier} />
      <span className="truncate">About: <span className="text-ink">{title}</span></span>
    </p>
  )
}
