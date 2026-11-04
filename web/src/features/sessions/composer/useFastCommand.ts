/**
 * `/fast` (D-14): the catalog lists it as an app `client` command — only where the
 * engine has live flag settings AND the CLI calls the current model fast-capable — so
 * the browser runs it, through the same `POST /sessions/{id}/fast` as the ⚡ toggle
 * (`useSetFastMode`). The server's dispatcher only answers a `/fast` that reaches it
 * with a "send it from the message box" notice.
 *
 * Bare `/fast` toggles the stored choice; `on` / `off` set it. Anything else that is
 * not our client `/fast` goes on to the session, like `/fork` and `/btw`.
 *
 * `/effort` needs nothing here: it is a `control` command, so it already reaches the
 * server the way `/model` does and runs the same set-model action as the effort ▾.
 */
import { useCallback } from 'react'
import { toast } from 'sonner'
import { useSessions, useSetFastMode } from '@/api/hooks'
import type { SessionRuntime } from '@/session/useSessionRuntime'

export function useFastCommand(
  rt: Pick<SessionRuntime, 'sessionId' | 'commands' | 'onCommand'>,
): SessionRuntime['onCommand'] {
  const { sessionId, commands, onCommand } = rt
  const { data: sessions } = useSessions()
  const fastOn = sessions?.find((s) => s.id === sessionId)?.fastMode === true
  const { mutate } = useSetFastMode()
  const ours = commands.some((c) => c.name === 'fast' && c.kind === 'client')
  return useCallback((name: string, args: string) => {
    if (name !== 'fast' || !ours) { onCommand(name, args); return }
    const arg = args.trim().toLowerCase()
    const enabled = arg === '' ? !fastOn : arg === 'on' ? true : arg === 'off' ? false : null
    if (enabled === null) { toast('Usage: /fast [on|off]'); return }
    mutate({ id: sessionId, enabled })
  }, [ours, fastOn, onCommand, mutate, sessionId])
}
