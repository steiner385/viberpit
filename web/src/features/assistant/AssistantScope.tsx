/**
 * The drawer header's scope line (D-12). It shows the scope the SERVER holds, never
 * what the client merely sent.
 *
 * `scope` is `selectPromptScope(turns)`: the session named by the newest started turn's
 * context — moved only when a turn starts, so queued sends, sends mid-turn and sends
 * taken as answers leave it where the server left it, and a reload or another tab
 * shows the same. The line is "About: <scope>"; while a turn runs or prompts are
 * queued and the open session (`current`) differs, it adds what the next message will
 * be about. Before any context-bearing turn the first send sets the scope, so while
 * idle it names the open session; while in flight it names none (the running turn
 * carried no session). Outside a session it renders nothing, as the drawer always has.
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
  /** The server's scope (`selectPromptScope`); `undefined` before any context-bearing turn. */
  scope: string | null | undefined
  /** A turn is running or prompts are queued. */
  inFlight: boolean
}

export function AssistantScope({ current, scope, inFlight }: Props) {
  const { data: sessions } = useSessions()
  const find = (id: string | null) => (id ? sessions?.find((s) => s.id === id) ?? { id } : null)
  // No context-bearing turn yet: while idle, the first send sets the scope, so the open
  // session is what it will be. While a turn runs or prompts are queued, that turn
  // carried no session, so the server holds none — claim nothing rather than a guard
  // that is not in force.
  const held = scope === undefined ? (inFlight ? null : current) : scope
  const about = find(held)
  const showNext = inFlight && held !== current
  if (!about && !showNext) return null
  const next = find(current)
  return (
    <div data-testid="assistant-scope" className="mt-0.5 min-w-0 text-xs text-ink-muted">
      {about && <ScopeLine session={about} />}
      {showNext && (
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
