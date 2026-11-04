/**
 * `/btw` (D-12): ask the Assistant about the session you are in without leaving it.
 * The dock composer's onCommand opens the Assistant drawer and, with a question, parks
 * it for the drawer to send — through `requestAssistant`, the one open-and-send path
 * (`shell/assistantGestures`). The question is tied to THIS session: the drawer sends
 * it with this session as its context, and only while this session is still open.
 *
 * Only the catalog's `client` entry is ours: the server lists it in coding sessions
 * with the Assistant on, never in the Assistant's own. Anything else — including a
 * `btw` that is not ours — goes on to the session.
 *
 * The server's catalog gate reads the env kill switch only; the Settings override
 * (`assistantEnabled`) is resolved by `GET /assistant/status`. With that off, `/btw`
 * is absent here too — out of the slash menu (`commands`) and never intercepted.
 * Unknown (still loading) counts as on, so the menu does not flicker.
 */
import { useCallback, useMemo } from 'react'
import { useAssistantStatus } from '@/api/assistant'
import { useRequestAssistant } from '@/shell/assistantGestures'
import type { SessionRuntime } from '@/session/useSessionRuntime'

const isBtw = (c: { name: string; kind: string }) => c.name === 'btw' && c.kind === 'client'

export function useBtwCommand(
  rt: Pick<SessionRuntime, 'sessionId' | 'commands' | 'onCommand'>,
): Pick<SessionRuntime, 'commands' | 'onCommand'> {
  const requestAssistant = useRequestAssistant()
  const off = useAssistantStatus().data?.enabled === false
  const { sessionId, commands: all, onCommand } = rt
  const commands = useMemo(() => (off ? all.filter((c) => !isBtw(c)) : all), [off, all])
  const ours = commands.some(isBtw)
  const handle = useCallback((name: string, args: string) => {
    if (name !== 'btw' || !ours) { onCommand(name, args); return }
    requestAssistant({ send: args, sessionId })
  }, [ours, onCommand, requestAssistant, sessionId])
  return { commands, onCommand: handle }
}
