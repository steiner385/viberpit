/**
 * `/btw` (D-12): ask the Assistant about the session you are in without leaving it.
 * The dock composer's onCommand opens the Assistant drawer and, with a question, sends
 * it there as the drawer's next prompt — through `requestAssistant`, the one
 * open-and-send path (`shell/assistantGestures`). The drawer's context names the open
 * session, which is what scopes the side chat to it.
 *
 * Only the catalog's `client` entry is ours: the server lists it in coding sessions
 * with the Assistant on, never in the Assistant's own. Anything else — including a
 * `btw` that is not ours — goes on to the session.
 */
import { useCallback } from 'react'
import { useRequestAssistant } from '@/shell/assistantGestures'
import type { SessionRuntime } from '@/session/useSessionRuntime'

export function useBtwCommand(rt: Pick<SessionRuntime, 'commands' | 'onCommand'>): SessionRuntime['onCommand'] {
  const requestAssistant = useRequestAssistant()
  const { commands, onCommand } = rt
  const ours = commands.some((c) => c.name === 'btw' && c.kind === 'client')
  return useCallback((name: string, args: string) => {
    if (name !== 'btw' || !ours) { onCommand(name, args); return }
    requestAssistant({ send: args })
  }, [ours, onCommand, requestAssistant])
}
