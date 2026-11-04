/**
 * `/btw` (D-12): ask the Assistant about the session you are in without leaving it.
 * The dock composer's onCommand opens the Assistant drawer and, with a question, parks
 * it for the drawer to send — through `requestAssistant`, the one open-and-send path
 * (`shell/assistantGestures`). The question is tied to THIS session: the drawer sends
 * it with this session as its context, and only while this session is still open.
 *
 * Only the catalog's `client` entry is ours: the server lists it in coding sessions
 * with the env kill switch on, never in the Assistant's own. Anything else — including
 * a `btw` that is not ours, as when the env switch is off and the CLI's own `/btw`
 * passes through, like `/fork` and `/rewind` — goes on to the session.
 *
 * The Settings override (`assistantEnabled`, resolved by `GET /assistant/status`) is
 * invisible to the server's catalog, so with it off `/btw` is still ours here: it says
 * the Assistant is off and sends nothing. Unknown (still loading) counts as on.
 */
import { useCallback } from 'react'
import { toast } from 'sonner'
import { useAssistantStatus } from '@/api/assistant'
import { useRequestAssistant } from '@/shell/assistantGestures'
import type { SessionRuntime } from '@/session/useSessionRuntime'

export function useBtwCommand(
  rt: Pick<SessionRuntime, 'sessionId' | 'commands' | 'onCommand'>,
): SessionRuntime['onCommand'] {
  const requestAssistant = useRequestAssistant()
  const off = useAssistantStatus().data?.enabled === false
  const { sessionId, commands, onCommand } = rt
  const ours = commands.some((c) => c.name === 'btw' && c.kind === 'client')
  return useCallback((name: string, args: string) => {
    if (name !== 'btw' || !ours) { onCommand(name, args); return }
    if (off) { toast('The Assistant is off — turn it on in Settings'); return }
    requestAssistant({ send: args, sessionId })
  }, [ours, off, onCommand, requestAssistant, sessionId])
}
