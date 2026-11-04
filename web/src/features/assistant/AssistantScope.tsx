/**
 * The drawer header's scope line (D-12). It never claims a scope the server is not
 * enforcing.
 *
 * The server's scope is the session named by the last context the drawer SENT
 * (`sent`, recorded by every drawer send). While that is still in play — a turn
 * running, or prompts queued behind it — and the open session (`current`) differs,
 * the line says both: "About: <sent>" and what the next message will be about. With
 * nothing in flight the next send decides, so it names the open session, with its
 * status dot. Outside a session, with nothing in flight, it renders nothing and the
 * drawer reads exactly as it always has.
 */
import { useSessions } from '@/api/hooks'
import { useSessionPromptFlight } from '@/lib/promptFlight'
import { displayNameOf } from '@/session/displayName'
import { sessionTriage } from '@/session/triageTier'
import type { Session } from '@/api/types'
import { SessionStatusDot } from '@/features/sessions/SessionStatusDot'

interface Props {
  /** The open session — what the next drawer send will be about. */
  current: string | null
  /** The session the last sent context named (`undefined`: nothing sent yet). */
  sent: string | null | undefined
  /** A turn is running or prompts are queued: the server still holds `sent`. */
  inFlight: boolean
}

export function AssistantScope({ current, sent, inFlight }: Props) {
  const { data: sessions } = useSessions()
  const find = (id: string | null) => (id ? sessions?.find((s) => s.id === id) ?? { id } : null)
  const lagging = inFlight && sent !== undefined && sent !== current
  const about = find(lagging ? sent : current)
  if (!about && !lagging) return null
  const next = find(current)
  return (
    <div data-testid="assistant-scope" className="mt-0.5 min-w-0 text-xs text-ink-muted">
      {about && <ScopeLine session={about} />}
      {lagging && (
        <p className="truncate">
          {next ? <>Next message will be about <span className="text-ink">{displayNameOf(next)}</span></>
            : 'Next message won’t be about a session'}
        </p>
      )}
    </div>
  )
}

/** One session, by name, with its status dot when the list has its record. */
function ScopeLine({ session }: { session: Session | { id: string } }) {
  const title = displayNameOf(session)
  return (
    <p className="flex min-w-0 items-center gap-1.5">
      {'status' in session && <SessionDot session={session} />}
      <span className="truncate">About: <span className="text-ink">{title}</span></span>
    </p>
  )
}

function SessionDot({ session }: { session: Session }) {
  // The session card's own tier read (same function, same prompt-flight input), so
  // the dot here and the dot on its card are always the same colour.
  return <SessionStatusDot tier={sessionTriage(session, useSessionPromptFlight(session.id)).tier} />
}
